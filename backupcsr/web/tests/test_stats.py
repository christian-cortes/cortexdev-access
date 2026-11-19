"""Pruebas de los agregados históricos del Panel (app/stats.py y /api/stats).

Corre con el python del venv del portal y usa directorios temporales: **no** toca
/mnt/nas, /etc, /run/lock ni MySQL reales.

    /opt/backupcsr/web/venv/bin/python backupcsr/web/tests/test_stats.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import yaml

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

TMP = Path(tempfile.mkdtemp(prefix="stats-"))
os.environ["BACKUP_NAS"] = str(TMP / "nas")
os.environ["BACKUP_OPT"] = str(TMP / "opt")
os.environ["BACKUP_LOG"] = str(TMP / "log")
os.environ["BACKUP_ETC"] = str(TMP / "etc")
os.environ["BACKUP_CRON"] = str(TMP / "cron" / "backupcsr")
os.environ["BACKUP_CATALOG"] = str(TMP / "jobs.yml")
Path(os.environ["BACKUP_NAS"]).mkdir(parents=True, exist_ok=True)
Path(os.environ["BACKUP_LOG"]).mkdir(parents=True, exist_ok=True)

JOBS = [
    {"slug": "demo", "name": "Demo", "enabled": 1, "dest_rel": "demo",
     "lockfile": str(TMP / "locks" / "demo.lock")},
    {"slug": "vieja", "name": "Vieja", "enabled": 0, "dest_rel": "vieja",
     "lockfile": str(TMP / "locks" / "vieja.lock")},
]
Path(os.environ["BACKUP_CATALOG"]).write_text(
    yaml.safe_dump({"jobs": JOBS}, allow_unicode=True),
    encoding="utf-8",
)

from app import api, logs, stats  # noqa: E402

USER = {"username": "tester", "role": "admin"}
REF = logs.now()


def check(name, ok, extra=""):
    global checks, failures
    checks += 1
    if not ok:
        failures += 1
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


checks = 0
failures = 0


def stamp(delta):
    return (REF + delta).strftime("%Y-%m-%d %H:%M:%S")


# Log del job activo: OK (600 s), PARCIAL, FALLO, OMITIDO, dry-run y EN_CURSO.
demo = Path(os.environ["BACKUP_LOG"]) / "demo.log"
demo.write_text("\n".join([
    f"{stamp(timedelta(days=-2, hours=-3))} [demo] === inicio backup=demo trigger=cron ===",
    "Transferring file `uno.bin'",
    "Removing old file `viejo.bin'",
    f"{stamp(timedelta(days=-2, hours=-3, minutes=10))} [demo] === fin demo ===",
    f"{stamp(timedelta(days=-1, hours=-4))} [demo] === inicio backup=demo trigger=cron ===",
    f"{stamp(timedelta(days=-1, hours=-4, minutes=1))} [demo] AVISO: 2 archivos con error",
    f"{stamp(timedelta(days=-1, hours=-4, minutes=5))} [demo] === fin demo ===",
    f"{stamp(timedelta(days=-1, hours=-2))} [demo] === inicio backup=demo trigger=cron ===",
    f"{stamp(timedelta(days=-1, hours=-2, minutes=1))} [demo] FALLO: conexión rechazada",
    f"{stamp(timedelta(hours=-5))} [demo] === inicio backup=demo (dry_run=1) ===",
    f"{stamp(timedelta(hours=-5, minutes=1))} [demo] === fin demo ===",
    f"{stamp(timedelta(hours=-3))} [demo] === inicio backup=demo trigger=cron ===",
    f"{stamp(timedelta(hours=-3))} [demo] OMITIDO: sin turno de cola",
    f"{stamp(timedelta(minutes=-20))} [demo] === inicio backup=demo trigger=cron ===",
    "",
]), encoding="utf-8")

# Log de la tarea deshabilitada: no debe entrar en ningún agregado.
vieja = Path(os.environ["BACKUP_LOG"]) / "vieja.log"
vieja.write_text("\n".join([
    f"{stamp(timedelta(hours=-8))} [vieja] === inicio backup=vieja trigger=cron ===",
    "Transferring file `no-cuenta.bin'",
    f"{stamp(timedelta(hours=-7))} [vieja] === fin vieja ===",
    "",
]), encoding="utf-8")

# ------------------------------------------------------------- fallback logs
stats.db.available = lambda: False
data = stats.history(JOBS, 30)
check("logs: fuente declarada", data["source"] == "logs", data["source"])
check("logs: la deshabilitada no aparece", all(item["slug"] != "vieja" for item in data["jobs"]),
      [item["slug"] for item in data["jobs"]])
check("logs: dry-run no cuenta", data["windows"]["d7"]["counts"]["OK"] == 1,
      data["windows"]["d7"]["counts"])
check("logs: parcial y fallo cuentan", data["windows"]["d7"]["counts"]["PARCIAL"] == 1
      and data["windows"]["d7"]["counts"]["FALLO"] == 1, data["windows"]["d7"]["counts"])
check("logs: omitida aparte de la tasa", data["windows"]["d7"]["counts"]["OMITIDO"] == 1,
      data["windows"]["d7"]["counts"])
check("logs: tasa 1 OK de 3 completadas", data["windows"]["d7"]["success_rate"] == 33.3,
      data["windows"]["d7"]["success_rate"])
check("logs: en curso no completa la tasa", data["windows"]["d7"]["completed"] == 3,
      data["windows"]["d7"])
check("logs: archivos transferidos/borrados", data["windows"]["d7"]["files_transferred"] == 1
      and data["windows"]["d7"]["files_removed"] == 1, data["windows"]["d7"])
check("logs: p50/p95 solo con OK", data["windows"]["d7"]["duration"] == {"p50_s": 600, "p95_s": 600},
      data["windows"]["d7"]["duration"])
check("logs: 30 días con días sin corridas en cero",
      len(data["daily"]) == 30 and data["daily"][0]["counts"]["OK"] == 0,
      data["daily"][0])
check("logs: la ventana de 7 días tiene 7 puntos", len(data["daily"][-7:]) == 7)

# ------------------------------------------------------------------- DB
def db_rows(sql, params=None):
    return [
        {"job_slug": "demo",
         "started_at": logs.to_naive(REF - timedelta(days=1, minutes=30)),
         "finished_at": logs.to_naive(REF - timedelta(days=1, minutes=20)),
         "status": "OK", "files_transferred": 4, "files_removed": 1},
        {"job_slug": "demo",
         "started_at": logs.to_naive(REF - timedelta(days=2, minutes=30)),
         "finished_at": logs.to_naive(REF - timedelta(days=2, minutes=10)),
         "status": "OK", "files_transferred": 2, "files_removed": 0},
        {"job_slug": "demo",
         "started_at": logs.to_naive(REF - timedelta(hours=2)),
         "finished_at": logs.to_naive(REF - timedelta(hours=1, minutes=50)),
         "status": "FALLO", "files_transferred": 0, "files_removed": 0},
    ]


stats.db.available = lambda: True
stats.db.query = db_rows
db_data = stats.history(JOBS, 30)
check("db: fuente declarada", db_data["source"] == "db", db_data["source"])
check("db: éxito 2 de 3", db_data["windows"]["d7"]["success_rate"] == 66.7,
      db_data["windows"]["d7"]["success_rate"])
check("db: p50 con interpolación", db_data["windows"]["d7"]["duration"]["p50_s"] == 900,
      db_data["windows"]["d7"]["duration"])
check("db: archivos sumados", db_data["windows"]["d7"]["files_transferred"] == 6,
      db_data["windows"]["d7"])
check("db: tarea activa con métricas", len(db_data["jobs"]) == 1
      and db_data["jobs"][0]["slug"] == "demo", db_data["jobs"])

# --------------------------------------------------- /api/stats y avg duration
api.effective_jobs = lambda: JOBS
api.db.available = lambda: False
stats.db.available = lambda: False
endpoint = asyncio.run(api.history_stats(30, USER))
check("/api/stats: responde con ventanas", set(endpoint["windows"]) == {"d7", "d30"},
      endpoint.keys())

fake_jobs = [
    {"slug": "demo", "name": "Demo", "enabled": 1, "status": "OK", "size": {},
     "size_bytes": 0, "last_run": {"duration_s": 600, "started_at": REF.isoformat()}},
    {"slug": "vieja", "name": "Vieja", "enabled": 0, "status": "DESHABILITADA", "size": {},
     "size_bytes": 0, "last_run": {"duration_s": 3600, "started_at": REF.isoformat()}},
]
api.collect_jobs = lambda: fake_jobs
api.sizes.warm = lambda *args, **kwargs: False
api.sizes.nas_stats = lambda: None
api.sizes.growth = lambda *args, **kwargs: {}
api.files.nas_ready = lambda: False
api.build_alerts = lambda jobs: []
summary = asyncio.run(api.summary(USER))
check("summary: duración media excluye deshabilitadas", summary["avg_duration_s"] == 600,
      summary["avg_duration_s"])

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"{checks - failures}/{checks} PASS")
sys.exit(1 if failures else 0)
