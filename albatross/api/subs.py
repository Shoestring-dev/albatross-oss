"""The Setup page's subscriptions panel: sign the agents' gateway (deploy/codex_gateway.py, the `subs` container) in
to a ChatGPT plan (Codex CLI) or a Claude plan (Claude Code CLI), and see where it stands.

Everything goes through the gateway's Unix socket (settings.codex_gateway_sock). Most installs don't run the gateway,
so an unreachable one is a normal answer that says how to start it, never an error. The logins stay in the gateway:
a token or auth.json passes through here once on its way in and is never stored, logged or sent back.
The switches (CODEX_ENABLED, CLAUDE_ENABLED, CLAUDE_MODEL, AGENTS_ON) are ordinary settings the Setup page saves.
"""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from albatross.api.guard import read_guard, write_guard
from albatross.config import settings
from albatross.llm import codex

log = logging.getLogger(__name__)
router = APIRouter()

HOW = "docker compose --profile subscriptions up -d"
STATUS_S = 30                  # /v1/status runs `codex login status` and, the first time, each CLI's --version
LOGIN_S = 45                   # the device sign-in waits up to 20 s for Codex to show its code
TOKEN_S = 150                  # a new Claude token is checked with one tiny call (90 s at most) after up to 30 s queued
IDLE = {"state": "idle", "url": None, "code": None, "error": None}


class AuthJson(BaseModel):
    auth_json: str


class Token(BaseModel):
    token: str


NOT_RUNNING = "The subscriptions gateway isn't running."
SLOW = "The subscriptions gateway didn't answer in time."
OLD = "The subscriptions gateway is an older version without sign-in. Rebuild or reinstall it."


def hint(why: str) -> str:
    return f"{why} Start it with: {HOW}" if why == NOT_RUNNING else why


async def call(method: str, path: str, body: dict | None = None, timeout: float = STATUS_S) -> tuple[dict | None, str | None]:
    """(the gateway's JSON answer, None) or (None, why it couldn't be asked)."""
    try:
        status, out = await codex.gateway(method, path, body, timeout=timeout)
    except httpx.TimeoutException:
        return None, SLOW
    except (httpx.HTTPError, OSError):
        return None, NOT_RUNNING
    if status == 404:
        return None, OLD
    if status >= 500 and "ok" not in out and "state" not in out:
        return None, f"The subscriptions gateway answered with an error (HTTP {status})."
    return out, None


def login_of(body: dict | None) -> dict:
    if not isinstance(body, dict) or body.get("state") not in ("idle", "pending", "done", "failed"):
        return dict(IDLE)
    return {k: body.get(k) for k in IDLE}


def outcome(body: dict | None, why: str | None) -> dict:
    """A sign-in step's answer as {"ok", "error"}."""
    if body is None:
        return {"ok": False, "error": hint(why)}
    if body.get("ok") is True:
        return {"ok": True, "error": None}
    return {"ok": False, "error": str(body.get("error") or "The gateway refused it.")}


@router.get("/api/setup/subs", dependencies=[Depends(read_guard)])
async def subs() -> dict:
    (st, why), (login, _) = await asyncio.gather(call("GET", "/v1/status"), call("GET", "/v1/login/codex"))
    cx = (st or {}).get("codex") or {}
    cl = (st or {}).get("claude") or {}
    return {
        "gateway": {"reachable": st is not None, "error": why, "how": HOW},
        "chatgpt": {"installed": bool(cx.get("installed")), "version": cx.get("version"),
                    "signed_in": bool(cx.get("signed_in")), "plan": cx.get("plan"), "enabled": settings.codex_enabled,
                    "login": login_of(login)},
        "claude": {"installed": bool(cl.get("installed")), "version": cl.get("version"),
                   "signed_in": bool(cl.get("signed_in")), "enabled": settings.claude_enabled,
                   "model": settings.claude_model},
        "agents_on": settings.agents_on,
    }


@router.post("/api/setup/subs/chatgpt/login", dependencies=[Depends(write_guard)])
async def chatgpt_login() -> dict:
    body, why = await call("POST", "/v1/login/codex/start", {}, timeout=LOGIN_S)
    if body is None:
        return {**IDLE, "state": "failed", "error": hint(why)}
    return login_of(body)


@router.post("/api/setup/subs/chatgpt/auth-json", dependencies=[Depends(write_guard)])
async def chatgpt_auth_json(req: AuthJson) -> dict:
    return outcome(*await call("POST", "/v1/login/codex/auth-json", {"auth_json": req.auth_json}))


@router.post("/api/setup/subs/claude/token", dependencies=[Depends(write_guard)])
async def claude_token(req: Token) -> dict:
    return outcome(*await call("POST", "/v1/login/claude", {"token": req.token}, timeout=TOKEN_S))


async def signout(path: str) -> dict:
    """{"ok": true} once the gateway signed out; {"ok": false, "error"} when it couldn't be asked."""
    out = outcome(*await call("DELETE", path))
    return {"ok": True} if out["ok"] else out


@router.delete("/api/setup/subs/chatgpt", dependencies=[Depends(write_guard)])
async def chatgpt_signout() -> dict:
    return await signout("/v1/login/codex")


@router.delete("/api/setup/subs/claude", dependencies=[Depends(write_guard)])
async def claude_signout() -> dict:
    return await signout("/v1/login/claude")
