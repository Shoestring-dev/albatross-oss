import base64
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from albatross.alerts import bot
from albatross.api import gate
from albatross.config import settings

SECRET = "test-secret-0123456789"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "site_user", "admin")
    monkeypatch.setattr(settings, "site_password_hash", bot.hash_password("correct horse"))
    monkeypatch.setattr(settings, "site_session_secret", SECRET)
    gate._fails.clear()
    gate._basic_ok_until.clear()
    app = FastAPI()
    app.include_router(gate.router)
    return TestClient(app, base_url="https://testserver")


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def page_load(uri="/#/social"):
    return {"X-Forwarded-Method": "GET", "X-Forwarded-Uri": uri, "Accept": "text/html,application/xhtml+xml"}


def test_cookie_signature_expiry_and_tampering():
    now = time.time()
    tok = gate.sign("admin", int(now) + 60, SECRET)
    assert gate.session_user(tok, SECRET, now) == "admin"
    assert gate.session_user(tok, SECRET, now + 61) is None                    # expired
    assert gate.session_user(tok, "another-secret", now) is None               # key rotated: signed out
    user, exp, mac = tok.rsplit(".", 2)
    assert gate.session_user(f"{user}.{int(exp) + 10**6}.{mac}", SECRET, now) is None   # extended by hand
    assert gate.session_user(gate.sign("someone", int(now) + 60, SECRET), SECRET, now) is None
    assert gate.session_user("garbage", SECRET, now) is None and gate.session_user(tok, None, now) is None


def test_check_sends_page_loads_to_login_and_refuses_the_rest(client):
    r = client.get("/auth/check", headers=page_load("/"), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=/"
    r = client.get("/auth/check", headers={"X-Forwarded-Method": "GET", "X-Forwarded-Uri": "/api/social", "Accept": "*/*"})
    assert r.status_code == 401 and "www-authenticate" not in r.headers          # no browser password prompt
    assert client.get("/auth/check", headers=basic("admin", "correct horse")).status_code == 204
    assert client.get("/auth/check", headers=basic("admin", "wrong")).status_code == 401


def test_login_remembers_the_device_for_30_days(client):
    r = client.post("/login", data={"username": "admin", "password": "correct horse", "remember": "1",
                                    "next": "/#/social"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#/social"
    cookie = r.headers["set-cookie"]
    assert "Max-Age=2592000" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert client.get("/auth/check").status_code == 204                            # the cookie now opens the gate


def test_login_without_remember_is_a_browser_session(client):
    r = client.post("/login", data={"username": "admin", "password": "correct horse", "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303 and "Max-Age" not in r.headers["set-cookie"]


def test_wrong_password_then_lockout(client):
    for _ in range(gate.MAX_FAILS):
        r = client.post("/login", data={"username": "admin", "password": "nope"}, headers={"CF-Connecting-IP": "1.2.3.4"})
        assert r.status_code == 401 and "set-cookie" not in r.headers
    r = client.post("/login", data={"username": "admin", "password": "correct horse"}, headers={"CF-Connecting-IP": "1.2.3.4"})
    assert r.status_code == 429                                                    # even the right password waits
    r = client.post("/login", data={"username": "admin", "password": "correct horse"},
                    headers={"CF-Connecting-IP": "5.6.7.8"}, follow_redirects=False)
    assert r.status_code == 303                                                    # other visitors unaffected


def test_unconfigured_gate_lets_nobody_in(client, monkeypatch):
    monkeypatch.setattr(settings, "site_password_hash", None)
    assert client.get("/auth/check", headers=basic("admin", "correct horse")).status_code == 401
    r = client.post("/login", data={"username": "admin", "password": "correct horse"}, follow_redirects=False)
    assert r.status_code == 401


def test_next_never_leaves_the_site():
    for bad in ("https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)", "/login?next=/x", "/logout", ""):
        assert gate.safe_next(bad) == "/"
    assert gate.safe_next("/#/coin/BTC") == "/#/coin/BTC"


def test_logout_clears_the_cookie(client):
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert 'albatross_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]


def test_login_page_does_not_reveal_the_username(client):
    body = client.get("/login").text
    assert 'name="password"' in body and "Remember this device for 30 days" in body and "admin" not in body


def test_basic_credentials_share_the_lockout(client):
    ip = {"CF-Connecting-IP": "9.9.9.9"}
    for _ in range(gate.MAX_FAILS):
        assert client.get("/auth/check", headers={**basic("admin", "guess"), **ip}).status_code == 401
    r = client.get("/auth/check", headers={**basic("admin", "correct horse"), **ip})
    assert r.status_code == 429                                                     # guessing over Basic is limited too
    assert client.get("/auth/check", headers={**basic("admin", "correct horse"), "CF-Connecting-IP": "8.8.8.8"}).status_code == 204


def test_a_good_basic_header_is_trusted_briefly(client, monkeypatch):
    assert client.get("/auth/check", headers=basic("admin", "correct horse")).status_code == 204
    monkeypatch.setattr(gate, "check_password", lambda *a: (_ for _ in ()).throw(AssertionError("hashed again")))
    assert client.get("/auth/check", headers=basic("admin", "correct horse")).status_code == 204   # cached
