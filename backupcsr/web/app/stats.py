"""Agregados históricos del Panel (ventanas de 7/30 días).

Se calculan sobre `runs` cuando MySQL está disponible y, si no, sobre los logs
parseados. Solo cuentan tareas habilitadas y corridas reales (`dry_run=0`).
No hay tablas nuevas ni rollups: la ventana se limita a 30 días para mantener la
consulta acotada.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from math import ceil, floor

from . import db, logs

log = logging.getLogger("backupcsr-web")

STATUSES = ("OK", "FALLO", "PARCIAL", "INCIERTO", "OMITIDO", "EN_CURSO")
COMPLETED = ("OK", "FALLO", "PARCIAL", "INCIERTO")
WINDOWS = (7, 30)


def _active_jobs(jobs: list[dict]) -> list[dict]:
    return [job for job in jobs if job.get("enabled")]


def _empty_counts() -> dict:
    return {status: 0 for status in STATUSES}


def _empty_day() -> dict:
    return {
        "counts": _empty_counts(),
        "files_transferred": 0,
        "files_removed": 0,
    }


def _normalize_status(value: str | None) -> str:
    return value if value in STATUSES else "INCIERTO"


def _percentile(values: list[int], pct: float) -> int | None:
    """Percentil con interpolación lineal; `None` si no hay valores."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return int(ordered[0])
    position = (len(ordered) - 1) * pct / 100
    low = floor(position)
    high = ceil(position)
    if low == high:
        return int(ordered[low])
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return int(round(value))


def _duration(started, finished) -> int | None:
    if not isinstance(started, datetime) or not isinstance(finished, datetime):
        return None
    seconds = int((finished - started).total_seconds())
    return seconds if seconds >= 0 else None


def _run_record(job_slug: str, job_name: str, started, status: str | None,
                duration_s: int | None, files_transferred, files_removed) -> dict:
    return {
        "job_slug": job_slug,
        "job_name": job_name,
        "started_at": started,
        "status": _normalize_status(status),
        "duration_s": duration_s,
        "files_transferred": int(files_transferred or 0),
        "files_removed": int(files_removed or 0),
    }


def _db_runs(jobs: list[dict], cutoff) -> list[dict]:
    """Corridas reales de las tareas activas dentro de la ventana, desde MySQL."""
    if not jobs:
        return []
    slugs = [job["slug"] for job in jobs]
    names = {job["slug"]: job.get("name") or job["slug"] for job in jobs}
    placeholders = ", ".join(["%s"] * len(slugs))
    rows = db.query(
        f"""
        SELECT j.slug AS job_slug, r.started_at, r.finished_at, r.status,
               r.files_transferred, r.files_removed
        FROM runs r
        JOIN jobs j ON j.id = r.job_id
        WHERE j.slug IN ({placeholders})
          AND r.dry_run = 0
          AND r.started_at >= %s
        ORDER BY r.started_at
        """,
        (*slugs, logs.to_naive(cutoff)),
    )
    result = []
    for row in rows:
        started = logs.from_naive(row["started_at"]) if row.get("started_at") else None
        finished = logs.from_naive(row["finished_at"]) if row.get("finished_at") else None
        slug = row.get("job_slug")
        result.append(_run_record(
            slug, names.get(slug, slug), started, row.get("status"),
            _duration(started, finished), row.get("files_transferred"),
            row.get("files_removed"),
        ))
    return result


def _log_runs(jobs: list[dict], cutoff) -> list[dict]:
    """Corridas reales de las tareas activas dentro de la ventana, desde logs."""
    result = []
    for job in jobs:
        slug = job["slug"]
        name = job.get("name") or slug
        for run in logs.parse_slug(slug):
            if run.get("dry_run"):
                continue
            started = run.get("started_at")
            if not isinstance(started, datetime) or started < cutoff:
                continue
            result.append(_run_record(
                slug, name, started, run.get("status"), logs.duration_s(run),
                run.get("files_transferred"), run.get("files_removed"),
            ))
    result.sort(key=lambda item: item["started_at"])
    return result


