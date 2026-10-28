"""The listener's state for the Setup page. The listener runs in ingest and the page in the dashboard, so the state
goes through table telegram_listener (migration 046): one row, whose updated_at is the listener's heartbeat.

state:
  starting      signed in, or the chats changed: written by the dashboard until the listener takes over
  connecting    the listener, while it runs (a heartbeat every HEARTBEAT_S)
  listening     the same
  reconnecting  the same
  signed_out    Telegram ended the session; the listener stopped until the next sign-in
  error         the listener stopped on something the operator must fix (the API ID and hash)
  off           signed out from the Setup page
The account (name, @username) is cached here at sign-in, so the page never connects to Telegram to show it.
"""

import logging
from datetime import datetime, timedelta

import asyncpg
import orjson

from albatross import db

log = logging.getLogger(__name__)

LIVE = {"starting", "connecting", "listening", "reconnecting"}
HEARTBEAT_S = 30
STALE = timedelta(minutes=3)            # a live state this old: the listener isn't running
SIGNED_OUT_TEXT = "Telegram signed this session out; sign in again."
COLS = ("account_name", "account_username", "titles", "last_message_at")


async def save(state: str, status: str, **cols) -> None:
    """Set the state and the sentence the page shows, and any of COLS (titles: {chat id: title}). A database error is
    logged, not raised: a status that can't be written must not stop a sign-in or the listener."""
    if not set(cols) <= set(COLS):
        raise ValueError(f"unknown columns {sorted(set(cols) - set(COLS))}")
    names = ["state", "status", *cols]
    values = [state, status, *(orjson.dumps({str(k): t for k, t in v.items()}).decode() if k == "titles" else v
                               for k, v in cols.items())]
    try:
        pool = await db.pool()
        await pool.execute(
            f"""INSERT INTO telegram_listener (id, {", ".join(names)}, updated_at)
                VALUES (1, {", ".join(f"${i + 1}" for i in range(len(names)))}, now())
                ON CONFLICT (id) DO UPDATE SET {", ".join(f"{n} = EXCLUDED.{n}" for n in names)}, updated_at = now()""",
            *values)
    except (OSError, asyncpg.PostgresError) as e:
        log.warning("telegram: listener status not saved: %s", type(e).__name__)


async def beat(text: str, last_message_at: datetime | None) -> None:
    """The listener's heartbeat while it listens, with the newest message it has seen in a watched chat. It restates
    the state, since the dashboard writes "starting" when the chats change and the listener may not restart."""
    try:
        pool = await db.pool()
        await pool.execute("""UPDATE telegram_listener SET state = 'listening', status = $1, updated_at = now(),
                                     last_message_at = greatest(last_message_at, $2) WHERE id = 1""",
                           text, last_message_at)
    except (OSError, asyncpg.PostgresError) as e:
        log.warning("telegram: heartbeat not saved: %s", type(e).__name__)


async def read() -> dict | None:
    pool = await db.pool()
    try:
        r = await pool.fetchrow("SELECT * FROM telegram_listener WHERE id = 1")
    except asyncpg.UndefinedTableError:         # the dashboard can start before ingest has run the migrations
        return None
    if r is None:
        return None
    out = dict(r)
    out["titles"] = {int(k): t for k, t in orjson.loads(out["titles"] or "{}").items()}
    return out
