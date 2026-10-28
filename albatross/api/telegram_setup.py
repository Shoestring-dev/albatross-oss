"""Setup › Telegram: sign in with your own Telegram account, pick the groups and channels the listener reads, and see
how it is doing (albatross/telegram). Every route is behind the Setup guard (api/guard.py).

The page reads the listener's state from the database (telegram/status.py): the listener runs in ingest, not here.
GET never connects to Telegram; the chat list does, with the saved session, and is kept for CHATS_CACHE_S.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import NoReturn

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from telethon import errors

from albatross import db, keys
from albatross.api import guard
from albatross.api.ingest import LISTENER
from albatross.config import settings
from albatross.telegram import client as tg
from albatross.telegram import login, status

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup/telegram")

CHATS_CACHE_S = 60
_chats: tuple[float, str, list[dict]] | None = None         # (when, the session it was read with, chats)


class Phone(BaseModel):
    phone: str = Field(max_length=40)


class Code(BaseModel):
    code: str = Field(max_length=20)


class Password(BaseModel):
    password: str = Field(max_length=512)


class Watch(BaseModel):
    ids: list[int] = Field(max_length=500)


def signed_in(row: dict | None) -> bool:
    return bool(settings.telegram_session) and (row or {}).get("state") != "signed_out"


def listener_view(row: dict | None, watch: list[int], now: datetime) -> dict:
    """running, and the sentence the page shows. Pure."""
    state = (row or {}).get("state")
    fresh = row is not None and now - row["updated_at"] <= status.STALE
    if not signed_in(row):
        text = (row["status"] if state == "signed_out" else
                "Not signed in." if tg.ready() else "Add the API ID and API hash, then sign in.")
    elif not tg.ready():
        text = "Add the API ID and API hash."
    elif not watch:
        text = "Pick the chats to watch."
    elif state is None or state == "off":
        text = "Starting."
    elif state in status.LIVE and not fresh:
        text = "Not running. Check that the ingest service is up."
    else:
        text = row["status"]
    running = signed_in(row) and tg.ready() and bool(watch) and state == "listening" and fresh
    return {"running": running, "status": text,
            "last_message_at": row["last_message_at"].isoformat() if row and row["last_message_at"] else None}


async def _calls_today() -> int:
    pool = await db.pool()
    try:
        return await pool.fetchval(
            "SELECT count(*) FROM micro_calls WHERE source = $1 AND called_at >= date_trunc('day', now())", LISTENER)
    except asyncpg.UndefinedColumnError:            # before migration 046
        return 0


def _titles(row: dict | None) -> dict[int, str]:
    out = {c["id"]: c["title"] for c in (_chats[2] if _chats else ())}
    return out | (row or {}).get("titles", {})


def _watch_list(ids: list[int], titles: dict[int, str]) -> list[dict]:
    return [{"id": i, "title": titles.get(i) or f"Chat {i}"} for i in ids]


@router.get("", dependencies=[Depends(guard.read_guard)])
async def overview() -> dict:
    row = await status.read()
    watch = tg.watch_ids()
    account = None
    if signed_in(row) and row and (row["account_name"] or row["account_username"]):
        account = {"name": row["account_name"] or "", "username": row["account_username"]}
    return {
        "api_id_set": bool(settings.telegram_api_id), "api_hash_set": bool(settings.telegram_api_hash),
        "signed_in": signed_in(row), "account": account, "login": login.view(),
        "watch": _watch_list(watch, _titles(row)),
        "listener": {**listener_view(row, watch, datetime.now(UTC)), "calls_today": await _calls_today()},
    }


@router.post("/start", dependencies=[Depends(guard.write_guard)])
async def start(body: Phone) -> dict:
    return await login.start(body.phone)


@router.post("/code", dependencies=[Depends(guard.write_guard)])
async def code(body: Code) -> dict:
    return await login.code(body.code)


@router.post("/password", dependencies=[Depends(guard.write_guard)])
async def password(body: Password) -> dict:
    return await login.password(body.password)


async def chat_list() -> list[dict]:
    """The groups and channels the account is in, newest activity first: {id, title, kind, members}."""
    global _chats
    session = settings.telegram_session
    if not tg.ready():
        raise HTTPException(409, "Add the API ID and API hash first.")
    if not session:
        raise HTTPException(409, "Sign in to Telegram first.")
    if _chats and _chats[1] == session and time.monotonic() - _chats[0] < CHATS_CACHE_S:
        return _chats[2]
    try:
        client = tg.make(session)
    except ValueError:
        raise HTTPException(409, "The API ID must be a number. Check it at my.telegram.org.") from None
    except tg.SignedOut as e:
        await _signed_out(e)
    rows = []
    try:
        async with asyncio.timeout(login.TIMEOUT_S):
            await client.connect()
            if await client.get_me() is None:
                raise tg.SignedOut
            async for d in client.iter_dialogs():
                if kind := tg.chat_kind(d.entity):
                    rows.append({"id": d.id, "title": d.title or f"Chat {d.id}", "kind": kind,
                                 "members": getattr(d.entity, "participants_count", None)})
    except tg.SIGNED_OUT as e:
        await _signed_out(e)
    except (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError, errors.FloodWaitError) as e:
        raise HTTPException(429 if isinstance(e, errors.FloodWaitError) else 409, login.why(e)) from None
    except (OSError, ConnectionError, TimeoutError, errors.RPCError) as e:
        log.info("telegram: listing chats failed: %s", type(e).__name__)
        raise HTTPException(502, "Couldn't get your chats from Telegram. Try again.") from None
    finally:
        await client.disconnect()
    _chats = (time.monotonic(), session, rows)
    return rows


async def _signed_out(e: Exception) -> NoReturn:
    log.warning("telegram: listing chats: Telegram ended the session (%s)", type(e).__name__)
    await status.save("signed_out", status.SIGNED_OUT_TEXT)
    raise HTTPException(409, status.SIGNED_OUT_TEXT) from None


@router.get("/chats", dependencies=[Depends(guard.read_guard)])
async def chats() -> list[dict]:
    watch = set(tg.watch_ids())
    return [{**c, "watched": c["id"] in watch} for c in await chat_list()]


@router.put("/watch", dependencies=[Depends(guard.write_guard)])
async def put_watch(body: Watch) -> dict:
    if keys.from_env("TELEGRAM_WATCH"):
        raise HTTPException(409, "TELEGRAM_WATCH is set in .env or data.keys, which wins over this list. "
                                 "Remove it there first.")
    ids = list(dict.fromkeys(body.ids))
    titles: dict[int, str] = {}
    if ids:
        known = {c["id"]: c["title"] for c in await chat_list()}
        if any(i not in known for i in ids):
            raise HTTPException(400, "Pick from the groups and channels you're in.")
        titles = {i: known[i] for i in ids}
    if ids != tg.watch_ids():
        if ids:
            await keys.save("TELEGRAM_WATCH", ",".join(map(str, ids)))
            await status.save("starting", "Starting.", titles=titles)       # the listener restarts within 30 s
        else:
            await keys.delete("TELEGRAM_WATCH")
    return {"ok": True, "watch": _watch_list(ids, titles)}


@router.delete("", dependencies=[Depends(guard.write_guard)])
async def sign_out() -> dict:
    global _chats
    reached = await login.log_out()
    _chats = None
    if not reached:
        raise HTTPException(502, "Signed out here, but Telegram couldn't be reached to end the session. End it in "
                                 "Telegram under Settings > Devices.")
    return {"ok": True}