def _window(runs: list[dict], since) -> dict:
    selected = [run for run in runs if run["started_at"] >= since]
    counts = _empty_counts()
    for run in selected:
        counts[run["status"]] += 1
    completed = sum(counts[status] for status in COMPLETED)
    durations = [
        run["duration_s"] for run in selected
        if run["status"] == "OK" and run["duration_s"] and run["duration_s"] > 0
    ]
    return {
        "counts": counts,
        "completed": completed,
        "success_rate": round(counts["OK"] * 100 / completed, 1) if completed else None,
        "files_transferred": sum(run["files_transferred"] for run in selected),
        "files_removed": sum(run["files_removed"] for run in selected),
        "duration": {
            "p50_s": _percentile(durations, 50),
            "p95_s": _percentile(durations, 95),
        },
    }


def _job_stats(jobs: list[dict], runs: list[dict], since) -> list[dict]:
    names = {job["slug"]: job.get("name") or job["slug"] for job in jobs}
    grouped: dict[str, list[dict]] = {}
    for run in runs:
        if run["started_at"] < since:
            continue
        grouped.setdefault(run["job_slug"], []).append(run)

    result = []
    for slug, items in grouped.items():
        counts = _empty_counts()
        for run in items:
            counts[run["status"]] += 1
        completed = sum(counts[status] for status in COMPLETED)
        durations = [
            run["duration_s"] for run in items
            if run["status"] == "OK" and run["duration_s"] and run["duration_s"] > 0
        ]
        last = max(items, key=lambda item: item["started_at"])
        result.append({
            "slug": slug,
            "name": names.get(slug, slug),
            "counts": counts,
            "success_rate": round(counts["OK"] * 100 / completed, 1) if completed else None,
            "p50_s": _percentile(durations, 50),
            "p95_s": _percentile(durations, 95),
            "last_s": last["duration_s"],
            "last_status": last["status"],
        })
    result.sort(key=lambda item: (
        item["p95_s"] is None, -(item["p95_s"] or 0), item["name"].lower(),
    ))
    return result


def history(jobs: list[dict], days: int = 30) -> dict:
    """Agregados de 7/30 días para el Panel.

    `jobs` es el catálogo efectivo; aquí se descartan las tareas deshabilitadas.
    """
    days = max(7, min(int(days or 30), 365))
    active = _active_jobs(jobs)
    reference = logs.now()
    span = max(days, max(WINDOWS))
    cutoff = reference - timedelta(days=span)

    source = "logs"
    runs: list[dict] = []
    if db.available():
        try:
            runs = _db_runs(active, cutoff)
            source = "db"
        except Exception:  # noqa: BLE001 - el histórico cae a logs
            log.exception("no se pudieron leer las corridas para las estadísticas")
    if source == "logs":
        runs = _log_runs(active, cutoff)

    daily_map: dict[str, dict] = {}
    for offset in range(span - 1, -1, -1):
        key = (reference.date() - timedelta(days=offset)).isoformat()
        daily_map[key] = _empty_day()
    for run in runs:
        key = run["started_at"].date().isoformat()
        bucket = daily_map.setdefault(key, _empty_day())
        bucket["counts"][run["status"]] += 1
        bucket["files_transferred"] += run["files_transferred"]
        bucket["files_removed"] += run["files_removed"]

    daily = []
    for offset in range(days - 1, -1, -1):
        key = (reference.date() - timedelta(days=offset)).isoformat()
        item = daily_map.get(key) or _empty_day()
        daily.append({"date": key, **item})

    windows = {
        f"d{days_window}": _window(runs, reference - timedelta(days=days_window))
        for days_window in WINDOWS
    }
    return {
        "generated_at": reference.isoformat(),
        "days": days,
        "source": source,
        "windows": windows,
        "daily": daily,
        "jobs": _job_stats(active, runs, reference - timedelta(days=days)),
    }
