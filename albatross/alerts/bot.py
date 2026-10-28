"""The alerts bot: Albatross's own Telegram bot (TELEGRAM_ALERT_BOT_TOKEN), password-gated.

Anyone can find a bot by its username, so alerts go only to chats that have sent the password
(TELEGRAM_ALERT_PASSWORD_HASH in data.keys). /start asks for it; the right password subscribes the chat
(alert_subscribers) and the message holding it is deleted from the chat; five wrong tries lock that chat
out for an hour; /stop unsubscribes. Private chats only: the bot ignores groups it is added to.

It long-polls getUpdates, which is only safe because the token is Albatross's alone. Polling a token that
another program also polls steals that program's updates, so run_forever() asks getMe first and refuses any bot
listed in TELEGRAM_SHARED_BOTS.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets

import httpx

from albatross import db
from albatross.config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
# The Setup page names that restart the bot when they change (albatross/__main__.py).
KEYS = {"TELEGRAM_ALERT_BOT_TOKEN", "TELEGRAM_ALERT_PASSWORD_HASH", "TELEGRAM_SHARED_BOTS"}


def shared_bots() -> set[str]:
    return {b.strip().lstrip("@").lower() for b in settings.telegram_shared_bots.split(",") if b.strip()}   # never poll these
MAX_TRIES, LOCKOUT_S = 5, 3600
ITERATIONS = 200_000

REPLY = {
    "welcome": "🔒 This is Albatross's private alerts bot. Send the password to receive alerts here.",
    "ok": "✅ Subscribed. Albatross will message you here when something breaks: failing jobs, stale data, "
          "OpenRouter credit, ledger integrity. Send /stop to stop.",
    "wrong": "That isn't the password.",
    "locked": "Too many wrong tries. Try again in an hour.",
    "already": "You're subscribed. Send /stop to stop alerts.",
    "stopped": "Stopped. Send /start and the password to subscribe again.",
    "not_subscribed": "You're not subscribed. Send /start to begin.",
}


def hash_password(password: str, salt: str | None = None) -> str:
    """-> "pbkdf2:<iterations>:<salt>:<hex>", the form stored in data.keys (no "$": Compose would read
    it as a variable when it loads the file)."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2:{ITERATIONS}:{salt}:{digest}"


def check_password(password: str, stored: str | None) -> bool:
    try:
        algo, iterations, salt, digest = (stored or "").split(":")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.strip().encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(got, digest)


class Gate:
    """Wrong-password limiter per chat: MAX_TRIES misses inside LOCKOUT_S lock the chat out."""

    def __init__(self):
        self.misses: dict[int, list[float]] = {}

    def _recent(self, chat: int, now: float) -> list[float]:
        self.misses[chat] = [t for t in self.misses.get(chat, []) if now - t < LOCKOUT_S]
        return self.misses[chat]

    def locked(self, chat: int, now: float) -> bool:
        return len(self._recent(chat, now)) >= MAX_TRIES

    def miss(self, chat: int, now: float) -> None:
        self._recent(chat, now).append(now)


def decide(text: str, subscribed: bool, locked: bool, password_ok: bool) -> str:
    """-> the REPLY key for a private message."""
    cmd = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
    if cmd == "/start":
        return "already" if subscribed else "welcome"
    if cmd == "/stop":
        return "stopped" if subscribed else "not_subscribed"
    if subscribed:
        return "already"
    if locked:
        return "locked"
    return "ok" if password_ok else "wrong"


async def _call(http: httpx.AsyncClient, token: str, method: str, **payload) -> dict:
    r = await http.post(API.format(token=token, method=method), json=payload)
    r.raise_for_status()
    return r.json().get("result")


async def handle(http: httpx.AsyncClient, token: str, update: dict, gate: Gate, now: float) -> None:
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private" or "text" not in msg:
        return
    chat_id, text = chat["id"], msg["text"]
    pool = await db.pool()
    subscribed = bool(await pool.fetchval("SELECT 1 FROM alert_subscribers WHERE chat_id = $1", chat_id))
    is_command = text.strip().startswith("/")
    locked = gate.locked(chat_id, now)
    ok = not is_command and not subscribed and not locked and check_password(text, settings.telegram_alert_password_hash)
    key = decide(text, subscribed, locked, ok)
    if key == "ok":
        await pool.execute(
            """INSERT INTO alert_subscribers (chat_id, username) VALUES ($1, $2)
               ON CONFLICT (chat_id) DO NOTHING""", chat_id, (msg.get("from") or {}).get("username"))
        log.info("alerts bot: chat subscribed")
    elif key == "stopped":
        await pool.execute("DELETE FROM alert_subscribers WHERE chat_id = $1", chat_id)
        log.info("alerts bot: chat unsubscribed")
    elif key == "wrong":
        gate.miss(chat_id, now)
    if not is_command and not subscribed:
        # A password attempt, right or wrong: don't leave it sitting in the chat.
        try:
            await _call(http, token, "deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
        except httpx.HTTPError:
            pass
    await _call(http, token, "sendMessage", chat_id=chat_id, text=REPLY[key])


def _why(e: Exception) -> str:
    # Never str(e): an httpx error's text includes the URL, and the URL contains the bot token.
    return f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__


async def run_forever() -> None:
    token = settings.telegram_alert_bot_token
    if not token or not settings.telegram_alert_password_hash:
        log.info("alerts bot: token or password hash not set, not polling")
        return
    gate, offset, backoff = Gate(), None, 5
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(timeout=40) as http:
        while True:
            try:
                me = await _call(http, token, "getMe")
                break
            except httpx.HTTPError as e:
                log.warning("alerts bot: getMe failed: %s", _why(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
        if (me.get("username") or "").lower() in shared_bots():
            log.error("alerts bot: this token is @%s, which another program polls; refusing to poll", me["username"])
            return
        log.info("alerts bot: polling as @%s", me.get("username"))
        backoff = 5
        while True:
            try:
                params = {"timeout": 25, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                r = await http.get(API.format(token=token, method="getUpdates"), params=params)
                r.raise_for_status()
                for u in r.json().get("result", []):
                    offset = u["update_id"] + 1
                    try:
                        await handle(http, token, u, gate, loop.time())
                    except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                        log.warning("alerts bot: update failed: %s", _why(e))
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep polling through network trouble
                log.warning("alerts bot: poll failed: %s", _why(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
