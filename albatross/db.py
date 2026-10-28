import logging
from collections.abc import Iterable, Sequence

import asyncpg

from albatross.config import ROOT, settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_dsn, min_size=1, max_size=10)
    return _pool


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def upsert(
    table: str,
    cols: Sequence[str],
    rows: Iterable[Sequence],
    conflict: Sequence[str],
    update: Sequence[str] | None = None,
) -> int:
    """INSERT ... ON CONFLICT. `update=None` updates every non-conflict column; `update=[]` ignores dupes."""
    rows = list(rows)
    if not rows:
        return 0
    if update is None:
        update = [c for c in cols if c not in conflict]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    action = (
        "DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in update) if update else "DO NOTHING"
    )
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict)}) {action}"
    )
    p = await pool()
    async with p.acquire() as conn:
        await conn.executemany(sql, rows)
    return len(rows)


async def migrate() -> None:
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())"
        )
        done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
        for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
