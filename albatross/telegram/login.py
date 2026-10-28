"""Signing in to Telegram from the Setup page, in the dashboard: phone, then the code Telegram sends, then the two-step
verification password when the account has one.

One sign-in at a time (Albatross has one operator). The client in progress and the phone number live in this process's
memory only, and are dropped when the sign-in ends or after EXPIRES_S. On success the session string is saved as
TELEGRAM_SESSION (keys.py), the account is cached for the page (status.py) and the client disconnects; the listener in
ingest picks the session up within 30 s. Nothing here logs the phone number, the code, the password or the session.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import asyncpg
from telethon import TelegramClient, errors, utils

from albatross import keys
from albatross.config import settings
from albatross.telegram import client as tg
from albatross.telegram import status

log = logging.getLogger(__name__)

EXPIRES_S = 15 * 60         # a sign-in left half done this long is dropped (Telegram's codes expire sooner)
TIMEOUT_S = 60              # one step's wait on Telegram
LOGOUT_S = 30


@dataclass
class Login:
    state: str = "idle"                     # idle | code_sent | password_needed | done | failed
    error: str | None = None
    hint: str | None = None                 # the masked phone number the page shows
    phone: str | None = None
    code_hash: str | None = None
    client: TelegramClient | None = None
    started: float = 0.0


_login = Login()
_lock = asyncio.Lock()


def mask(phone: str) -> str:
    """"+15550100012" -> "+1 ••• ••• ••12": the start of the country code and the last two digits."""
    d = re.sub(r"\D", "", phone)
    cc = d[:1] if d[:1] in ("1", "7") else d[:2]
    rest = "•" * max(len(d) - len(cc) - 2, 0) + d[len(cc):][-2:]
    groups = [rest[i:i + 3] for i in range(0, len(rest), 3)]
    if len(groups) > 1 and len(groups[-1]) == 1:
        groups[-2:] = [groups[-2] + groups[-1]]
    return f"+{cc} " + " ".join(groups)


def _wait(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} seconds"
    if seconds < 2 * 3600:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds / 3600)} hours"


def why(e: BaseException) -> str:
    """A plain sentence for what went wrong."""
    if isinstance(e, errors.FloodWaitError):
        return f"Telegram asks you to wait {_wait(e.seconds)} before trying again."
    if isinstance(e, (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError)):
        return "Telegram refused this API ID and hash. Check them at my.telegram.org."
    if isinstance(e, errors.PhoneNumberInvalidError):
        return "Telegram says this phone number isn't valid."
    if isinstance(e, errors.PhoneNumberBannedError):
        return "Telegram has banned this phone number."
    if isinstance(e, errors.PhoneNumberFloodError):
        return "Too many sign-in attempts for this number. Try again later."
    if isinstance(e, errors.PhoneNumberUnoccupiedError):
        return "This number has no Telegram account. Create one in the Telegram app first."
    if isinstance(e, (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError)):
        return "That code isn't right. Check it and try again."
    if isinstance(e, errors.PhoneCodeExpiredError):
        return "That code has expired. Start again to get a new one."
    if isinstance(e, errors.PasswordHashInvalidError):
        return "That password isn't right."
    if isinstance(e, (OSError, ConnectionError, TimeoutError)):
        return "Couldn't reach Telegram. Check the internet connection and try again."
    if isinstance(e, errors.RPCError):
        return f"Telegram refused the sign-in ({e.message})."
    return "The sign-in failed. Try again."


def view() -> dict:
    return {"state": _login.state, "phone_hint": _login.hint, "error": _login.error}


async def _finish(state: str, error: str | None = None) -> dict:
    """End the sign-in in progress: disconnect, forget the number and the code's hash."""
    client = _login.client
    _login.client = _login.phone = _login.code_hash = None
    _login.state, _login.error = state, error
    if state == "idle":
        _login.hint = None
    if client is not None:
        try:
            await client.disconnect()
        except Exception as e:  # noqa: BLE001 - the sign-in is over either way
            log.info("telegram: disconnect after sign-in: %s", type(e).__name__)
    return view()


async def _expired() -> bool:
    if _login.client is not None and time.monotonic() - _login.started > EXPIRES_S:
        await _finish("failed", "The sign-in timed out. Start again.")
        return True
    return False


