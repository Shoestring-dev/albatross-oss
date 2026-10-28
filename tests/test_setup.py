"""The Setup page: keys saved in the database layered under the environment (albatross/keys.py), its API
(albatross/api/setup.py) and who may use it (albatross/api/guard.py)."""

import asyncio
import functools

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from albatross import keys
from albatross.api import guard, setup
from albatross.config import settings
from albatross.deepend import cfg

WRITE = {"X-Albatross-Action": "1"}


def run(fn):
    """An async test run with asyncio.run, as the rest of the suite does (no pytest async plugin)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return wrapper


@pytest.fixture
def store(monkeypatch):
    """app_settings in memory: save/delete/load run the real _apply against this dict."""
    rows: dict[str, str] = {}

    async def _rows():
        return dict(rows)

    async def save(name, raw):
        rows[name] = raw
        return await keys.load()

    async def delete(name):
        rows.pop(name, None)
        return await keys.load()

    async def refresh():
        return len(await keys.load())

    monkeypatch.setattr(keys, "_rows", _rows)
    monkeypatch.setattr(keys, "save", save)
    monkeypatch.setattr(keys, "delete", delete)
    monkeypatch.setattr(keys, "refresh", refresh)
    monkeypatch.setattr(keys, "_env_files", lambda: {})
    for name in [*setup.CATALOG, "SETUP_DONE"]:
        monkeypatch.delenv(name, raising=False)
    snapshot = {f: getattr(settings, f) for f in type(settings).model_fields}
    yield rows
    rows.clear()
    keys.OVERLAY.clear()
    keys._DEFAULTS.clear()
    setup._tests.clear()
    for f, v in snapshot.items():
        object.__setattr__(settings, f, v)


@pytest.fixture
def api(store):
    app = FastAPI()
    app.include_router(setup.router)
    return TestClient(app, base_url="http://localhost:8787")


# ─── keys ───────────────────────────────────────────────────────────────────────────────────────────────────────
@run
async def test_a_saved_key_applies_in_place_and_removing_it_restores_the_default(store):
    assert settings.coingecko_plan == "demo" and settings.codex_enabled is False
    await keys.save("COINGECKO_PLAN", "pro")
    await keys.save("CODEX_ENABLED", "true")
    assert settings.coingecko_plan == "pro" and settings.codex_enabled is True     # typed, not the string "true"
    await keys.delete("COINGECKO_PLAN")
    assert settings.coingecko_plan == "demo" and keys.source("COINGECKO_PLAN") == "missing"


@run
async def test_the_environment_wins_over_a_saved_key(store, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "from-env")
    await keys.save("FRED_API_KEY", "saved")
    assert keys.source("FRED_API_KEY") == "env" and keys.value("FRED_API_KEY") == "from-env"
    assert "FRED_API_KEY" not in keys.OVERLAY


@run
async def test_deep_end_code_sees_a_saved_key(store):
    await keys.save("BIRDEYE_API_KEY", "be-key")
    assert cfg.env_str("BIRDEYE_API_KEY") == "be-key"


@run
async def test_a_saved_value_of_the_wrong_type_is_ignored(store):
    store["XAI_DAILY_USD"] = "lots"
    await keys.load()
    assert settings.xai_daily_usd == 3.0 and "XAI_DAILY_USD" not in keys.OVERLAY


@run
async def test_listeners_hear_which_names_changed(store):
    heard = []
    fn = keys.on_change(lambda changed: heard.append(set(changed)))
    try:
        await keys.save("TELEGRAM_ALERT_BOT_TOKEN", "1:abc")
        await keys.save("FRED_API_KEY", "f")
    finally:
        keys._listeners.remove(fn)
    assert heard == [{"TELEGRAM_ALERT_BOT_TOKEN"}, {"FRED_API_KEY"}]


# ─── guard ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_setup_answers_only_on_local_host_names_without_a_site_password(api, monkeypatch):
    monkeypatch.setattr(settings, "site_password_hash", None)
    assert api.get("/api/setup").status_code == 200
    assert api.get("/api/setup", headers={"Host": "127.0.0.1:8787"}).status_code == 200
    assert api.get("/api/setup", headers={"Host": "[::1]:8787"}).status_code == 200
    assert api.get("/api/setup", headers={"Host": "evil.example:8787"}).status_code == 403     # DNS rebinding
    monkeypatch.setattr(settings, "allowed_hosts", "localhost,box.lan")
    assert api.get("/api/setup", headers={"Host": "box.lan:8787"}).status_code == 200


def test_setup_needs_a_sign_in_when_a_site_password_is_set(api, monkeypatch):
    from albatross.alerts.bot import hash_password
    from albatross.api import gate
    monkeypatch.setattr(settings, "site_password_hash", hash_password("correct horse"))
    monkeypatch.setattr(settings, "site_session_secret", "s3cret")
    assert api.get("/api/setup").status_code == 401
    api.cookies.set(gate.COOKIE, gate.sign(settings.site_user, 2_000_000_000, "s3cret"))
    assert api.get("/api/setup").status_code == 200


def test_a_write_needs_json_the_action_header_and_the_same_origin(api):
    url = "/api/setup/keys/COINGECKO_PLAN"
    assert api.put(url, json={"value": "pro"}).status_code == 403                                   # no header
    assert api.put(url, content="value=pro", headers={**WRITE, "Content-Type": "application/x-www-form-urlencoded"}
                   ).status_code == 415
    assert api.put(url, json={"value": "pro"}, headers={**WRITE, "Origin": "https://evil.example"}).status_code == 403
    assert api.put(url, json={"value": "pro"}, headers={**WRITE, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert api.put(url, json={"value": "pro"}, headers={**WRITE, "Origin": "http://localhost:8787"}).status_code == 200


def test_host_names_parse_with_ports_and_brackets():
    assert guard._hostname("LocalHost:8787") == "localhost"
    assert guard._hostname("[::1]:8787") == "::1" and guard._hostname("::1") == "::1"
    assert guard._hostname("example.com") == "example.com"


# ─── the API ────────────────────────────────────────────────────────────────────────────────────────────────────
def test_first_run_until_something_runs_the_agents_or_setup_is_dismissed(api, monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert api.get("/api/setup").json()["first_run"] is True
    assert api.post("/api/setup/done", json={"done": True}, headers=WRITE).status_code == 200
    body = api.get("/api/setup").json()
    assert body["first_run"] is False and body["done"] is True and body["ai_ready"] is False


def test_a_saved_secret_is_never_sent_back(api, monkeypatch):
    async def ok(key):
        return True, "Key works."
    monkeypatch.setitem(setup.CATALOG, "OPENROUTER_API_KEY",
                        setup.Item("OPENROUTER_API_KEY", "OpenRouter key", "", check=ok))
    secret = "sk-or-v1-0123456789abcdefWXYZ"
    r = api.put("/api/setup/keys/OPENROUTER_API_KEY", json={"value": f"  {secret}  "}, headers=WRITE)
    assert r.status_code == 200
    assert secret not in r.text and r.json()["item"]["hint"] == "…WXYZ" and r.json()["item"]["value"] is None
    assert r.json()["item"]["test"]["ok"] is True
    assert secret not in api.get("/api/setup").text
    assert settings.openrouter_api_key == secret                           # stripped, and in force
    assert api.get("/api/setup").json()["first_run"] is False


def test_values_are_checked_by_kind(api):
    put = lambda name, v: api.put(f"/api/setup/keys/{name}", json={"value": v}, headers=WRITE)   # noqa: E731
    assert put("XAI_DAILY_USD", "-1").status_code == 422
    assert put("XAI_DAILY_USD", "2.5").status_code == 200 and settings.xai_daily_usd == 2.5
    assert put("CODEX_ENABLED", "maybe").status_code == 422
    assert put("CLAUDE_MODEL", "gpt").status_code == 422
    assert put("CLAUDE_MODEL", "haiku").status_code == 200 and settings.claude_model == "haiku"
    assert put("SOLANA_RPC", "ftp://x").status_code == 422
    assert put("FRED_API_KEY", "a\nb").status_code == 422
    assert put("FRED_API_KEY", "   ").status_code == 422
    assert put("NOT_A_KEY", "x").status_code == 404
    assert put("TELEGRAM_SESSION", "x").status_code == 404           # only the Telegram login writes the session


def test_a_key_from_the_environment_cant_be_changed_here(api, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "from-env")
    assert api.put("/api/setup/keys/FRED_API_KEY", json={"value": "x"}, headers=WRITE).status_code == 409
    assert api.delete("/api/setup/keys/FRED_API_KEY", headers={**WRITE, "Content-Type": "application/json"}
                      ).status_code == 409
    item = next(i for g in api.get("/api/setup").json()["groups"] for i in g["items"] if i["name"] == "FRED_API_KEY")
    assert item["state"] == "env"


def test_the_alerts_password_is_stored_as_a_hash(api, store):
    from albatross.alerts.bot import check_password
    assert api.put("/api/setup/keys/TELEGRAM_ALERT_PASSWORD_HASH", json={"value": "short"}, headers=WRITE).status_code == 422
    r = api.put("/api/setup/keys/TELEGRAM_ALERT_PASSWORD_HASH", json={"value": "a long password"}, headers=WRITE)
    assert r.status_code == 200 and "a long password" not in store["TELEGRAM_ALERT_PASSWORD_HASH"]
    assert check_password("a long password", settings.telegram_alert_password_hash)


def test_a_generated_call_feed_token_is_shown_once(api):
    r = api.put("/api/setup/keys/CALL_FEED_TOKEN", json={"generate": True}, headers=WRITE)
    token = r.json()["token"]
    assert len(token) >= 40 and settings.call_feed_token == token
    assert token not in api.get("/api/setup").text
    assert api.put("/api/setup/keys/FRED_API_KEY", json={"generate": True}, headers=WRITE).status_code == 422


def test_remove_clears_a_saved_key(api):
    api.put("/api/setup/keys/COINALYZE_API_KEY", json={"value": "k" * 20}, headers=WRITE)
    r = api.delete("/api/setup/keys/COINALYZE_API_KEY", headers={**WRITE, "Content-Type": "application/json"})
    assert r.json()["item"]["state"] == "missing" and settings.coinalyze_api_key is None


# ─── provider checks ────────────────────────────────────────────────────────────────────────────────────────────
def _client(monkeypatch, handler):
    c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(setup, "client", lambda: c)


@run
async def test_a_rejected_key_says_so_without_the_key(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(401, json={"error": "bad key"})
    _client(monkeypatch, handler)
    ok, msg = await setup.check_fred("secret-fred-key")
    assert not ok and "rejected" in msg and "secret-fred-key" not in msg
    ok, msg = await setup.check_telegram_bot("123:secret-token")
    assert not ok and "secret-token" not in msg and "secret-token" in seen[-1]


@run
async def test_a_check_that_cant_reach_the_provider_keeps_the_key_out_of_the_message(monkeypatch, store):
    def handler(request):
        raise httpx.ConnectError(f"failed for {request.url}")
    _client(monkeypatch, handler)
    store["FRED_API_KEY"] = "secret-fred-key"
    await keys.load()
    out = await setup.run_check(setup.CATALOG["FRED_API_KEY"])
    assert out["ok"] is False and "secret-fred-key" not in out["message"] and "ConnectError" in out["message"]


@run
async def test_openrouter_check_reports_usage(monkeypatch):
    _client(monkeypatch, lambda r: httpx.Response(200, json={"data": {"usage": 1.5, "limit": 10}}))
    assert await setup.check_openrouter("k") == (True, "Key works. $1.50 used of its $10.00 limit.")


@run
async def test_telegram_check_refuses_a_shared_bot(monkeypatch):
    monkeypatch.setattr(settings, "telegram_shared_bots", "busy_bot")
    _client(monkeypatch, lambda r: httpx.Response(200, json={"ok": True, "result": {"username": "Busy_Bot"}}))
    ok, msg = await setup.check_telegram_bot("1:x")
    assert not ok and "TELEGRAM_SHARED_BOTS" in msg


@run
async def test_rpc_check_reads_get_health(monkeypatch):
    _client(monkeypatch, lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "result": "ok", "id": 1}))
    assert (await setup.check_rpc("https://rpc.example/key"))[0] is True
    _client(monkeypatch, lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "error": {"message": "behind"}, "id": 1}))
    ok, msg = await setup.check_rpc("https://rpc.example/key")
    assert not ok and "behind" in msg


def test_every_catalog_name_is_a_setting_or_a_deep_end_key():
    deep = {"BIRDEYE_API_KEY", "GITHUB_TOKEN", "FOMOAPI_KEY", "CRYPTOPANIC_PLAN", "DEEP_X_WEEKLY_USD"}
    for name in setup.CATALOG:
        assert keys.field(name) or name in deep, name


# ─── keys never reach an error text ─────────────────────────────────────────────────────────────────────────────
def test_redact_hides_keys_in_urls_and_bot_paths():
    from albatross.http import redact
    assert redact("401 for url https://api.coinalyze.net/v1/x?api_key=SECRET1&a=1") == \
        "401 for url https://api.coinalyze.net/v1/x?api_key=…&a=1"
    assert "SECRET" not in redact("https://api.telegram.org/bot123:AA-SECRET_x/getMe")
    assert "SECRET" not in redact("posts/?auth_token=SECRET&public=true")
    assert redact("monkey=1, keyboard") == "monkey=1, keyboard"


def test_coinalyze_and_fred_errors_leave_the_key_out(monkeypatch):
    from albatross.venues import coinalyze, fred
    c = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": "bad key"})))
    monkeypatch.setattr(coinalyze, "client", lambda: c)
    monkeypatch.setattr(fred, "client", lambda: c)
    monkeypatch.setattr(settings, "coinalyze_api_key", "COINALYZE-SECRET")
    monkeypatch.setattr(settings, "fred_api_key", "FRED-SECRET")
    with pytest.raises(httpx.HTTPStatusError) as e:
        asyncio.run(coinalyze._get("future-markets"))
    assert "SECRET" not in str(e.value) and e.value.response.status_code == 401
    import datetime as dt
    with pytest.raises(httpx.HTTPStatusError) as e:
        asyncio.run(fred.observations("DGS10", dt.date(2026, 1, 1)))
    assert "SECRET" not in str(e.value)


def test_the_whole_dashboard_answers_only_on_local_names_without_a_password(monkeypatch):
    from albatross.api.app import app
    monkeypatch.setattr(settings, "site_password_hash", None)
    assert TestClient(app, base_url="http://evil.example").get("/favicon.ico").status_code == 403
    assert TestClient(app, base_url="http://evil.example").post("/api/ingest/calls", json={}).status_code != 403
    assert TestClient(app, base_url="http://localhost:8787").get("/favicon.ico").status_code == 200
