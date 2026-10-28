"""The Telegram listener, a long-lived task in ingest (albatross/__main__.py restarts it when one of KEYS changes on the
Setup page). It connects as the operator's own account with the saved session, reads new messages in the watched chats
only, and stores every token address they name (parse.py) as a call through the call feed's own storage
(api/ingest.store, source "telegram"), so each call gets the same ledger entry, dossier checklist and 24-hour grade.

- One call per chat and token a day (REPEAT): a group re-posting its coin ("2x! CA: ...") within a day of calling it
  hasn't made a new call. The ledger logs every stored call and grades it from that moment, so a repeat would read as a
  second call bought higher; the caller stats and the paper books count each chat's first call only anyway.
- Reconnects with a backoff (5 s doubling to 5 minutes). When Telegram has ended the session, it stops and leaves that
  status for the Setup page; it starts again only when a key changes (a new sign-in).
- The heartbeat (status.py) carries the newest message seen in a watched chat; the page counts today's calls itself.
- Never logs message text, phone numbers or the session: errors are logged by their type only.
"""

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta

from telethon import errors, events, utils
from telethon.tl.types import MessageEntityTextUrl

from albatross.api import ingest
from albatross.config import settings
from albatross.telegram import client as tg
from albatross.telegram import parse, status

log = logging.getLogger(__name__)

