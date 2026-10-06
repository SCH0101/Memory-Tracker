"""Non-blocking cache for dashboard time-series JSON payloads."""
from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime
from typing import Awaitable, Callable

from ..db.connection import get_db
from .dashboard_datapoints import ensure_dashboard_datapoint_tables

FREQUENCIES = ("yearly", "quarterly")
READY_STATUSES = {"ready", "dirty", "building", "missing", "error"}

Builder = Callable[[str], Awaitable[dict]]

_locks: dict[str, asyncio.Lock] = {freq: asyncio.Lock() for freq in FREQUENCIES}
_jobs_lock = asyncio.Lock()
_jobs: dict[str, dict] = {}
_freq_jobs: dict[str, str] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_frequency(value: str | None) -> str:
    return "quarterly" if str(value or "").lower() == "quarterly" else "yearly"


async def _add_column_if_missing(db, table: str, ddl: str) -> None:
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except Exception:
        pass


async def ensure_dashboard_cache_table() -> None:
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS dashboard_timeseries_cache (
               frequency TEXT PRIMARY KEY,
               payload_json TEXT NOT NULL DEFAULT '',
               version TEXT NOT NULL DEFAULT '',
               status TEXT NOT NULL DEFAULT 'missing',
               dirty_reason TEXT NOT NULL DEFAULT '',
               error TEXT NOT NULL DEFAULT '',
               raw_max_id INTEGER NOT NULL DEFAULT 0,
               verified_max_id INTEGER NOT NULL DEFAULT 0,
               quarantine_max_id INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    for ddl in (
        "payload_json TEXT NOT NULL DEFAULT ''",
        "version TEXT NOT NULL DEFAULT ''",
        "status TEXT NOT NULL DEFAULT 'missing'",
        "dirty_reason TEXT NOT NULL DEFAULT ''",
        "error TEXT NOT NULL DEFAULT ''",
        "raw_max_id INTEGER NOT NULL DEFAULT 0",
        "verified_max_id INTEGER NOT NULL DEFAULT 0",
        "quarantine_max_id INTEGER NOT NULL DEFAULT 0",
        "created_at TEXT NOT NULL DEFAULT ''",
        "updated_at TEXT NOT NULL DEFAULT ''",
    ):
        await _add_column_if_missing(db, "dashboard_timeseries_cache", ddl)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_dashboard_verified_freq_metric_period "
        "ON dashboard_datapoints_verified(frequency, metric, period_int)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_dashboard_verified_broker_subject "
        "ON dashboard_datapoints_verified(source_broker, subject_entity)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_dashboard_quarantine_freq_metric_period "
        "ON dashboard_datapoints_quarantine(frequency, metric, period_int)"
    )
    await db.commit()


async def _max_id(db, table: str) -> int:
    cur = await db.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}")
    row = await cur.fetchone()
    return int(row["m"] or 0) if row else 0


def _meta_from_row(row, frequency: str, status_override: str | None = None) -> dict:
    if row is None:
        return {
            "frequency": frequency,
            "status": status_override or "missing",
            "version": "",
            "dirty_reason": "",
            "error": "",
            "raw_max_id": 0,
            "verified_max_id": 0,
            "quarantine_max_id": 0,
            "created_at": "",
            "updated_at": "",
            "job_id": _freq_jobs.get(frequency, ""),
        }
    return {
        "frequency": frequency,
        "status": status_override or (row["status"] or "missing"),
        "version": row["version"] or "",
        "dirty_reason": row["dirty_reason"] or "",
        "error": row["error"] or "",
        "raw_max_id": int(row["raw_max_id"] or 0),
        "verified_max_id": int(row["verified_max_id"] or 0),
        "quarantine_max_id": int(row["quarantine_max_id"] or 0),
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "job_id": _freq_jobs.get(frequency, ""),
    }


