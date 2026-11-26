"""Proyectos externos: catálogo editable en SQLite y estado independiente."""
from pathlib import Path

import yaml

from . import db, notify, probe, status

SEED_PATH = Path(__file__).resolve().parent.parent / "external-projects.yml"


def seed():
    with db.db() as conn:
        seeded = conn.execute("SELECT 1 FROM meta WHERE key = 'external_seeded'").fetchone()
        icons_seeded = conn.execute(
            "SELECT 1 FROM meta WHERE key = 'external_icons_seeded'"
        ).fetchone()
        if seeded and icons_seeded:
            return
        with SEED_PATH.open(encoding="utf-8") as handle:
            groups = yaml.safe_load(handle)
        for group in groups:
            for project in group["projects"]:
                if not seeded:
                    conn.execute(
                        "INSERT INTO external_projects (company, name, url, local_slug) VALUES (?, ?, ?, ?)",
                        (group["company"], project["name"], project["url"], project.get("local_slug")),
                    )
                elif not icons_seeded and project.get("local_slug"):
                    conn.execute(
                        """UPDATE external_projects SET local_slug = ?
                           WHERE company = ? AND url = ? AND local_slug IS NULL""",
                        (project["local_slug"], group["company"], project["url"]),
                    )
        if not seeded:
            conn.execute("INSERT INTO meta (key, value) VALUES ('external_seeded', '1')")
        if not icons_seeded:
            conn.execute("INSERT INTO meta (key, value) VALUES ('external_icons_seeded', '1')")


def projects():
    with db.db() as conn:
        rows = conn.execute(
            """SELECT id, company, name, url, local_slug, active, state, state_since,
                      checked_url, checked_at, last_state, code, latency_ms, error,
                      failures, successes, icon_type, icon_checked_at, icon_checked_url
               FROM external_projects WHERE active = 1 ORDER BY company COLLATE NOCASE, id"""
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        if item["url"] != item["checked_url"]:
            for key in ("state_since", "checked_at", "last_state", "code", "latency_ms", "error"):
                item[key] = None
            item["state"] = "unknown"
        result.append(item)
    return result


def icon(project_id):
    with db.db() as conn:
        return conn.execute(
            """SELECT icon_data, icon_type FROM external_projects
               WHERE id = ? AND active = 1 AND url = icon_checked_url
                 AND icon_data IS NOT NULL AND icon_type IS NOT NULL""",
            (project_id,),
        ).fetchone()


async def run_checks():
    items = projects()
    services = [
        {"url": item["url"], "accept_set": db.DEFAULT_ACCEPT}
        for item in items
    ]
    results = await probe.probe_all(services)
    events = []
    now = db.now_utc()
    with db.db() as conn:
        for item, result in zip(items, results):
            current = conn.execute(
                "SELECT * FROM external_projects WHERE id = ? AND active = 1 AND url = ?",
                (item["id"], item["url"]),
            ).fetchone()
            if not current:
                continue
            changed_url = current["checked_url"] != current["url"]
            old = "unknown" if changed_url else current["state"]
            failures = (0 if changed_url else current["failures"]) + 1 if not result["ok"] else 0
            successes = (0 if changed_url else current["successes"]) + 1 if result["ok"] else 0
            streak = [bool(result["ok"])] * min(
                successes if result["ok"] else failures,
                max(status.FAIL_THRESHOLD, status.OK_THRESHOLD),
            )
            new = status.advance(old, streak, result["state"])
            since = now if changed_url or new != old else current["state_since"]
            conn.execute(
                """UPDATE external_projects SET state = ?, state_since = ?, checked_url = ?,
                   checked_at = ?, last_state = ?, code = ?, latency_ms = ?, error = ?,
                   failures = ?, successes = ? WHERE id = ?""",
                (new, since, current["url"], now, result["state"], result["code"],
                 result["latency_ms"], result["error"], failures, successes, current["id"]),
            )
            if new == "offline" and old != "offline":
                events.append(("service.down", current, result))
            elif old == "offline" and new in status.UP_STATES:
                events.append(("service.recovered", current, result))
    for event, item, result in events:
        down = event == "service.down"
        scope = f"external-{item['id']}"
        notify.publish(
            "index", event,
            f"{item['name']} {'no responde' if down else 'recuperado'}",
            body=(f"{item['company']}: " + (
                result["error"] or f"Sin respuesta ({result['code'] or 'sin código'})."
                if down else "El proyecto vuelve a responder."
            )),
            url=item["url"], dedupe_key=scope, scope=scope,
        )