KEYS = {"TELEGRAM_SESSION", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_WATCH"}
SOURCE = ingest.LISTENER
REPEAT = timedelta(hours=24)            # the ledger's horizon for a call (micro/ledger_feed.py HORIZON)
BACKOFF_S, BACKOFF_MAX_S = 5, 300
STEADY_S = 300                          # connected this long: the next drop starts the backoff over
SETUP_S = 180                           # connecting, signing in and reading the chat list; Telegram can stay silent


def src_id(chat_id: int, message_id: int, address: str) -> int:
    """A stable micro_calls id per (chat, message, token): a message seen twice is stored once. 2^52 and up, far above a
    feed's own ids, and below 2^53 so the page's JavaScript reads it exactly."""
    h = hashlib.sha256(f"telegram:{chat_id}:{message_id}:{address}".encode()).digest()
    return 2 ** 52 + int.from_bytes(h[:8], "big") % 2 ** 52


def urls(message) -> list[str]:
    """Links the text doesn't show: a word linked to a URL, and the buttons under the message."""
    out = [e.url for e in message.entities or () if isinstance(e, MessageEntityTextUrl)]
    for row in getattr(message.reply_markup, "rows", None) or ():
        for b in row.buttons:
            # A URL button: KeyboardButtonUrl(url) in older layers, KeyboardInlineButton(type=InlineButtonTypeUrl(url))
            # in newer ones.
            url = getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", None)
            if isinstance(url, str):
                out.append(url)
    return out


def caller(sender) -> str | None:
    """@username when there is one, else the display name (a channel posting as itself is its own caller)."""
    if sender is None:
        return None
    if getattr(sender, "username", None):
        return f"@{sender.username}"
    return utils.get_display_name(sender) or None


def calls(message, found: list[parse.Found], chat_id: int, title: str | None, who: str | None) -> list[ingest.CallIn]:
    """The calls one message makes: one per token it names (found = parse.addresses of it)."""
    return [ingest.CallIn(src_id=src_id(chat_id, message.id, f.address), chain=f.chain, address=f.address,
                          group_id=chat_id, group_name=title, caller=who, intent="mention",
                          called_at=message.date.timestamp())
            for f in found]


class Listener:
    def __init__(self, client, watch: set[int]):
        self.client, self.watch = client, watch
        self.titles: dict[int, str] = {}
        self.text = "Listening."
        self.last_message_at: datetime | None = None

    async def on_message(self, event) -> None:
        if event.chat_id not in self.watch:
            return
        msg = event.message
        self.last_message_at = max(self.last_message_at or msg.date, msg.date)
        try:
            await self.store(msg, event.chat_id)
        except Exception as e:  # noqa: BLE001 - Telethon would log the message with the error
            log.warning("telegram listener: a message wasn't stored: %s", type(e).__name__)

    async def store(self, msg, chat_id: int) -> int:
        """-> the calls stored (a repeat isn't)."""
        found = parse.addresses(msg.message, urls(msg))
        if not found:
            return 0
        title = getattr(msg.chat, "title", None) or self.titles.get(chat_id)
        try:
            sender = await msg.get_sender()
        except (errors.RPCError, OSError, ConnectionError, ValueError):
            sender = None
        who = caller(sender) or (caller(msg.chat) if msg.post else None)       # a channel posts as itself
        return (await ingest.store(SOURCE, calls(msg, found, chat_id, title, who), repeats=REPEAT))["calls"]

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(status.HEARTBEAT_S)
            await status.beat(self.text, self.last_message_at)

    async def run(self) -> None:
        """Connect, listen until Telegram disconnects us, then return (or raise)."""
        async with asyncio.timeout(SETUP_S):
            await self.client.connect()
            me = await self.client.get_me()
            if me is None:
                raise tg.SignedOut
            async for d in self.client.iter_dialogs():    # also fills the cache Telethon needs for channel updates
                if d.id in self.watch:
                    self.titles[d.id] = d.title or d.name
        missing = len(self.watch) - len(self.titles)
        self.text = f"Listening to {len(self.titles)} {'chat' if len(self.titles) == 1 else 'chats'}."
        if missing:
            self.text += f" {missing} picked {'chat is' if missing == 1 else 'chats are'} no longer in your chat list."
        self.client.add_event_handler(self.on_message, events.NewMessage())
        await status.save("listening", self.text, account_name=utils.get_display_name(me) or None,
                          account_username=me.username, titles=self.titles)
        log.info("telegram listener: listening to %d chats", len(self.titles))
        beat = asyncio.create_task(self.heartbeat())
        try:
            await self.client.run_until_disconnected()
        finally:
            beat.cancel()
            await asyncio.wait([beat])             # no heartbeat lands after the state the caller writes next


async def run_forever() -> None:
    session, watch = settings.telegram_session, set(tg.watch_ids())
    if not (session and tg.ready() and watch):
        log.info("telegram listener: not signed in or no chats picked, not listening")
        return
    backoff = BACKOFF_S
    while True:
        started = time.monotonic()
        try:
            client = tg.make(session, updates=True)
        except ValueError:
            await status.save("error", "The API ID must be a number. Check it on the Setup page.")
            return
        except tg.SignedOut:
            log.warning("telegram listener: the saved session doesn't decode; stopped")
            await status.save("signed_out", status.SIGNED_OUT_TEXT)
            return
        await status.save("connecting", "Connecting.")
        try:
            await Listener(client, watch).run()
            why = "Telegram closed the connection"
        except tg.SIGNED_OUT as e:
            log.warning("telegram listener: Telegram ended the session (%s); stopped", type(e).__name__)
            await status.save("signed_out", status.SIGNED_OUT_TEXT)
            return
        except (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError) as e:
            log.warning("telegram listener: %s; stopped", type(e).__name__)
            await status.save("error", "Telegram refused the API ID and hash. Check them on the Setup page.")
            return
        except errors.FloodWaitError as e:
            why, backoff = "Telegram asked to wait", max(backoff, e.seconds)    # never sooner than it asks
        except Exception as e:  # noqa: BLE001 - network trouble or a Telegram hiccup: back off and reconnect
            why = type(e).__name__
        finally:
            await client.disconnect()
        if time.monotonic() - started > STEADY_S:
            backoff = BACKOFF_S
        log.warning("telegram listener: %s; reconnecting in %d s", why, backoff)
        await status.save("reconnecting", f"Connection lost. Trying again in {_seconds(backoff)}.")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)


def _seconds(s: int) -> str:
    return f"{s} seconds" if s < 120 else f"{round(s / 60)} minutes"