async def get_cached_timeseries(frequency: str) -> tuple[dict | None, dict]:
    frequency = normalize_frequency(frequency)
    await ensure_dashboard_cache_table()
    db = await get_db()
    cur = await db.execute(
        """SELECT *
           FROM dashboard_timeseries_cache
           WHERE frequency=?""",
        (frequency,),
    )
    row = await cur.fetchone()
    meta = _meta_from_row(row, frequency)
    if row is None or not (row["payload_json"] or "").strip():
        return None, meta
    try:
        payload = json.loads(row["payload_json"])
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = f"cache JSON decode failed: {exc}"
        return None, meta
    payload["cache"] = meta
    return payload, meta


async def mark_cache_dirty(frequency: str, reason: str = "") -> None:
    frequency = normalize_frequency(frequency)
    await ensure_dashboard_cache_table()
    db = await get_db()
    now = _now()
    await db.execute(
        """INSERT INTO dashboard_timeseries_cache
           (frequency, payload_json, version, status, dirty_reason, error,
            raw_max_id, verified_max_id, quarantine_max_id, created_at, updated_at)
           VALUES (?, '', '', 'dirty', ?, '', 0, 0, 0, ?, ?)
           ON CONFLICT(frequency) DO UPDATE SET
             status=CASE
               WHEN dashboard_timeseries_cache.status='building' THEN 'building'
               ELSE 'dirty'
             END,
             dirty_reason=excluded.dirty_reason,
             error='',
             updated_at=excluded.updated_at""",
        (frequency, reason or "dashboard data changed", now, now),
    )
    await db.commit()


async def mark_cache_dirty_many(frequencies: list[str] | tuple[str, ...], reason: str = "") -> None:
    targets = FREQUENCIES if "all" in frequencies else frequencies
    for freq in targets:
        await mark_cache_dirty(freq, reason)


async def _mark_building(frequency: str) -> None:
    await ensure_dashboard_cache_table()
    db = await get_db()
    now = _now()
    await db.execute(
        """INSERT INTO dashboard_timeseries_cache
           (frequency, payload_json, version, status, dirty_reason, error,
            raw_max_id, verified_max_id, quarantine_max_id, created_at, updated_at)
           VALUES (?, '', '', 'building', '', '', 0, 0, 0, ?, ?)
           ON CONFLICT(frequency) DO UPDATE SET
             status='building',
             error='',
             updated_at=excluded.updated_at""",
        (frequency, now, now),
    )
    await db.commit()


async def _store_ready(frequency: str, payload: dict) -> dict:
    db = await get_db()
    now = _now()
    raw_max = await _max_id(db, "dashboard_datapoints_raw")
    verified_max = await _max_id(db, "dashboard_datapoints_verified")
    quarantine_max = await _max_id(db, "dashboard_datapoints_quarantine")
    version = str(payload.get("version") or f"{raw_max}-{verified_max}-{quarantine_max}-{frequency}")
    stored_payload = dict(payload)
    stored_payload.pop("cache", None)
    await db.execute(
        """INSERT INTO dashboard_timeseries_cache
           (frequency, payload_json, version, status, dirty_reason, error,
            raw_max_id, verified_max_id, quarantine_max_id, created_at, updated_at)
           VALUES (?, ?, ?, 'ready', '', '', ?, ?, ?, ?, ?)
           ON CONFLICT(frequency) DO UPDATE SET
             payload_json=excluded.payload_json,
             version=excluded.version,
             status='ready',
             dirty_reason='',
             error='',
             raw_max_id=excluded.raw_max_id,
             verified_max_id=excluded.verified_max_id,
             quarantine_max_id=excluded.quarantine_max_id,
             updated_at=excluded.updated_at""",
        (
            frequency, json.dumps(stored_payload, ensure_ascii=False),
            version, raw_max, verified_max, quarantine_max, now, now,
        ),
    )
    await db.commit()
    return {
        "frequency": frequency,
        "status": "ready",
        "version": version,
        "raw_max_id": raw_max,
        "verified_max_id": verified_max,
        "quarantine_max_id": quarantine_max,
        "updated_at": now,
    }


