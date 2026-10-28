"""The website's login: a password page and a signed cookie that remembers the device for 30 days.

Caddy (deploy/Caddyfile) still stands in front of everything. For every request except /login and the
call-feed ingest path it first asks GET /auth/check here (forward_auth) and passes the request on only
when that answers 2xx. /auth/check accepts either:
- the session cookie "<user>.<expires unix>.<HMAC-SHA256>", signed with SITE_SESSION_SECRET (data.keys).
  The login form sets it for 30 days when "Remember this device" is ticked (the default); otherwise for
  12 hours, in a cookie the browser drops when it closes;
- HTTP Basic credentials (scripts, curl -u), checked against the same password hash.
A browser page load with neither is sent to /login?next=<where it was going>; anything else gets a 401.
Nothing configured means nobody gets in. Rotating SITE_SESSION_SECRET signs every device out. Wrong
passwords, from the form or as Basic credentials, are limited per visitor (Cloudflare's CF-Connecting-IP):
10 in 15 minutes, then a 15-minute wait. The password hash (200k PBKDF2 rounds, ~0.1 s) runs off the event
loop, and a Basic header that checked out is trusted for 5 minutes so a script's page of requests costs one.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import time
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from albatross.alerts.bot import check_password
from albatross.config import settings

router = APIRouter()

COOKIE = "albatross_session"
REMEMBER_S = 30 * 24 * 3600
SESSION_S = 12 * 3600
MAX_FAILS, WINDOW_S = 10, 15 * 60
BASIC_TRUST_S = 300
_fails: dict[str, list[float]] = {}
_basic_ok_until: dict[str, float] = {}      # sha256 of a Basic header that checked out -> trusted until


def sign(user: str, expires: int, secret: str) -> str:
    body = f"{user}.{expires}"
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def session_user(token: str | None, secret: str | None, now: float | None = None) -> str | None:
    """The signed-in user if the cookie is genuine and unexpired, else None."""
    if not token or not secret:
        return None
    try:
        user, expires, mac = token.rsplit(".", 2)
        expires_at = int(expires)
    except ValueError:
        return None
    if not hmac.compare_digest(sign(user, expires_at, secret), token):
        return None
    return user if expires_at > (now or time.time()) and user == settings.site_user else None


def basic_ok(header: str | None) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:].strip()).decode().partition(":")
    except ValueError:
        return False
    ok_pw = check_password(password, settings.site_password_hash)       # always hashed: the time says nothing
    return hmac.compare_digest(user, settings.site_user) & ok_pw


def safe_next(target: str | None) -> str:
    """Only a path on this site: never another host (open redirect) and never back to /login."""
    t = (target or "/").strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t or t.startswith(("/login", "/logout")):
        return "/"
    return t


def _client(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _locked(ip: str, now: float) -> bool:
    if len(_fails) > 10_000:                     # forget visitors whose failures have all aged out
        for k in [k for k, ts in _fails.items() if not ts or now - ts[-1] >= WINDOW_S]:
            del _fails[k]
    recent = [t for t in _fails.get(ip, []) if now - t < WINDOW_S]
    _fails[ip] = recent
    return len(recent) >= MAX_FAILS


@router.get("/auth/check")
async def check(request: Request) -> Response:
    if session_user(request.cookies.get(COOKIE), settings.site_session_secret):
        return Response(status_code=204)
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("basic "):
        ip, now, key = _client(request), time.time(), hashlib.sha256(auth.encode()).hexdigest()
        if _basic_ok_until.get(key, 0) > now:
            return Response(status_code=204)
        if _locked(ip, now):
            return JSONResponse({"error": "too many wrong passwords; wait 15 minutes"}, status_code=429)
        if await asyncio.to_thread(basic_ok, auth):
            if len(_basic_ok_until) > 100:           # only good headers land here; a handful at most
                _basic_ok_until.clear()
            _basic_ok_until[key] = now + BASIC_TRUST_S
            return Response(status_code=204)
        _fails.setdefault(ip, []).append(now)
        return JSONResponse({"error": "wrong username or password"}, status_code=401)
    uri = request.headers.get("x-forwarded-uri") or "/"
    if request.headers.get("x-forwarded-method", "GET") == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/login?next={quote(safe_next(uri), safe='/?=&#')}", status_code=302)
    return JSONResponse({"error": "login required"}, status_code=401)


@router.get("/login", response_class=HTMLResponse)
async def login_page(next: str = "/", error: str | None = None) -> HTMLResponse:
    return HTMLResponse(_page(safe_next(next), error), headers={"Cache-Control": "no-store"})


@router.post("/login")
async def login(request: Request) -> Response:
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
    target, ip, now = safe_next(form.get("next")), _client(request), time.time()
    if _locked(ip, now):
        return HTMLResponse(_page(target, "locked"), status_code=429, headers={"Cache-Control": "no-store"})
    user, password = form.get("username", "").strip(), form.get("password", "")
    ok_pw = await asyncio.to_thread(check_password, password, settings.site_password_hash)   # always hashed
    if not (settings.site_password_hash and settings.site_session_secret
            and hmac.compare_digest(user, settings.site_user) & ok_pw):
        _fails.setdefault(ip, []).append(now)
        return HTMLResponse(_page(target, "wrong"), status_code=401, headers={"Cache-Control": "no-store"})
    _fails.pop(ip, None)
    remember = form.get("remember") == "1"
    expires = int(now) + (REMEMBER_S if remember else SESSION_S)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(COOKIE, sign(user, expires, settings.site_session_secret), max_age=REMEMBER_S if remember else None,
                    httponly=True, secure=True, samesite="lax", path="/")
    return resp


@router.get("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp


ERRORS = {"wrong": "That username or password isn't right.",
          "locked": "Too many wrong tries from here. Wait 15 minutes and try again."}


def _page(target: str, error: str | None) -> str:
    msg = ERRORS.get(error or "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow"><meta name="color-scheme" content="dark">
<title>Albatross · Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{ --ink: #efe8dc; --ink-2: #bdb4a5; --muted: #a39c90; --cream: #e7d9bf; --accent: #a3392f; --ground: #0d0e0e; --surface: #16181a; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; min-height: 100%; background: var(--ground); color: var(--ink); font: 400 15px/1.5 Inter, system-ui, sans-serif; }}
  body {{ display: grid; place-items: center; min-height: 100vh; min-height: 100dvh; padding: 24px 16px calc(24px + env(safe-area-inset-bottom)); }}
  main {{ width: 100%; max-width: 380px; padding: 30px 26px 26px; border-radius: 18px; background: var(--surface); border: 1px solid rgb(239 232 220 / .1); }}
  .kicker {{ margin: 0 0 8px; font: 600 11.5px/1 Inter, sans-serif; letter-spacing: .18em; text-transform: uppercase; color: var(--cream); }}
  h1 {{ margin: 0 0 22px; font: 400 44px/1 "Instrument Serif", Georgia, serif; }}
  h1 em {{ color: var(--cream); }}
  label {{ display: block; margin: 0 0 6px; font-size: 13px; font-weight: 500; color: var(--ink-2); }}
  input[type=text], input[type=password] {{ width: 100%; height: 48px; margin: 0 0 16px; padding: 0 14px; border-radius: 12px; font: 400 16px Inter, sans-serif;
    color: var(--ink); background: #0f1011; border: 1px solid rgb(239 232 220 / .2); }}
  input:focus-visible, button:focus-visible {{ outline: 2px solid var(--cream); outline-offset: 2px; }}
  .remember {{ display: flex; align-items: center; gap: 10px; min-height: 44px; margin: 0 0 18px; color: var(--ink); font-size: 14.5px; cursor: pointer; }}
  .remember input {{ width: 20px; height: 20px; accent-color: var(--accent); }}
  button {{ width: 100%; height: 50px; border: 0; border-radius: 999px; font: 600 16px Inter, sans-serif; color: #fff4f0; background: var(--accent); cursor: pointer; }}
  .err {{ margin: 0 0 16px; padding: 10px 12px; border-radius: 10px; font-size: 14px; color: #fff4f0; background: rgb(163 57 47 / .35); border: 1px solid rgb(192 69 59 / .6); }}
  .foot {{ margin: 16px 0 0; font-size: 12.5px; color: var(--muted); }}
</style></head>
<body><main>
  <p class="kicker">Private</p>
  <h1>Albatross<em>.</em></h1>
  {f'<p class="err" role="alert">{html.escape(msg)}</p>' if msg else ""}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{html.escape(target)}">
    <label for="u">Username</label>
    <input id="u" name="username" type="text" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required autofocus>
    <label class="remember"><input type="checkbox" name="remember" value="1" checked> Remember this device for 30 days</label>
    <button type="submit">Sign in</button>
  </form>
  <p class="foot">On a shared computer, untick "Remember" and you'll be signed out when the browser closes.</p>
</main>
<script>
  // The server never sees the #/view part of a link; the browser keeps it through the redirect, so carry it on.
  const n = document.querySelector("input[name=next]");
  if (location.hash && !n.value.includes("#")) n.value += location.hash;
</script></body></html>"""
