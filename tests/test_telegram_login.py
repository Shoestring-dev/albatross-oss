"""Signing in to Telegram from the Setup page (albatross/telegram/login.py), against a Telethon stand-in: the steps,
the errors Telegram answers with, and what is kept, saved and logged. No phone number, code or session here is real."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from telethon import errors
from telethon.tl.types import User

from albatross import keys
from albatross.config import settings
from albatross.telegram import client as tg
from albatross.telegram import login, status

PHONE = "+1 555 010 0012"           # 555-01xx: reserved for fiction
CODE = "12345"
PASSWORD = "made-up 2fa words"
ME = User(id=42, first_name="My", last_name="Account", username="me_myself")


class FakeClient:
    """send: what send_code_request does; codes / passwords: what each sign_in step answers, in turn (an exception is
    raised, anything else returned)."""

    def __init__(self, send=None, codes=(ME,), passwords=(ME,), log_out=True):
        self.send, self.codes, self.passwords, self.log_out_answer = send, list(codes), list(passwords), log_out
        self.connected, self.disconnects, self.calls, self.logged_out = False, 0, [], False
        self.session = SimpleNamespace(save=lambda: "made-up-session-string")

    async def connect(self):
        if isinstance(self.send, OSError):
            raise self.send
        self.connected = True

    async def send_code_request(self, phone):
        self.calls.append(("send", phone))
        if isinstance(self.send, BaseException):
            raise self.send
        return SimpleNamespace(phone_code_hash="made-up-hash")

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None):
        self.calls.append(("sign_in", phone, code, password, phone_code_hash))
        out = (self.passwords if password else self.codes).pop(0)
        if isinstance(out, BaseException):
            raise out
        return out

    async def log_out(self):
        if isinstance(self.log_out_answer, BaseException):
            raise self.log_out_answer
        self.logged_out = True
        return self.log_out_answer

    async def disconnect(self):
        self.disconnects += 1


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(login, "_login", login.Login())
    monkeypatch.setattr(login, "_lock", asyncio.Lock())
    monkeypatch.setattr(settings, "telegram_api_id", "12345")
    monkeypatch.setattr(settings, "telegram_api_hash", "0" * 32)
    monkeypatch.setattr(settings, "telegram_session", None)
    out = SimpleNamespace(saved={}, deleted=[], status=[], clients=[], from_env=set())

    async def save(name, value):
        out.saved[name] = value
        return {name}

    async def delete(name):
        out.deleted.append(name)
        return {name}

    async def status_save(state, text, **cols):
        out.status.append((state, text, cols))
    monkeypatch.setattr(keys, "save", save)
    monkeypatch.setattr(keys, "delete", delete)
    monkeypatch.setattr(keys, "from_env", lambda name: name in out.from_env)
    monkeypatch.setattr(status, "save", status_save)

    def use(*fakes):
        queue = list(fakes)

        def make(session=None, updates=False):
            out.clients.append(queue.pop(0))
            return out.clients[-1]
        monkeypatch.setattr(tg, "make", make)
    out.use = use
    return out


def run(coro):
    return asyncio.run(coro)


def test_phone_then_code(env):
    env.use(fake := FakeClient())
    assert run(login.start(PHONE)) == {"state": "code_sent", "phone_hint": "+1 ••• ••• ••12", "error": None}
    assert fake.calls == [("send", "+15550100012")]
    assert run(login.code("12 345")) == {"state": "done", "phone_hint": "+1 ••• ••• ••12", "error": None}
    assert fake.calls[1] == ("sign_in", "+15550100012", CODE, None, "made-up-hash")
    assert env.saved == {"TELEGRAM_SESSION": "made-up-session-string"}
    assert env.status == [("starting", "Starting.", {"account_name": "My Account", "account_username": "me_myself"})]
    assert fake.disconnects == 1
    assert login._login.phone is None and login._login.client is None and login._login.code_hash is None


def test_two_step_password(env):
    env.use(fake := FakeClient(codes=[errors.SessionPasswordNeededError(None)],
                               passwords=[errors.PasswordHashInvalidError(None), ME]))
    run(login.start(PHONE))
    assert run(login.code(CODE))["state"] == "password_needed"
    assert run(login.password("wrong")) == {"state": "password_needed", "phone_hint": "+1 ••• ••• ••12",
                                            "error": "That password isn't right."}
    assert run(login.password(PASSWORD))["state"] == "done"
    assert fake.calls[-1] == ("sign_in", None, None, PASSWORD, None)
    assert env.saved["TELEGRAM_SESSION"] == "made-up-session-string"


def test_a_wrong_code_can_be_retried_an_expired_one_cannot(env):
    env.use(FakeClient(codes=[errors.PhoneCodeInvalidError(None), errors.PhoneCodeExpiredError(None)]))
    run(login.start(PHONE))
    assert run(login.code("11111")) == {"state": "code_sent", "phone_hint": "+1 ••• ••• ••12",
                                        "error": "That code isn't right. Check it and try again."}
    out = run(login.code("22222"))
    assert out["state"] == "failed" and out["error"] == "That code has expired. Start again to get a new one."
    assert env.saved == {} and env.clients[0].disconnects == 1


@pytest.mark.parametrize("err, message", [
    (errors.FloodWaitError(None, capture=45), "Telegram asks you to wait 45 seconds before trying again."),
    (errors.FloodWaitError(None, capture=600), "Telegram asks you to wait 10 minutes before trying again."),
    (errors.FloodWaitError(None, capture=36000), "Telegram asks you to wait 10 hours before trying again."),
    (errors.ApiIdInvalidError(None), "Telegram refused this API ID and hash. Check them at my.telegram.org."),
    (errors.PhoneNumberInvalidError(None), "Telegram says this phone number isn't valid."),
    (errors.PhoneNumberBannedError(None), "Telegram has banned this phone number."),
    (OSError(), "Couldn't reach Telegram. Check the internet connection and try again."),
])
def test_sending_the_code_fails_plainly(env, err, message):
    env.use(fake := FakeClient(send=err))
    assert run(login.start(PHONE)) == {"state": "failed", "phone_hint": "+1 ••• ••• ••12", "error": message}
    assert fake.disconnects == 1 and login._login.client is None


def test_a_flood_wait_on_the_code_keeps_the_step(env):
    env.use(FakeClient(codes=[errors.FloodWaitError(None, capture=30), ME]))
    run(login.start(PHONE))
    out = run(login.code(CODE))
    assert out["state"] == "code_sent" and "30 seconds" in out["error"]
    assert run(login.code(CODE))["state"] == "done"


@pytest.mark.parametrize("phone", ["", "12", "call me", "+1 555 010 00123456789"])
def test_a_phone_number_that_isnt_one(env, phone):
    env.use()
    out = run(login.start(phone))
    assert out["state"] == "failed" and out["error"].startswith("Enter the phone number with its country code")
    assert env.clients == []


def test_api_keys_first(env, monkeypatch):
    env.use()
    monkeypatch.setattr(settings, "telegram_api_hash", None)
    assert run(login.start(PHONE))["error"] == "Save the API ID and API hash first."
    monkeypatch.setattr(settings, "telegram_api_hash", "0" * 32)
    monkeypatch.setattr(settings, "telegram_api_id", "not-a-number")
    monkeypatch.setattr(tg, "make", lambda *a, **k: int("not-a-number"))
    assert run(login.start(PHONE))["error"] == "The API ID must be a number. Check it at my.telegram.org."


def test_a_session_set_in_the_environment_wins(env):
    env.use()
    env.from_env.add("TELEGRAM_SESSION")
    out = run(login.start(PHONE))
    assert out["state"] == "failed" and "TELEGRAM_SESSION is set in .env or data.keys" in out["error"]


def test_steps_out_of_order(env):
    env.use(FakeClient(codes=[errors.SessionPasswordNeededError(None)]))
    assert run(login.code(CODE))["error"] == "Start again with your phone number."
    assert run(login.password(PASSWORD))["error"] == "Start again with your phone number."
    run(login.start(PHONE))
    assert run(login.password(PASSWORD)) == {"state": "code_sent", "phone_hint": "+1 ••• ••• ••12",
                                             "error": "Enter the code Telegram sent you."}
    run(login.code(CODE))
    assert run(login.code(CODE))["error"] == "Enter your two-step verification password."


def test_a_new_start_drops_the_sign_in_in_progress(env):
    env.use(first := FakeClient(), FakeClient())
    run(login.start(PHONE))
    run(login.start("+44 7700 900123"))                 # Ofcom's drama range
    assert first.disconnects == 1 and login.view()["phone_hint"] == "+44 ••• ••• ••23"


def test_a_sign_in_left_half_done_expires(env, monkeypatch):
    env.use(fake := FakeClient())
    now = [1000.0]
    monkeypatch.setattr(login.time, "monotonic", lambda: now[0])
    run(login.start(PHONE))
    now[0] += login.EXPIRES_S + 1
    out = run(login.code(CODE))
    assert out["state"] == "failed" and out["error"] == "The sign-in timed out. Start again."
    assert fake.disconnects == 1 and env.saved == {}


def test_a_session_that_cannot_be_saved_is_logged_out(env, monkeypatch):
    import asyncpg

    async def broken(name, value):
        raise asyncpg.PostgresConnectionError("down")
    monkeypatch.setattr(keys, "save", broken)
    env.use(fake := FakeClient())
    run(login.start(PHONE))
    out = run(login.code(CODE))
    assert out == {"state": "failed", "phone_hint": "+1 ••• ••• ••12",
                   "error": "Signed in, but the session couldn't be saved. Try again."}
    assert fake.logged_out


def test_nothing_secret_is_logged(env, caplog):
    env.use(FakeClient(codes=[errors.SessionPasswordNeededError(None)], passwords=[OSError("boom"), ]))
    with caplog.at_level(logging.DEBUG):
        run(login.start(PHONE))
        run(login.code(CODE))
        run(login.password(PASSWORD))
    for secret in ("5550100012", "555 010", CODE, PASSWORD, "made-up-session-string", "made-up-hash"):
        assert secret not in caplog.text


def test_mask():
    assert login.mask("+15550100012") == "+1 ••• ••• ••12"
    assert login.mask("+447700900123") == "+44 ••• ••• ••23"
    assert login.mask("+79990001122") == "+7 ••• ••• ••22"


# ─── signing out ────────────────────────────────────────────────────────────
def test_log_out_ends_the_session_at_telegram_then_forgets_it(env, monkeypatch):
    monkeypatch.setattr(settings, "telegram_session", "made-up-session-string")
    env.use(fake := FakeClient())
    assert run(login.log_out()) is True
    assert fake.logged_out and fake.disconnects >= 1
    assert env.deleted == ["TELEGRAM_SESSION"]
    assert env.status == [("off", "Not signed in.", {"account_name": None, "account_username": None})]


def test_log_out_of_a_session_telegram_already_ended(env, monkeypatch):
    monkeypatch.setattr(settings, "telegram_session", "made-up-session-string")
    env.use(FakeClient(log_out=False))                  # Telethon answers False on an RPC error
    run(login.log_out())
    assert env.deleted == ["TELEGRAM_SESSION"]


def test_log_out_forgets_the_session_even_when_telegram_cannot_be_reached(env, monkeypatch):
    monkeypatch.setattr(settings, "telegram_session", "made-up-session-string")
    env.use(fake := FakeClient(send=OSError()))         # connect fails
    assert run(login.log_out()) is False
    assert env.deleted == ["TELEGRAM_SESSION"] and fake.disconnects == 1


def test_log_out_of_a_session_telegram_never_answers_for(env, monkeypatch):
    # A session whose key Telegram has dropped: requests go unanswered, so log_out() would wait forever.
    monkeypatch.setattr(settings, "telegram_session", "made-up-session-string")
    monkeypatch.setattr(login, "LOGOUT_S", 0.05)

    class Silent(FakeClient):
        async def log_out(self):
            await asyncio.Event().wait()
    env.use(Silent())
    assert run(login.log_out()) is False and env.deleted == ["TELEGRAM_SESSION"]


def test_log_out_drops_a_sign_in_in_progress(env, monkeypatch):
    env.use(pending := FakeClient())
    run(login.start(PHONE))
    run(login.log_out())                                # no session saved: nothing to end at Telegram
    assert pending.disconnects == 1 and login.view() == {"state": "idle", "phone_hint": None, "error": None}
    assert env.deleted == ["TELEGRAM_SESSION"]