async def _store_error(frequency: str, exc: BaseException) -> dict:
    db = await get_db()
    now = _now()
    err = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    tb = traceback.format_exc()
    await db.execute(
        """INSERT INTO dashboard_timeseries_cache
           (frequency, payload_json, version, status, dirty_reason, error,
            raw_max_id, verified_max_id, quarantine_max_id, created_at, updated_at)
           VALUES (?, '', '', 'error', '', ?, 0, 0, 0, ?, ?)
           ON CONFLICT(frequency) DO UPDATE SET
             status='error',
             error=?,
             updated_at=excluded.updated_at""",
        (frequency, (tb or err)[:4000], now, now, (tb or err)[:4000]),
    )
    await db.commit()
    return {"frequency": frequency, "status": "error", "error": err, "updated_at": now}


async def _default_builder(frequency: str) -> dict:
    from .dashboard_timeseries import build_dashboard_timeseries_payload
    return await build_dashboard_timeseries_payload(frequency)


async def rebuild_timeseries_cache(
    frequency: str,
    builder: Builder | None = None,
) -> dict:
    frequency = normalize_frequency(frequency)
    lock = _locks[frequency]
    if lock.locked():
        return {
            "frequency": frequency,
            "status": "building",
            "started": False,
            "job_id": _freq_jobs.get(frequency, ""),
        }
    async with lock:
        await _mark_building(frequency)
        try:
            payload = await (builder or _default_builder)(frequency)
            result = await _store_ready(frequency, payload)
            result["started"] = True
            return result
        except Exception as exc:
            result = await _store_error(frequency, exc)
            result["started"] = True
            return result


def _new_job_id(frequency: str) -> str:
    return f"cache_{frequency}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"


async def _run_cache_job(frequency: str, job_id: str, builder: Builder | None = None) -> None:
    _jobs[job_id]["status"] = "building"
    try:
        result = await rebuild_timeseries_cache(frequency, builder)
        _jobs[job_id].update(result)
        _jobs[job_id]["status"] = result.get("status", "done")
    except Exception as exc:
        _jobs[job_id].update({"status": "error", "error": str(exc)[:500]})
    finally:
        if _freq_jobs.get(frequency) == job_id:
            _freq_jobs.pop(frequency, None)
        _jobs[job_id]["finished_at"] = _now()


async def start_cache_rebuild(frequency: str, builder: Builder | None = None) -> dict:
    frequency = normalize_frequency(frequency)
    async with _jobs_lock:
        running = _freq_jobs.get(frequency)
        if running:
            return {"frequency": frequency, "job_id": running, "started": False, "status": "building"}
        job_id = _new_job_id(frequency)
        _freq_jobs[frequency] = job_id
        _jobs[job_id] = {
            "id": job_id,
            "frequency": frequency,
            "status": "queued",
            "started_at": _now(),
        }
    asyncio.create_task(_run_cache_job(frequency, job_id, builder))
    return {"frequency": frequency, "job_id": job_id, "started": True, "status": "queued"}


async def start_cache_rebuild_many(frequencies: list[str] | tuple[str, ...]) -> list[dict]:
    targets = FREQUENCIES if "all" in frequencies else tuple(normalize_frequency(f) for f in frequencies)
    out = []
    for freq in targets:
        out.append(await start_cache_rebuild(freq))
    return out


async def get_cache_status() -> dict:
    await ensure_dashboard_cache_table()
    db = await get_db()
    rows = await db.execute_fetchall("SELECT * FROM dashboard_timeseries_cache ORDER BY frequency")
    by_freq = {r["frequency"]: _meta_from_row(r, r["frequency"]) for r in rows}
    for freq in FREQUENCIES:
        by_freq.setdefault(freq, _meta_from_row(None, freq))
    return {
        "frequencies": by_freq,
        "jobs": list(_jobs.values())[-20:],
    }
