"""The Telethon client the login (dashboard) and the listener (ingest) build, and the chats picked on the Setup page.

The session string is as powerful as the account's own login: it is never logged, and only keys.py stores it.
"""

import logging
import struct

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat

from albatross.config import settings

# Telethon's own log stays at warnings and above: its debug lines can carry message contents.
logging.getLogger("telethon").setLevel(logging.WARNING)


class SignedOut(Exception):
    """The saved session no longer signs in (get_me() answers None)."""


# What Telegram answers once a session is no longer valid: logged out from another device, terminated under
# Settings > Devices, or the account deleted or banned. AuthKeyError covers a session used from two places at once.
SIGNED_OUT = (SignedOut, errors.UnauthorizedError, errors.AuthKeyError, errors.AuthKeyNotFound)


def ready() -> bool:
    return bool(settings.telegram_api_id and settings.telegram_api_hash)


def make(session: str | None = None, updates: bool = False) -> TelegramClient:
    """A client on the saved API ID and hash; Telegram's Devices list shows it as Albatross. session None starts a new
    sign-in. updates: receive new messages and keep reconnecting (the listener); the dashboard's clients give up
    quickly instead, since a page is waiting. ValueError when the API ID isn't a number, SignedOut when the session
    string doesn't decode."""
    api_id = int((settings.telegram_api_id or "").strip())
    try:
        string = StringSession(session or "")
    except (ValueError, struct.error):
        raise SignedOut from None
    return TelegramClient(string, api_id, (settings.telegram_api_hash or "").strip(), receive_updates=updates,
                          connection_retries=5 if updates else 1, request_retries=5 if updates else 2,
                          device_model="Albatross", app_version="Albatross")


def watch_ids() -> list[int]:
    """TELEGRAM_WATCH as chat ids, in order, without repeats; a part that isn't a number is ignored."""
    out = []
    for part in settings.telegram_watch.split(","):
        try:
            out.append(int(part.strip()))
        except ValueError:
            continue
    return list(dict.fromkeys(out))


def chat_kind(entity) -> str | None:
    """group | supergroup | channel for a chat the listener can read; None for people and bots, a chat you've left, a
    basic group that became a supergroup, and a channel's direct messages."""
    if isinstance(entity, Chat):
        return None if entity.left or entity.deactivated or entity.migrated_to else "group"
    if isinstance(entity, Channel):
        if entity.left or entity.monoforum:
            return None
        return "supergroup" if entity.megagroup or entity.gigagroup else "channel"
    return None
