"""Setup › Telegram API (albatross/api/telegram_setup.py): the contract the Setup page is coded against, the guard on
every route, the listener's state as the page shows it, the chat list and the watch list. Telegram and the database are
stand-ins; chat ids and names are made up."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from telethon import errors
from telethon.tl.types import Channel, Chat, ChatPhotoEmpty, User

from albatross import keys
from albatross.api import telegram_setup as api
from albatross.config import settings
from albatross.telegram import client as tg
from albatross.telegram import login, status

NOW = datetime.now(UTC)
WRITE = {"X-Albatross-Action": "1"}
G1, G2, C1 = -1000000000101, -202, -1000000000303


def row(state="listening", text="Listening to 1 chat.", ago=timedelta(seconds=20), **kw):
    return {"state": state, "status": text, "account_name": "My Account", "account_username": "me_myself",
            "titles": {G1: "Example Calls"}, "last_message_at": NOW - timedelta(minutes=2),
            "updated_at": NOW - ago, **kw}


class Pool:
    async def fetchval(self, sql, *a):
        assert "source = $1" in sql and a == ("telegram",)
        return 7


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "site_password_hash", None)
    monkeypatch.setattr(settings, "allowed_hosts", "testserver")
    monkeypatch.setattr(settings, "telegram_api_id", "12345")
    monkeypatch.setattr(settings, "telegram_api_hash", "0" * 32)
    monkeypatch.setattr(settings, "telegram_session", "made-up-session")
    monkeypatch.setattr(settings, "telegram_watch", str(G1))
    monkeypatch.setattr(api, "_chats", None)
    out = SimpleNamespace(row=row(), saved={}, deleted=[], status=[], from_env=set(), clients=[])

    async def read():
        return out.row

    async def pool():
        return Pool()

    async def save(name, value):
        out.saved[name] = value
        monkeypatch.setattr(settings, name.lower(), value)

    async def delete(name):
        out.deleted.append(name)
        monkeypatch.setattr(settings, name.lower(), "" if name == "TELEGRAM_WATCH" else None)

    async def status_save(state, text, **cols):
        out.status.append((state, text, cols))
    monkeypatch.setattr(status, "read", read)
    monkeypatch.setattr(api.db, "pool", pool)
    monkeypatch.setattr(keys, "save", save)
    monkeypatch.setattr(keys, "delete", delete)
    monkeypatch.setattr(keys, "from_env", lambda name: name in out.from_env)
    monkeypatch.setattr(status, "save", status_save)

    def use(*fakes):
        queue = list(fakes)

        def make(session=None, updates=False):
            assert session == settings.telegram_session and not updates
            out.clients.append(queue.pop(0))
            return out.clients[-1]
        monkeypatch.setattr(tg, "make", make)
    out.use = use
    app = FastAPI()
    app.include_router(api.router)
    out.http = TestClient(app)
    return out


# ─── GET /api/setup/telegram ────────────────────────────────────────────────
def test_overview_contract(env):
    r = env.http.get("/api/setup/telegram")
    assert r.status_code == 200
    j = r.json()
    assert j == {
        "api_id_set": True, "api_hash_set": True, "signed_in": True,
        "account": {"name": "My Account", "username": "me_myself"},
        "login": {"state": "idle", "phone_hint": None, "error": None},
        "watch": [{"id": G1, "title": "Example Calls"}],
        "listener": {"running": True, "status": "Listening to 1 chat.",
                     "last_message_at": (NOW - timedelta(minutes=2)).isoformat(), "calls_today": 7},
    }


def test_overview_before_anything_is_set(env, monkeypatch):
    for name in ("telegram_api_id", "telegram_api_hash", "telegram_session"):
        monkeypatch.setattr(settings, name, None)
    monkeypatch.setattr(settings, "telegram_watch", "")
    env.row = None
    j = env.http.get("/api/setup/telegram").json()
    assert (j["api_id_set"], j["api_hash_set"], j["signed_in"]) == (False, False, False)
    assert (j["account"], j["watch"]) == (None, [])
    assert j["listener"] == {"running": False, "status": "Add the API ID and API hash, then sign in.",
                             "last_message_at": None, "calls_today": 7}


def test_a_session_telegram_ended_shows_as_signed_out(env):
    env.row = row("signed_out", status.SIGNED_OUT_TEXT, ago=timedelta(hours=5))
    j = env.http.get("/api/setup/telegram").json()
    assert j["signed_in"] is False and j["account"] is None
    assert j["listener"]["status"] == "Telegram signed this session out; sign in again."
    assert j["listener"]["running"] is False


@pytest.mark.parametrize("state, ago, watch, want", [
    ("listening", timedelta(minutes=10), str(G1), (False, "Not running. Check that the ingest service is up.")),
    ("reconnecting", timedelta(seconds=10), str(G1), (False, "Connection lost.")),
    ("starting", timedelta(seconds=10), str(G1), (False, "Starting.")),
    ("error", timedelta(days=2), str(G1), (False, "Bad keys.")),           # a stopped state doesn't go stale
    ("listening", timedelta(seconds=10), "", (False, "Pick the chats to watch.")),
    ("off", timedelta(seconds=10), str(G1), (False, "Starting.")),
])
def test_listener_states(env, monkeypatch, state, ago, watch, want):
    text = {"reconnecting": "Connection lost.", "starting": "Starting.", "error": "Bad keys."}.get(state, "Listening.")
    env.row = row(state, text, ago=ago)
    monkeypatch.setattr(settings, "telegram_watch", watch)
    lis = env.http.get("/api/setup/telegram").json()["listener"]
    assert (lis["running"], lis["status"]) == want


def test_watched_chats_without_a_known_title(env, monkeypatch):
    monkeypatch.setattr(settings, "telegram_watch", f"{G1},{C1}")
    assert env.http.get("/api/setup/telegram").json()["watch"] == [
        {"id": G1, "title": "Example Calls"}, {"id": C1, "title": f"Chat {C1}"}]


def test_the_page_answers_only_where_setup_is_allowed(env, monkeypatch):
    monkeypatch.setattr(settings, "allowed_hosts", "localhost")
    assert env.http.get("/api/setup/telegram").status_code == 403
    assert env.http.get("/api/setup/telegram/chats").status_code == 403
    assert env.http.post("/api/setup/telegram/start", json={"phone": "+1"}, headers=WRITE).status_code == 403


# ─── sign-in steps ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("path, body, fn", [
    ("start", {"phone": "+1 555 010 0012"}, "start"),
    ("code", {"code": "12345"}, "code"),
    ("password", {"password": "made-up"}, "password"),
])
def test_sign_in_steps_answer_the_login_object(env, monkeypatch, path, body, fn):
    got = []

    async def step(value):
        got.append(value)
        return {"state": "code_sent", "phone_hint": "+1 ••• ••• ••12", "error": None}
    monkeypatch.setattr(login, fn, step)
    url = f"/api/setup/telegram/{path}"
    assert env.http.post(url, json=body).status_code == 403                 # no X-Albatross-Action header
    assert env.http.post(url, content=str(body), headers=WRITE).status_code == 415     # not JSON
    r = env.http.post(url, json=body, headers=WRITE)
    assert r.status_code == 200 and r.json() == {"state": "code_sent", "phone_hint": "+1 ••• ••• ••12", "error": None}
    assert got == list(body.values())


# ─── the chat list ──────────────────────────────────────────────────────────
def dialogs():
    from telethon import utils
    ents = [
        Channel(id=101, title="Example Calls", photo=ChatPhotoEmpty(), date=None, megagroup=True,
                participants_count=1200),
        Chat(id=202, title="Small Group", photo=ChatPhotoEmpty(), participants_count=8, date=None, version=1),
        Channel(id=303, title="Example Channel", photo=ChatPhotoEmpty(), date=None, broadcast=True),
        User(id=404, first_name="Some", last_name="Friend"),                                    # a private chat
        Channel(id=505, title="Left Group", photo=ChatPhotoEmpty(), date=None, megagroup=True, left=True),
        Chat(id=606, title="Old Group", photo=ChatPhotoEmpty(), participants_count=3, date=None, version=1,
             deactivated=True),
    ]
    return [SimpleNamespace(id=utils.get_peer_id(e), entity=e, title=utils.get_display_name(e)) for e in ents]


class FakeClient:
    def __init__(self, me=True, error=None):
        self.me, self.error, self.disconnects = me, error, 0

    async def connect(self):
        if self.error:
            raise self.error

    async def get_me(self):
        return SimpleNamespace(id=1) if self.me else None

    async def iter_dialogs(self):
        for d in dialogs():
            yield d

    async def disconnect(self):
        self.disconnects += 1


def test_chats_lists_groups_and_channels_only(env):
    env.use(fake := FakeClient())
    r = env.http.get("/api/setup/telegram/chats")
    assert r.status_code == 200
    assert r.json() == [
        {"id": G1, "title": "Example Calls", "kind": "supergroup", "members": 1200, "watched": True},
        {"id": -202, "title": "Small Group", "kind": "group", "members": 8, "watched": False},
        {"id": C1, "title": "Example Channel", "kind": "channel", "members": None, "watched": False},
    ]
    assert fake.disconnects == 1
    env.http.get("/api/setup/telegram/chats")
    assert len(env.clients) == 1                         # kept for a minute


def test_chats_when_telegram_ended_the_session(env):
    env.use(FakeClient(me=False))
    r = env.http.get("/api/setup/telegram/chats")
    assert r.status_code == 409 and r.json()["detail"] == "Telegram signed this session out; sign in again."
    assert env.status == [("signed_out", status.SIGNED_OUT_TEXT, {})]


def test_chats_with_a_session_string_that_does_not_decode(env, monkeypatch):
    monkeypatch.setattr(tg, "make", lambda *a, **k: (_ for _ in ()).throw(tg.SignedOut()))
    r = env.http.get("/api/setup/telegram/chats")
    assert r.status_code == 409 and env.status == [("signed_out", status.SIGNED_OUT_TEXT, {})]


@pytest.mark.parametrize("err, code", [(OSError(), 502), (errors.FloodWaitError(None, capture=30), 429),
                                       (errors.ApiIdInvalidError(None), 409)])
def test_chats_errors(env, err, code):
    env.use(FakeClient(error=err))
    assert env.http.get("/api/setup/telegram/chats").status_code == code


def test_chats_need_a_sign_in(env, monkeypatch):
    monkeypatch.setattr(settings, "telegram_session", None)
    r = env.http.get("/api/setup/telegram/chats")
    assert r.status_code == 409 and r.json()["detail"] == "Sign in to Telegram first."


# ─── the watch list ─────────────────────────────────────────────────────────
def test_put_watch_saves_the_ids_and_restarts_the_listener(env):
    env.use(FakeClient())
    r = env.http.put("/api/setup/telegram/watch", json={"ids": [C1, G1, C1]}, headers=WRITE)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "watch": [{"id": C1, "title": "Example Channel"},
                                              {"id": G1, "title": "Example Calls"}]}
    assert env.saved == {"TELEGRAM_WATCH": f"{C1},{G1}"}
    assert env.status == [("starting", "Starting.", {"titles": {C1: "Example Channel", G1: "Example Calls"}})]


def test_put_watch_refuses_a_chat_that_isnt_a_group_or_channel_of_yours(env):
    env.use(FakeClient())
    r = env.http.put("/api/setup/telegram/watch", json={"ids": [404]}, headers=WRITE)    # the private chat
    assert r.status_code == 400 and env.saved == {}


def test_put_watch_unchanged_or_empty(env):
    env.use(FakeClient())
    assert env.http.put("/api/setup/telegram/watch", json={"ids": [G1]}, headers=WRITE).status_code == 200
    assert env.saved == {} and env.status == []                                       # nothing changed
    r = env.http.put("/api/setup/telegram/watch", json={"ids": []}, headers=WRITE)
    assert r.json() == {"ok": True, "watch": []} and env.deleted == ["TELEGRAM_WATCH"]


def test_put_watch_when_the_environment_sets_it(env):
    env.from_env.add("TELEGRAM_WATCH")
    r = env.http.put("/api/setup/telegram/watch", json={"ids": [G1]}, headers=WRITE)
    assert r.status_code == 409 and "TELEGRAM_WATCH is set in .env or data.keys" in r.json()["detail"]


def test_put_watch_needs_the_action_header(env):
    assert env.http.put("/api/setup/telegram/watch", json={"ids": [G1]}).status_code == 403


# ─── signing out ────────────────────────────────────────────────────────────
def test_delete_signs_out(env, monkeypatch):
    called = []

    async def log_out():
        called.append(1)
        return True
    monkeypatch.setattr(login, "log_out", log_out)
    assert env.http.delete("/api/setup/telegram").status_code == 415                    # writes send JSON
    assert env.http.request("DELETE", "/api/setup/telegram", json={}).status_code == 403
    r = env.http.request("DELETE", "/api/setup/telegram", json={}, headers=WRITE)
    assert r.status_code == 200 and r.json() == {"ok": True} and called == [1]


def test_delete_when_telegram_cannot_be_reached(env, monkeypatch):
    async def log_out():
        return False                                    # forgotten here, not ended at Telegram
    monkeypatch.setattr(login, "log_out", log_out)
    r = env.http.request("DELETE", "/api/setup/telegram", json={}, headers=WRITE)
    assert r.status_code == 502
    assert r.json()["detail"] == ("Signed out here, but Telegram couldn't be reached to end the session. End it in "
                                  "Telegram under Settings > Devices.")


def test_the_router_is_registered_in_the_dashboard(env):
    from albatross.api.app import app
    r = TestClient(app).get("/api/setup/telegram")
    assert r.status_code == 200 and r.json()["listener"]["calls_today"] == 7
