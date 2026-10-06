from __future__ import annotations

import aiosqlite
from pathlib import Path

from ..paths import DB_PATH

_DB_PATH = DB_PATH
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(_DB_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA busy_timeout=5000")
        await _db.execute("PRAGMA synchronous=NORMAL")
        await _db.execute("PRAGMA foreign_keys=ON")
        await _init_schema(_db)
    return _db


async def _init_schema(db: aiosqlite.Connection) -> None:
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    await db.executescript(sql)
    await db.commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
