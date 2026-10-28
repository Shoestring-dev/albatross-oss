"""Append-only call ledger (docs/SPEC-LEDGER.md). Every row carries the hash of the row before it,
so any after-the-fact edit (the table also refuses UPDATE/DELETE) breaks the chain visibly."""

import hashlib

import orjson

from albatross import db

GENESIS = "0" * 64
FIELDS = ["logged_at", "source_kind", "source_id", "scope", "market", "direction", "tf", "entry_type",
          "ref_price", "stop", "targets", "horizon_until", "reward_risk", "confidence", "status", "thesis",
          "evidence", "critique", "parent_id", "agent_run_id"]


def _digest(prev: str, row: dict) -> str:
    body = orjson.dumps({k: row.get(k) for k in FIELDS}, option=orjson.OPT_SORT_KEYS | orjson.OPT_NAIVE_UTC)
    return hashlib.sha256(prev.encode() + body).hexdigest()


async def append(rows: list[dict]) -> list[int]:
    """Insert rows in order under a lock so the chain is linear. Each dict uses FIELDS' names;
    logged_at is set here (server clock), evidence/critique are plain JSON-able values."""
    if not rows:
        return []
    pool = await db.pool()
    ids = []
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('trade_calls'))")
        prev = await conn.fetchval("SELECT hash FROM trade_calls ORDER BY id DESC LIMIT 1") or GENESIS
        for r in rows:
            r = dict(r)
            r["logged_at"] = await conn.fetchval("SELECT date_trunc('milliseconds', clock_timestamp())")
            h = _digest(prev, r)
            ids.append(await conn.fetchval(
                f"""INSERT INTO trade_calls ({", ".join(FIELDS)}, prev_hash, hash)
                    VALUES ({", ".join(f"${i + 1}" for i in range(len(FIELDS) + 2))}) RETURNING id""",
                *[orjson.dumps(r[k]).decode() if k in ("evidence", "critique") and r.get(k) is not None
                  else r.get(k) for k in FIELDS], prev, h,
            ))
            prev = h
    return ids


async def verify() -> tuple[int, int | None]:
    """Recompute the chain. -> (rows checked, first broken id or None)."""
    pool = await db.pool()
    prev, n = GENESIS, 0
    for r in await pool.fetch(f"SELECT id, {', '.join(FIELDS)}, prev_hash, hash FROM trade_calls ORDER BY id"):
        row = dict(r)
        for k in ("evidence", "critique"):
            if isinstance(row[k], str):
                row[k] = orjson.loads(row[k])
        if row["prev_hash"] != prev or _digest(prev, row) != row["hash"]:
            return n, r["id"]
        prev, n = row["hash"], n + 1
    return n, None

