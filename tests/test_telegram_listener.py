"""The Telegram listener (albatross/telegram/listener.py): message -> calls, the store it goes through, the repeat rule
(api/ingest.store), reconnects, a session Telegram ended, and cancellation. Telethon is never connected: the client is
a stand-in. Chat ids, names and addresses are made up."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from telethon import errors
from telethon.tl.types import (Channel, ChatPhotoEmpty, InlineButtonTypeCallback, InlineButtonTypeUrl,
                               KeyboardInlineButton, MessageEntityTextUrl, User)

from albatross.api import ingest
from albatross.config import settings
from albatross.telegram import client as tg
from albatross.telegram import listener, status

M1 = "B6qN8yBtYfxfF9JoTywSr4CZDaQZnDhRf4FKKWHbpump"
M2 = "8SPTAnmrZ7pfuMm2Z7Bt7fB9BuCTNDd7nA4vYWnTpump"
E1 = "0x" + "a1" * 20
CHAT = -1000000000101
OTHER = -1000000000202
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def message(text, *, id=7, at=T0, sender=None, chat=None, post=False, entities=None, markup=None):
    async def get_sender():
        return sender
    return SimpleNamespace(id=id, message=text, date=at, entities=entities, reply_markup=markup, chat=chat, post=post,
                           get_sender=get_sender)


def user(username=None, first="Some", last="Caller"):
    return User(id=42, first_name=first, last_name=last, username=username)


def channel(username=None, title="Example Calls"):
    return Channel(id=101, title=title, photo=ChatPhotoEmpty(), date=None, broadcast=True, username=username)


# ─── pure parts ─────────────────────────────────────────────────────────────
def test_src_id_is_stable_distinct_and_safe_for_javascript():
    a = listener.src_id(CHAT, 7, M1)
    assert a == listener.src_id(CHAT, 7, M1)
    assert len({a, listener.src_id(CHAT, 8, M1), listener.src_id(OTHER, 7, M1), listener.src_id(CHAT, 7, M2)}) == 4
    assert all(2 ** 52 <= listener.src_id(CHAT, i, M1) < 2 ** 53 for i in range(500))


def test_urls_from_linked_words_and_buttons():
    markup = SimpleNamespace(rows=[
        SimpleNamespace(buttons=[KeyboardInlineButton("Chart", InlineButtonTypeUrl(f"https://pump.fun/coin/{M2}")),
                                 KeyboardInlineButton("Vote", InlineButtonTypeCallback(b"x"))]),
        SimpleNamespace(buttons=[SimpleNamespace(text="Buy", url="https://t.me/SomeBot?start=a")]),   # an older layer
    ])
    m = message("Chart", entities=[MessageEntityTextUrl(0, 5, "https://jup.ag/swap/SOL-x")], markup=markup)
    assert listener.urls(m) == ["https://jup.ag/swap/SOL-x", f"https://pump.fun/coin/{M2}",
                                "https://t.me/SomeBot?start=a"]
    assert listener.urls(message("plain")) == []


def test_caller_is_the_username_else_the_display_name():
    assert listener.caller(user("somecaller")) == "@somecaller"
    assert listener.caller(user()) == "Some Caller"
    assert listener.caller(channel("examplecalls")) == "@examplecalls"
    assert listener.caller(channel()) == "Example Calls"
    assert listener.caller(None) is None


def test_calls_from_a_message():
    m = message(f"CA {M1} and {E1}", at=T0)
    found = listener.parse.addresses(m.message)
    (a, b) = listener.calls(m, found, CHAT, "Example Calls", "@somecaller")
    assert (a.chain, a.address, a.group_id, a.group_name, a.caller, a.intent, a.called_at) == (
        "solana", M1, CHAT, "Example Calls", "@somecaller", "mention", T0.timestamp())
    assert (b.chain, b.address) == ("evm", E1)
    assert a.src_id == listener.src_id(CHAT, 7, M1) and b.src_id == listener.src_id(CHAT, 7, E1)


# ─── message -> store ───────────────────────────────────────────────────────
@pytest.fixture
def stored(monkeypatch):
    out = []

    async def store(source, calls, trades=(), repeats=None):
        out.append({"source": source, "calls": calls, "repeats": repeats})
        return {"calls": len(calls), "trades": 0, "tokens": len(calls), "folded": 0}
    monkeypatch.setattr(ingest, "store", store)
    return out


def event(chat_id, msg):
    return SimpleNamespace(chat_id=chat_id, message=msg)


def test_a_watched_message_is_stored_as_telegram_calls_with_the_repeat_rule(stored):
    lis = listener.Listener(None, {CHAT})
    lis.titles[CHAT] = "Example Calls"
    run(lis.on_message(event(CHAT, message(f"CA: {M1}", sender=user("somecaller")))))
    (s,) = stored
    assert s["source"] == "telegram" and s["repeats"] == timedelta(hours=24)
    (c,) = s["calls"]
    assert (c.address, c.group_id, c.group_name, c.caller) == (M1, CHAT, "Example Calls", "@somecaller")
    assert lis.last_message_at == T0


def test_unwatched_chats_and_messages_without_a_token_store_nothing(stored):
    asked = []

    async def get_sender():
        asked.append(1)
    lis = listener.Listener(None, {CHAT})
    run(lis.on_message(event(OTHER, message(f"CA: {M1}"))))
    m = message("gm, chart looks good")
    m.get_sender = get_sender
    run(lis.on_message(event(CHAT, m)))
    assert stored == [] and asked == []                 # no sender lookup for a message with no token
    assert lis.last_message_at == T0                    # still the newest message seen in a watched chat


def test_a_channel_post_is_called_by_the_channel(stored):
    lis = listener.Listener(None, {CHAT})
    chan = channel("examplecalls", "Example Calls")
    run(lis.on_message(event(CHAT, message(f"{M1}", chat=chan, post=True, sender=None))))
    (c,) = stored[0]["calls"]
    assert (c.caller, c.group_name) == ("@examplecalls", "Example Calls")


def test_a_failing_store_is_logged_without_the_message(monkeypatch, caplog):
    async def store(*a, **k):
        raise OSError("db down")
    monkeypatch.setattr(ingest, "store", store)
    lis = listener.Listener(None, {CHAT})
    with caplog.at_level(logging.DEBUG):
        run(lis.on_message(event(CHAT, message(f"secret plans {M1}", sender=user("somecaller")))))
    assert "OSError" in caplog.text
    assert "secret plans" not in caplog.text and M1 not in caplog.text and "somecaller" not in caplog.text


# ─── the shared storage: source and the repeat rule ─────────────────────────
class Conn:
    """A connection stand-in: answers the repeat query from `held` and records what is written."""

    def __init__(self, held=()):
        self.held = list(held)          # stored (group_id, address, called_at)
        self.written = {}
        self.asked = []

    async def execute(self, *a):
        pass

    async def fetch(self, sql, groups, addrs, start, window, end):
        self.asked.append((start, window, end))
        want = set(zip(groups, addrs))
        return [{"group_id": g, "address": a, "called_at": t} for g, a, t in self.held
                if (g, a) in want and start - window < t <= end]

    async def executemany(self, sql, rows):
        self.written[sql.split("INTO", 1)[1].split()[0]] = list(rows)

    async def fetchval(self, *a):
        return 0

    @asynccontextmanager
    async def transaction(self):
        yield


def _pool(conn):
    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    async def pool():
        return Pool()
    return pool


def call(src, address, group=CHAT, at=T0):
    return ingest.CallIn(src_id=src, chain=None, address=address, group_id=group, group_name="g", caller="c",
                         intent="mention", called_at=at.timestamp())


def store(monkeypatch, conn, calls, repeats=listener.REPEAT, source="telegram"):
    monkeypatch.setattr(ingest.db, "pool", _pool(conn))
    return run(ingest.store(source, calls, repeats=repeats))


def test_a_repeat_in_the_same_chat_within_a_day_is_not_a_new_call(monkeypatch):
    conn = Conn(held=[(CHAT, M1, T0 - timedelta(hours=2))])
    out = store(monkeypatch, conn, [call(1, M1)])
    assert out["calls"] == 0 and conn.written["micro_calls"] == []
    assert conn.written["micro_tokens"] == []           # nothing new to say about the token either


def test_a_day_later_or_in_another_chat_it_is(monkeypatch):
    conn = Conn(held=[(CHAT, M1, T0 - timedelta(hours=24, minutes=1)), (OTHER, M2, T0 - timedelta(minutes=5))])
    out = store(monkeypatch, conn, [call(1, M1), call(2, M2)])
    assert out["calls"] == 2
    rows = conn.written["micro_calls"]
    assert [r[0] for r in rows] == [1, 2] and {r[8] for r in rows} == {"telegram"}       # source stored


def test_repeats_inside_one_batch_keep_the_first(monkeypatch):
    conn = Conn()
    out = store(monkeypatch, conn, [call(2, M1, at=T0 + timedelta(minutes=30)), call(1, M1), call(3, M1, group=OTHER)])
    assert out["calls"] == 2 and sorted(r[0] for r in conn.written["micro_calls"]) == [1, 3]


def test_evm_repeats_match_whatever_the_case(monkeypatch):
    conn = Conn(held=[(CHAT, E1, T0 - timedelta(hours=1))])
    assert store(monkeypatch, conn, [call(1, E1.upper().replace("0X", "0x"))])["calls"] == 0


def test_the_feed_keeps_every_call_it_sends(monkeypatch):
    conn = Conn(held=[(CHAT, M1, T0 - timedelta(hours=1))])
    out = store(monkeypatch, conn, [call(1, M1)], repeats=None, source="feed")
    assert out["calls"] == 1 and conn.asked == [] and conn.written["micro_calls"][0][8] == "feed"


def test_a_feed_calling_itself_telegram_stays_apart_from_the_listener(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    conn = Conn()
    monkeypatch.setattr(ingest.db, "pool", _pool(conn))
    monkeypatch.setattr(settings, "call_feed_token", "t")
    app = FastAPI()
    app.include_router(ingest.router)
    body = {"source": "telegram", "calls": [{"src_id": 1, "chain": "solana", "address": M1, "called_at": 1.7e9}]}
    r = TestClient(app).post("/api/ingest/calls", json=body, headers={"Authorization": "Bearer t"})
    assert r.status_code == 200 and r.json()["calls"] == 1
    assert conn.written["micro_calls"][0][8] == "feed:telegram"


# ─── the long-lived task ────────────────────────────────────────────────────
class FakeClient:
    """A Telethon client stand-in. me: what get_me answers; until: what run_until_disconnected does."""

    def __init__(self, me=None, connect_error=None, until=None, dialogs=()):
        self.me, self.connect_error, self.until, self.dialogs = me, connect_error, until, dialogs
        self.handlers, self.disconnects = [], 0

    async def connect(self):
        if self.connect_error:
            raise self.connect_error

    async def get_me(self):
        return self.me

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d

    def add_event_handler(self, fn, ev):
        self.handlers.append(fn)

    async def run_until_disconnected(self):
        if isinstance(self.until, BaseException):
            raise self.until
        if self.until == "forever":
            await asyncio.Event().wait()

    async def disconnect(self):
        self.disconnects += 1


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_api_id", "12345")
    monkeypatch.setattr(settings, "telegram_api_hash", "0" * 32)
    monkeypatch.setattr(settings, "telegram_session", "made-up-session")
    monkeypatch.setattr(settings, "telegram_watch", f"{CHAT},{OTHER}")
    saved, sleeps = [], []

    async def save(state, text, **cols):
        saved.append((state, text, cols))

    async def beat(text, last):
        saved.append(("beat", text, {}))

    real_sleep = asyncio.sleep

    async def sleep(s, *a):
        if s == status.HEARTBEAT_S:
            await asyncio.Event().wait()                # the heartbeat waits until it is cancelled
        if s:
            sleeps.append(s)                            # the backoff, without waiting
        await real_sleep(0)
    monkeypatch.setattr(status, "save", save)
    monkeypatch.setattr(status, "beat", beat)
    monkeypatch.setattr(listener.asyncio, "sleep", sleep)
    return SimpleNamespace(saved=saved, sleeps=sleeps)


def clients(monkeypatch, *fakes):
    queue = list(fakes)
    made = []

    def make(session=None, updates=False):
        assert session == "made-up-session" and updates
        made.append(queue.pop(0))
        return made[-1]
    monkeypatch.setattr(tg, "make", make)
    return made


def states(saved):
    return [s for s, _, _ in saved]


ME = user("me_myself", "My", "Account")


def test_not_configured_does_nothing(monkeypatch, configured):
    monkeypatch.setattr(settings, "telegram_watch", "")
    made = clients(monkeypatch)
    run(listener.run_forever())
    monkeypatch.setattr(settings, "telegram_watch", str(CHAT))
    monkeypatch.setattr(settings, "telegram_session", None)
    run(listener.run_forever())
    assert made == [] and configured.saved == []


def test_a_session_telegram_ended_stops_with_a_status_and_no_retry(monkeypatch, configured):
    made = clients(monkeypatch, FakeClient(me=None))
    run(listener.run_forever())
    assert states(configured.saved) == ["connecting", "signed_out"]
    assert configured.saved[-1][1] == "Telegram signed this session out; sign in again."
    assert configured.sleeps == [] and made[0].disconnects >= 1


def test_a_session_string_that_does_not_decode_is_signed_out(monkeypatch, configured):
    monkeypatch.setattr(settings, "telegram_session", "not a session string")
    run(listener.run_forever())                         # the real client factory: nothing connects
    assert states(configured.saved) == ["signed_out"] and configured.sleeps == []


def test_the_client_factory(monkeypatch):
    monkeypatch.setattr(settings, "telegram_api_id", " 12345 ")
    monkeypatch.setattr(settings, "telegram_api_hash", "0" * 32)
    c = tg.make()
    assert c.api_id == 12345 and c.session.save() == ""
    with pytest.raises(tg.SignedOut):
        tg.make("1AAAAAAAAAAAAAAAAAAAA")
    monkeypatch.setattr(settings, "telegram_api_id", "abc")
    with pytest.raises(ValueError):
        tg.make()
    monkeypatch.setattr(settings, "telegram_watch", f" {CHAT}, x,{OTHER},{CHAT},")
    assert tg.watch_ids() == [CHAT, OTHER]


@pytest.mark.parametrize("err", [errors.AuthKeyUnregisteredError(None), errors.SessionRevokedError(None),
                                 errors.UserDeactivatedBanError(None), errors.AuthKeyNotFound()])
def test_revoked_while_listening(monkeypatch, configured, err):
    clients(monkeypatch, FakeClient(me=ME, until=err))
    run(listener.run_forever())
    assert states(configured.saved) == ["connecting", "listening", "signed_out"]


def test_listening_status_names_the_chats_and_caches_the_account(monkeypatch, configured):
    dialogs = [SimpleNamespace(id=CHAT, title="Example Calls", name="Example Calls"),
               SimpleNamespace(id=-1000000000999, title="Not watched", name="Not watched")]
    clients(monkeypatch, FakeClient(me=ME, until=errors.AuthKeyUnregisteredError(None), dialogs=dialogs))
    run(listener.run_forever())
    _, text, cols = configured.saved[1]
    assert text == "Listening to 1 chat. 1 picked chat is no longer in your chat list."
    assert cols == {"account_name": "My Account", "account_username": "me_myself", "titles": {CHAT: "Example Calls"}}


def test_bad_api_keys_stop_with_a_status(monkeypatch, configured):
    clients(monkeypatch, FakeClient(connect_error=errors.ApiIdInvalidError(None)))
    run(listener.run_forever())
    assert states(configured.saved) == ["connecting", "error"] and configured.sleeps == []


def test_network_trouble_reconnects_with_a_growing_backoff(monkeypatch, configured):
    made = clients(monkeypatch, FakeClient(connect_error=OSError()), FakeClient(connect_error=ConnectionError()),
                   FakeClient(me=ME, until=None), FakeClient(connect_error=errors.FloodWaitError(None, capture=600)),
                   FakeClient(me=None))
    run(listener.run_forever())
    assert configured.sleeps == [5, 10, 20, 600]
    assert states(configured.saved) == ["connecting", "reconnecting", "connecting", "reconnecting", "connecting",
                                        "listening", "reconnecting", "connecting", "reconnecting", "connecting",
                                        "signed_out"]
    assert configured.saved[1][1] == "Connection lost. Trying again in 5 seconds."
    assert all(c.disconnects >= 1 for c in made)


def test_a_telegram_that_never_answers_is_retried_not_waited_on_forever(monkeypatch, configured):
    # Found by a live smoke: with a key Telegram doesn't know, get_me() was never answered.
    monkeypatch.setattr(listener, "SETUP_S", 0.05)

    class Silent(FakeClient):
        async def get_me(self):
            await asyncio.Event().wait()
    clients(monkeypatch, Silent(), FakeClient(me=None))
    run(listener.run_forever())
    assert states(configured.saved) == ["connecting", "reconnecting", "connecting", "signed_out"]


def test_cancelling_stops_cleanly(monkeypatch, configured):
    made = clients(monkeypatch, FakeClient(me=ME, until="forever"))

    async def go():
        task = asyncio.create_task(listener.run_forever())
        for _ in range(20):
            await asyncio.sleep(0)
        assert made[0].handlers                         # listening
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())
    assert made[0].disconnects >= 1
    assert states(configured.saved) == ["connecting", "listening"]         # nothing written on the way out


def test_keys_that_restart_it():
    assert listener.KEYS == {"TELEGRAM_SESSION", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_WATCH"}


def test_ingest_restarts_the_listener_when_its_keys_change():
    import inspect

    from albatross import __main__ as main
    src = inspect.getsource(main.run)
    assert "telegram_listener.run_forever()" in src and "changed & telegram_listener.KEYS" in src
    assert "tg_task.cancel()" in src
