"""Descubrimiento y almacenamiento limitado de favicons externos."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from . import db, probe

log = logging.getLogger("portal-api")
MAX_HTML = 256 * 1024
MAX_ICON = 128 * 1024
INTERVAL = timedelta(days=1)


class IconParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.icons = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        rel = (attrs.get("rel") or "").lower().split()
        if tag.lower() == "link" and any(item in ("icon", "apple-touch-icon") for item in rel):
            if attrs.get("href"):
                self.icons.append(attrs["href"])


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def get_bounded(client, url, host, limit, truncate=False):
    for _ in range(6):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.hostname != host:
            raise ValueError("el favicon apunta fuera del dominio del proyecto")
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirección sin destino")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    if truncate:
                        return str(response.url), response.headers.get("content-type", ""), bytes(body[:limit])
                    raise ValueError("respuesta de favicon demasiado grande")
            return str(response.url), response.headers.get("content-type", ""), bytes(body)
    raise ValueError("demasiadas redirecciones")


async def discover(client, project):
    host = urlsplit(project["url"]).hostname
    try:
        page_url, content_type, html = await get_bounded(
            client, project["url"], host, MAX_HTML, truncate=True
        )
        parser = IconParser()
        if "html" in content_type.lower():
            parser.feed(html.decode("utf-8", errors="replace"))
        candidates = [urljoin(page_url, href) for href in parser.icons]
        candidates.append(urljoin(page_url, "/favicon.ico"))
        for url in dict.fromkeys(candidates):
            try:
                _, _, data = await get_bounded(client, url, host, MAX_ICON)
                mime = image_type(data)
                if mime:
                    return data, mime
                log.warning("favicon inválido para %s: %s", project["name"], url)
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("no se pudo leer favicon de %s: %s", project["name"], exc)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("no se pudo buscar favicon de %s: %s", project["name"], exc)
    return None, None


async def refresh(projects, local_icons):
    now = datetime.now(timezone.utc)
    pending = []
    for project in projects:
        if project.get("local_slug") in local_icons:
            continue
        checked = db.parse_ts(project.get("icon_checked_at"))
        if (project["url"] != project.get("icon_checked_url") or not checked
                or now - checked >= INTERVAL):
            pending.append(project)
    if not pending:
        return
    semaphore = asyncio.Semaphore(probe.PROBE_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=probe.PROBE_TIMEOUT, follow_redirects=False,
        headers={"User-Agent": probe.USER_AGENT},
    ) as client:
        async def run(project):
            async with semaphore:
                return await discover(client, project)

        results = await asyncio.gather(*(run(project) for project in pending))
    with db.db() as conn:
        for project, (data, mime) in zip(pending, results):
            conn.execute(
                """UPDATE external_projects SET
                   icon_data = CASE WHEN ? IS NOT NULL THEN ? WHEN icon_checked_url = url THEN icon_data ELSE NULL END,
                   icon_type = CASE WHEN ? IS NOT NULL THEN ? WHEN icon_checked_url = url THEN icon_type ELSE NULL END,
                   icon_checked_url = ?, icon_checked_at = ?
                   WHERE id = ? AND active = 1 AND url = ?""",
                (data, data, mime, mime, project["url"], db.now_utc(),
                 project["id"], project["url"]),
            )
