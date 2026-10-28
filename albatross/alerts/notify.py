"""Alert notifications to Telegram through Albatross's own password-gated bot (albatross/alerts/bot.py):
every chat that sent the password (alert_subscribers), plus TELEGRAM_ALERT_CHAT_ID if set. Off until
TELEGRAM_ALERT_BOT_TOKEN is set (data.keys).

At most one message per alert key per hour. Each message states the key's current state, so an alert
that flaps open/resolved every few minutes sends one "open", then one "resolved" an hour later if it
has stayed clear, not a stream. An alert that opens and clears before it was ever announced sends
nothing. A failed send is retried on the next check.
"""

import logging
from datetime import datetime, timedelta

import httpx

from albatross import db
from albatross.alerts.rules import dur
from albatross.config import settings
from albatross.http import client

log = logging.getLogger(__name__)

RATE = timedelta(hours=1)
EMOJI = {"critical": "🔴", "warning": "🟠", "resolved": "✅"}

# HARD RULE: TELEGRAM_ALERT_BOT_TOKEN is Albatross's own bot, never a bot another program uses, which may receive
# its updates by long polling. Only albatross/alerts/bot.py reads updates, and it refuses to
# run on a shared bot. Nothing here may set or delete a webhook; tests/test_alerts.py checks both.
SEND_MESSAGE = "https://api.telegram.org/bot{token}/sendMessage"


def plan(open_: dict[str, dict], resolved: dict[str, dict], notices: dict[str, dict],
         now: datetime) -> list[tuple[str, str, dict]]:
    """-> (key, state, alert row) messages to send now. open_: open alerts by key; resolved: each
    key's latest resolved incident; notices: key -> {state, sent_at} of the last message sent."""
    out = []
    for key in sorted(set(open_) | {k for k, n in notices.items() if n["state"] == "open"}):
        state = "open" if key in open_ else "resolved"
        n = notices.get(key)
        if n and n["state"] == state:
            continue                        # already said
        if state == "resolved" and key not in resolved:
            continue
        if n and now - n["sent_at"] < RATE:
            continue                        # the next check after the hour passes will say it
        out.append((key, state, open_[key] if state == "open" else resolved[key]))
    return out


def message(state: str, a: dict, now: datetime) -> str:
    """Two lines: severity emoji, the condition and how long; then the dashboard link."""
    if state == "open":
        line = f"{EMOJI[a['severity']]} {a['title']} · for {dur(now - a['since'])}"
    else:
        line = f"{EMOJI['resolved']} Resolved: {a['title']} · lasted {dur(a['resolved_at'] - a['since'])}"
    return f"{line}\n{settings.alert_dashboard_url}"


class Telegram:
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    async def send(self, text: str) -> None:
        r = await client().post(SEND_MESSAGE.format(token=self.token),
                                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True})
        r.raise_for_status()


async def channels() -> list:
    token = settings.telegram_alert_bot_token
    if not token:
        return []
    pool = await db.pool()
    chats = [str(r["chat_id"]) for r in await pool.fetch("SELECT chat_id FROM alert_subscribers ORDER BY subscribed_at")]
    if settings.telegram_alert_chat_id and settings.telegram_alert_chat_id not in chats:
        chats.append(settings.telegram_alert_chat_id)
    return [Telegram(token, c) for c in chats]


def _why(e: Exception) -> str:
    # Never str(e): an httpx error's text includes the URL, and Telegram's URL contains the bot token.
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


async def send(chans: list, state: str, a: dict, now: datetime) -> bool:
    """True when at least one channel took the message."""
    text = message(state, a, now)
    ok = False
    for ch in chans:
        try:
            await ch.send(text)
            ok = True
        except Exception as e:  # noqa: BLE001 - a failed send is retried on the next check
            log.warning("alert %s via %s failed: %s", a["key"], ch.name, _why(e))
    return ok