async def start(phone: str) -> dict:
    async with _lock:
        await _finish("idle")
        number = re.sub(r"[\s().-]", "", phone or "")
        if not re.fullmatch(r"\+?\d{7,15}", number):
            return await _finish("failed", "Enter the phone number with its country code, starting with +.")
        if not tg.ready():
            return await _finish("failed", "Save the API ID and API hash first.")
        if keys.from_env("TELEGRAM_SESSION"):
            return await _finish("failed", "TELEGRAM_SESSION is set in .env or data.keys, so a sign-in here would be "
                                           "ignored. Remove it there first.")
        number = "+" + number.lstrip("+")
        _login.hint = mask(number)
        try:
            _login.client = tg.make()
        except ValueError:
            return await _finish("failed", "The API ID must be a number. Check it at my.telegram.org.")
        try:
            async with asyncio.timeout(TIMEOUT_S):
                await _login.client.connect()
                sent = await _login.client.send_code_request(number)
        except Exception as e:  # noqa: BLE001 - every failure becomes a sentence on the page
            log.info("telegram: sending the code failed: %s", type(e).__name__)
            return await _finish("failed", why(e))
        _login.phone, _login.code_hash, _login.started = number, sent.phone_code_hash, time.monotonic()
        _login.state, _login.error = "code_sent", None
        return view()


def _out_of_step() -> dict:
    _login.error = ("Enter your two-step verification password." if _login.state == "password_needed"
                    else "Enter the code Telegram sent you." if _login.state == "code_sent"
                    else "Start again with your phone number.")
    return view()


async def code(code: str) -> dict:
    async with _lock:
        if await _expired():
            return view()
        if _login.state != "code_sent":
            return _out_of_step()
        digits = re.sub(r"\D", "", code or "")
        if not digits:
            _login.error = "Enter the code Telegram sent you."
            return view()
        try:
            async with asyncio.timeout(TIMEOUT_S):
                user = await _login.client.sign_in(phone=_login.phone, code=digits, phone_code_hash=_login.code_hash)
        except errors.SessionPasswordNeededError:
            _login.state, _login.error = "password_needed", None
            return view()
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError, errors.FloodWaitError) as e:
            _login.error = why(e)                           # the same code step, again
            return view()
        except Exception as e:  # noqa: BLE001
            log.info("telegram: the code step failed: %s", type(e).__name__)
            return await _finish("failed", why(e))
        return await _signed_in(user)


async def password(password: str) -> dict:
    async with _lock:
        if await _expired():
            return view()
        if _login.state != "password_needed":
            return _out_of_step()
        if not password:
            _login.error = "Enter your two-step verification password."
            return view()
        try:
            async with asyncio.timeout(TIMEOUT_S):
                user = await _login.client.sign_in(password=password)
        except (errors.PasswordHashInvalidError, errors.FloodWaitError) as e:
            _login.error = why(e)
            return view()
        except Exception as e:  # noqa: BLE001
            log.info("telegram: the password step failed: %s", type(e).__name__)
            return await _finish("failed", why(e))
        return await _signed_in(user)


async def _signed_in(user) -> dict:
    session = _login.client.session.save()
    try:
        await keys.save("TELEGRAM_SESSION", session)
    except (OSError, asyncpg.PostgresError) as e:
        log.warning("telegram: signed in, but the session wasn't saved: %s", type(e).__name__)
        try:
            await _login.client.log_out()                   # leave no session behind that nothing holds
        except Exception:  # noqa: BLE001
            pass
        return await _finish("failed", "Signed in, but the session couldn't be saved. Try again.")
    await status.save("starting", "Starting.", account_name=utils.get_display_name(user) or None,
                      account_username=getattr(user, "username", None))
    log.info("telegram: signed in; the session is saved")
    return await _finish("done")


async def log_out() -> bool:
    """End the session on Telegram's side, then forget it here. -> False when Telegram couldn't be reached: the session
    is forgotten all the same (a session Telegram no longer answers for would otherwise never go), and works there
    until it is ended in Telegram under Settings > Devices."""
    async with _lock:
        await _finish("idle")
        session, reached = settings.telegram_session, True
        if session and tg.ready():
            try:
                client = tg.make(session)
            except (ValueError, tg.SignedOut):              # nothing that can reach Telegram
                client = None
            if client is not None:
                try:
                    async with asyncio.timeout(LOGOUT_S):
                        await client.connect()
                        await client.log_out()              # False when Telegram had already ended it
                except tg.SIGNED_OUT:
                    pass
                except (OSError, ConnectionError, TimeoutError) as e:
                    log.info("telegram: couldn't reach Telegram to end the session: %s", type(e).__name__)
                    reached = False
                finally:
                    await client.disconnect()
        await keys.delete("TELEGRAM_SESSION")
        await status.save("off", "Not signed in.", account_name=None, account_username=None)
        log.info("telegram: signed out")
        return reached
