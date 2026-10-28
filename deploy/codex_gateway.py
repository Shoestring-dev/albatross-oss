#!/usr/bin/env python3
"""Subscriptions gateway: the ChatGPT-plan lane (Codex CLI, albatross/llm/codex.py) and the Claude-plan lane (Claude
Code CLI, albatross/llm/claude.py) for the ingest container.

It runs either on a Linux host as the locked-down `codex` user (no shell, can't read other home directories or the
repo; deploy/install-codex-gateway.sh) or as the non-root user of the `subs` container (deploy/subs). The plans' logins
live with it and nowhere else. It turns one request into one run of a CLI's official headless mode and listens on a
Unix socket only (no network port); the socket's directory is mounted into the ingest and dashboard containers.

Every Codex run: the agent's own instructions replace Codex's coding prompt, the answer must match a strict JSON schema,
web search and every tool are off, the sandbox is read-only, the session is not saved, and the working directory is
a fresh empty folder. Measured 2026-10-02: 4.0k tokens of fixed overhead a call instead of 13.5k.

Every Claude run (`claude -p`): the agent's instructions are the whole system prompt; every built-in tool, MCP server,
settings file and slash command is off; one turn; no session saved; a fresh empty working directory; the answer comes
back through --json-schema. Its environment is built here, never inherited: the plan's token (CLAUDE_CODE_OAUTH_TOKEN,
from a 0600 file; never ANTHROPIC_API_KEY) and telemetry, autoupdate and other nonessential traffic off. Checked on
Claude Code 2.1.312 against a stand-in API (2026-10-27): the request carries no tools.

Stdlib only: deployed to /opt/albatross-codex (deploy/install-codex-gateway.sh) or baked into deploy/subs/Dockerfile.

  POST /v1/exec    {"model", "effort", "instructions", "input", "schema" (or null), "timeout_s", "queue_s",
                    "search" (bool), "image_b64" + "image_ext" (optional)}
                   -> 200 {"ok": true, "text", "usage", "secs", "opened", "searches"} | 4xx/5xx {"ok": false, "kind", "error"}
                   model: gpt-6-luna | gpt-6.1-sol on Codex; claude-haiku | claude-sonnet | claude-opus on Claude, which
                   takes no search or image and adds "cost_usd" (its list price, total_cost_usd)
                   search: GPT-5.5 with the plan's live web search (since 2026-10-05);
                   "opened" lists every page the search opened, the evidence the agents' citation gates check
                   kind: bad_request | busy | timeout | limit (the plan's usage limit; Claude's adds "resets_at", unix
                   seconds, when its message names a time) | failed
  GET  /v1/limits  -> {"windows": [{"used_percent", "window_mins", "resets_at"}, ...], "plan", "limit_reached"}
                   (every usage window the plan reports; some plans report only a weekly one)
  GET  /v1/status  -> {"codex": {"installed", "version", "signed_in", "plan"}, "claude": {"installed", "version",
                       "signed_in"}}
  POST /v1/login/codex/start      -> {"state": "pending", "url", "code", "error"}: the CLI's device-code sign-in,
                                     finished in the background
  GET  /v1/login/codex            -> {"state": "idle" | "pending" | "done" | "failed", "url", "code", "error"}
  POST /v1/login/codex/auth-json  {"auth_json"} -> {"ok", "error"}: the auth.json from `codex login` run elsewhere
  POST /v1/login/claude           {"token"} -> {"ok", "error"}: the token from `claude setup-token`, kept only after
                                     one tiny call with it works
  DELETE /v1/login/codex, /v1/login/claude -> {"ok": true}: sign out
  GET  /healthz
A token or auth.json is never sent back or logged.
"""

import base64
import json
import os
import re
import select
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOCK = os.environ.get("CODEX_GATEWAY_SOCK", "/var/lib/albatross-codex/sock/gateway.sock")
WORK = os.environ.get("CODEX_GATEWAY_WORK", "/home/codex/work/run")
CODEX = os.environ.get("CODEX_BIN", "/usr/local/bin/codex")
SLOTS = int(os.environ.get("CODEX_GATEWAY_SLOTS", "8"))
QUEUE_S = float(os.environ.get("CODEX_GATEWAY_QUEUE_S", "90"))
MODELS = {"gpt-6-luna", "gpt-6.1-sol"}           # what Albatross may ask for; anything else is refused
# Search runs only on GPT-5.5: the one model this CLI gives its native web search; the GPT-6 family reaches search only
# through code mode, which stays off (it would let the model run code on the server). Measured 2026-10-05.
SEARCH_MODELS = {"gpt-5.5"}
IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_IMAGE = 2_000_000
EFFORTS = {"low", "medium", "high"}
MAX_BODY = 3_500_000                             # bytes: a 1.5 MB image is 2 MB in base64, plus the prompt
MAX_TIMEOUT_S = 600
LIMITS_TTL_S = 60
# Features that add tools or instructions to the request. Turning them off is what cuts the fixed overhead; what is
# left (~3.6k tokens of tool definitions this CLI always sends for these models) is identical every call and caches.
DISABLE = ["apps", "plugins", "browser_use", "computer_use", "image_generation", "multi_agent", "shell_tool",
           "unified_exec", "view_image", "goals", "sleep_tool", "skill_search", "tool_suggest", "code_mode_host"]
CONFIG = ["include_apps_instructions=false", "include_collaboration_mode_instructions=false",
          "include_environment_context=false", "web_search=disabled", "model_verbosity=low",
          "check_for_update_on_startup=false"]
# Item types that mean the model ran or fetched something. With every tool off none should ever appear; if a CLI update
# brings one back, the run fails instead of passing silently (the answer is discarded and the call goes to OpenRouter).
FORBIDDEN = {"command_execution", "file_change", "mcp_tool_call", "web_search", "image_generation", "collab_tool_call"}
LIMIT = re.compile(r"usage limit|rate[ _-]?limit|too many requests|\b429\b|quota", re.IGNORECASE)
ENV = {"HOME": os.environ.get("HOME", "/home/codex"), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
CODEX_HOME = os.path.join(ENV["HOME"], ".codex")                  # where `codex login` keeps auth.json

CLAUDE = os.environ.get("CLAUDE_BIN", "/usr/local/bin/claude")
CLAUDE_MODELS = {"claude-haiku": "haiku", "claude-sonnet": "sonnet", "claude-opus": "opus"}   # Claude Code's aliases
CLAUDE_TOKEN = os.environ.get("CLAUDE_TOKEN_FILE", os.path.join(ENV["HOME"], ".albatross", "claude-token"))
# Everything a Claude run sees besides ENV: no telemetry, error reports, update checks or other nonessential traffic.
CLAUDE_QUIET = {"DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_ERROR_REPORTING": "1",
                "DISABLE_BUG_COMMAND": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
MAX_SCHEMA = 100_000                             # bytes: the schema goes on the command line (Linux caps one at 128 KiB)
# A Claude error that is the plan's limit: "You've hit your limit · resets 3pm (UTC)", "Claude AI usage limit
# reached|1761580800" (older CLIs). Claude Code says "not your usage limit" when the server is only throttling.
CLAUDE_LIMIT = re.compile(r"hit your\b[^.\n]*\blimit|usage limit|out of (?:extra )?usage|\|\d{10}\b", re.IGNORECASE)
NOT_LIMIT = re.compile(r"not your usage limit", re.IGNORECASE)
RESETS = re.compile(r"resets (?:at |on )?(?:(?P<mon>[A-Za-z]{3})[a-z]* (?P<day>\d{1,2}),? (?:at )?)?"
                    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)\b(?:\s*\((?P<tz>[^)]+)\))?", re.IGNORECASE)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
AUTH_ERROR = re.compile(r"\b40[13]\b|authenticat|invalid (?:api key|bearer|token)|expired|revoked|/login", re.IGNORECASE)
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~+/=-]{20,2000}")
VERIFY_S = 90                                    # the one tiny call that checks a new Claude token

LOGIN_WAIT_S = 20                                # for `codex login --device-auth` to print its link and code
LOGIN_MAX_S = 16 * 60                            # its code lasts 15 minutes
LOGIN_HOSTS = ("openai.com", "chatgpt.com")      # the only sign-in links passed on
MAX_LOGIN_BODY = 64_000
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
DEVICE_URL = re.compile(r"https://\S+")
DEVICE_CODE = re.compile(r"\b[A-Z0-9]{4,6}-[A-Z0-9]{4,6}\b")

slots = threading.BoundedSemaphore(SLOTS)
_limits: dict = {"at": 0.0, "body": None}
_limits_lock = threading.Lock()
_versions: dict = {}
_login: dict = {"state": "idle", "url": None, "code": None, "error": None, "proc": None}
_login_lock = threading.Lock()


def argv(model: str, effort: str, run_dir: str, schema: bool = True, search: bool = False,
         image: str | None = None) -> list[str]:
    out = [CODEX, "exec", "-m", model, "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "--json",
           "--ignore-user-config", "--ignore-rules", "-c", f"model_reasoning_effort={effort}",
           "-c", f"model_instructions_file={run_dir}/instructions.md"]
    for c in CONFIG:
        out += ["-c", "web_search=live" if search and c == "web_search=disabled" else c]
    for f in DISABLE:
        out += ["--disable", f]
    if image:
        out += ["-i", image]
    if schema:
        out += ["--output-schema", f"{run_dir}/schema.json"]
    return out + ["-"]                                                         # "-": the prompt comes on stdin


def web_actions(stdout: str) -> tuple[list[str], int]:
    """(every page the search opened, how many searches it ran), from the run's web_search items."""
    opened, searches = [], 0
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") != "item.completed" or item.get("type") != "web_search":
            continue
        action = item.get("action") or {}
        if action.get("type") == "open_page" and isinstance(action.get("url"), str):
            opened.append(action["url"])
        elif action.get("type") == "search":
            searches += max(1, len(action.get("queries") or []))
    return list(dict.fromkeys(opened)), searches


def parse_events(stdout: str, allow: frozenset = frozenset()) -> tuple[str | None, dict, list[str]]:
    """codex exec --json lines -> (the last agent message, the turn's token usage, error messages). An item type in
    FORBIDDEN and not in `allow` is an error (a search call allows web_search)."""
    text, usage, errors = None, {}, []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        item = ev.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text")
        elif kind == "turn.completed":
            usage = ev.get("usage") or {}
        elif kind == "error":
            errors.append(str(ev.get("message") or ""))
        elif kind == "turn.failed":
            errors.append(str((ev.get("error") or {}).get("message") or ""))
        elif kind == "item.completed" and item.get("type") == "error":
            # Notices such as "Code Mode is unavailable because code-mode host is disabled" (we disable it on purpose).
            pass
        if kind in ("item.started", "item.completed") and item.get("type") in FORBIDDEN - allow:
            errors.append(f"forbidden item: {item.get('type')}")
    return text, usage, errors


def classify(errors: list[str], returncode: int) -> str:
    return "limit" if LIMIT.search(" ".join(errors)) else "failed"


def run_exec(req: dict) -> tuple[int, dict]:
    if str(req.get("model") or "").startswith("claude-"):
        return run_claude(req)
    model, effort, search = req.get("model"), req.get("effort") or "low", req.get("search") is True
    if model not in (SEARCH_MODELS if search else MODELS) or effort not in EFFORTS:
        return 400, {"ok": False, "kind": "bad_request",
                     "error": f"model {model!r} / effort {effort!r} not allowed{' for search' if search else ''}"}
    image, ext = req.get("image_b64"), str(req.get("image_ext") or "").lower()
    if image is not None:
        try:
            if ext not in IMAGE_EXTS:
                raise ValueError(f"image type {ext!r}")
            image = base64.b64decode(image, validate=True)
            if not image or len(image) > MAX_IMAGE:
                raise ValueError(f"image of {len(image)} bytes")
        except (ValueError, TypeError) as e:
            return 400, {"ok": False, "kind": "bad_request", "error": f"image: {str(e)[:120]}"}
    instructions, text, schema = req.get("instructions"), req.get("input"), req.get("schema")
    if not isinstance(instructions, str) or not isinstance(text, str) or not isinstance(schema, (dict, type(None))):
        return 400, {"ok": False, "kind": "bad_request", "error": "instructions, input (strings), schema (object or null)"}
    try:
        timeout = min(float(req.get("timeout_s") or 150), MAX_TIMEOUT_S)
        queue = min(float(req.get("queue_s") if req.get("queue_s") is not None else QUEUE_S), QUEUE_S)
    except (TypeError, ValueError):
        return 400, {"ok": False, "kind": "bad_request", "error": "timeout_s and queue_s are numbers"}
    if not slots.acquire(timeout=queue):              # an every-minute job asks for a short wait and falls back
        return 503, {"ok": False, "kind": "busy", "error": f"all {SLOTS} slots busy for {queue:.0f}s"}
    started = time.monotonic()
    run_dir = None
    try:
        run_dir = tempfile.mkdtemp(prefix="x", dir=WORK)
        with open(f"{run_dir}/instructions.md", "w") as fh:
            fh.write(instructions)
        if schema is not None:
            with open(f"{run_dir}/schema.json", "w") as fh:
                json.dump(schema, fh)
        image_path = None
        if image:
            image_path = f"{run_dir}/image.{ext}"
            with open(image_path, "wb") as fh:
                fh.write(image)
        proc = subprocess.Popen(argv(model, effort, run_dir, schema is not None, search, image_path),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=run_dir, env=ENV, text=True, start_new_session=True)
        try:
            out, err = proc.communicate(text, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return 504, {"ok": False, "kind": "timeout", "error": f"no answer within {timeout:.0f}s"}
        answer, usage, errors = parse_events(out, frozenset({"web_search"}) if search else frozenset())
        opened, searches = web_actions(out) if search else ([], 0)
        secs = round(time.monotonic() - started, 1)
        if proc.returncode == 0 and answer and not any(e.startswith("forbidden item") for e in errors):
            return 200, {"ok": True, "text": answer, "usage": usage, "secs": secs, "opened": opened,
                         "searches": searches}
        detail = "; ".join(e for e in errors if e)[:600] or (err or "").strip()[-600:] or f"exit {proc.returncode}"
        return 502, {"ok": False, "kind": classify(errors, proc.returncode), "error": detail, "usage": usage,
                     "secs": secs}
    finally:
        slots.release()
        if run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)


# ─── Claude ────────────────────────────────────────────────────────────────────
def claude_argv(model: str, effort: str, run_dir: str, schema: str | None = None) -> list[str]:
    out = [CLAUDE, "-p", "--output-format", "json", "--model", CLAUDE_MODELS[model], "--effort", effort,
           "--system-prompt-file", f"{run_dir}/instructions.md", "--tools", "", "--strict-mcp-config",
           "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
           "--permission-mode", "dontAsk", "--max-turns", "1"]
    if schema is not None:
        out += ["--json-schema", schema]       # a StructuredOutput call, which ends the one turn (checked 2026-10-27)
    return out                                  # the prompt comes on stdin


def claude_env(token: str) -> dict:
    """A Claude run's whole environment. Nothing is inherited, so ANTHROPIC_API_KEY or any other key never reaches it."""
    return {**ENV, **CLAUDE_QUIET, "CLAUDE_CODE_OAUTH_TOKEN": token}


def claude_token() -> str | None:
    try:
        with open(CLAUDE_TOKEN) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def parse_claude(stdout: str) -> dict:
    """`claude -p --output-format json` -> its result object; {} when it printed none."""
    for chunk in [stdout, *reversed(stdout.splitlines())]:
        try:
            obj = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return {}


def claude_usage(res: dict) -> dict:
    """Claude's token counts in the Codex lane's terms: input_tokens includes the cached input."""
    u = res.get("usage")
    if not isinstance(u, dict):
        return {}
    cached = int(u.get("cache_read_input_tokens") or 0)
    return {"input_tokens": int(u.get("input_tokens") or 0) + cached + int(u.get("cache_creation_input_tokens") or 0),
            "cached_input_tokens": cached, "output_tokens": int(u.get("output_tokens") or 0)}


def claude_kind(detail: str, api_status) -> str:
    if NOT_LIMIT.search(detail):                  # the server throttling for a moment, not the plan's limit
        return "failed"
    return "limit" if api_status == 429 or CLAUDE_LIMIT.search(detail) else "failed"


def reset_at(text: str, now: float | None = None) -> int | None:
    """When a Claude limit resets (unix seconds), from its message: "...|1761580800" (older CLIs), "resets 3pm
    (Europe/London)", "resets Oct 30, 3:30pm (UTC)". A time without a zone is this machine's. None when it names no
    time this can read."""
    if m := re.search(r"\|(\d{10})\b", text):
        return int(m.group(1))
    if not (m := RESETS.search(text)):
        return None
    try:
        zone = ZoneInfo(m["tz"].strip()) if m["tz"] else datetime.now().astimezone().tzinfo
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    t0 = datetime.fromtimestamp(time.time() if now is None else now, zone)
    try:
        t = t0.replace(hour=int(m["h"]) % 12 + (12 if m["ap"].lower() == "pm" else 0), minute=int(m["m"] or 0),
                       second=0, microsecond=0)
        if m["mon"]:
            t = t.replace(month=MONTHS.index(m["mon"].lower()) + 1, day=int(m["day"]))
            if t < t0 - timedelta(days=1):
                t = t.replace(year=t.year + 1)
        elif t <= t0:
            t += timedelta(days=1)
    except ValueError:
        return None
    return int(t.timestamp())


def claude_reply(returncode: int, stdout: str, stderr: str, secs: float) -> tuple[int, dict]:
    """A finished `claude -p` run -> the /v1/exec answer. Claude Code exits 0 even on an API error, so is_error is
    what decides."""
    res = parse_claude(stdout)
    usage, cost = claude_usage(res), res.get("total_cost_usd")
    shaped = res.get("structured_output")
    text = json.dumps(shaped) if shaped is not None else res.get("result")
    if res and not res.get("is_error") and res.get("subtype") == "success" and isinstance(text, str) and text.strip():
        return 200, {"ok": True, "text": text, "usage": usage, "secs": secs, "cost_usd": cost, "opened": [],
                     "searches": 0}
    detail = ((res.get("result") if isinstance(res.get("result"), str) else "")
              or "; ".join(str(e) for e in res.get("errors") or [])
              or ("" if res.get("subtype") in (None, "success") else str(res["subtype"]))
              or stderr.strip()[-600:] or f"exit {returncode}")
    body = {"ok": False, "kind": claude_kind(detail, res.get("api_error_status")), "error": detail[:600],
            "usage": usage, "secs": secs, "cost_usd": cost}
    if body["kind"] == "limit" and (at := reset_at(detail)) is not None:
        body["resets_at"] = at
    return 502, body


def run_claude(req: dict) -> tuple[int, dict]:
    model, effort = req.get("model"), req.get("effort") or "low"
    if model not in CLAUDE_MODELS or effort not in EFFORTS:
        return 400, {"ok": False, "kind": "bad_request", "error": f"model {model!r} / effort {effort!r} not allowed"}
    if req.get("search") is True or req.get("image_b64") is not None:
        return 400, {"ok": False, "kind": "bad_request", "error": "the Claude lane takes no search or image"}
    instructions, text, schema = req.get("instructions"), req.get("input"), req.get("schema")
    if not isinstance(instructions, str) or not isinstance(text, str) or not isinstance(schema, (dict, type(None))):
        return 400, {"ok": False, "kind": "bad_request", "error": "instructions, input (strings), schema (object or null)"}
    schema_arg = json.dumps(schema) if schema is not None else None
    if schema_arg is not None and len(schema_arg) > MAX_SCHEMA:
        return 400, {"ok": False, "kind": "bad_request", "error": f"schema of {len(schema_arg)} bytes"}
    try:
        timeout = min(float(req.get("timeout_s") or 150), MAX_TIMEOUT_S)
        queue = min(float(req.get("queue_s") if req.get("queue_s") is not None else QUEUE_S), QUEUE_S)
    except (TypeError, ValueError):
        return 400, {"ok": False, "kind": "bad_request", "error": "timeout_s and queue_s are numbers"}
    token = claude_token()
    if not token:
        return 503, {"ok": False, "kind": "failed", "error": "Claude isn't signed in"}
    if not slots.acquire(timeout=queue):
        return 503, {"ok": False, "kind": "busy", "error": f"all {SLOTS} slots busy for {queue:.0f}s"}
    started = time.monotonic()
    run_dir = None
    try:
        run_dir = tempfile.mkdtemp(prefix="c", dir=WORK)
        with open(f"{run_dir}/instructions.md", "w") as fh:
            fh.write(instructions)
        os.mkdir(f"{run_dir}/cwd")                # the run's working directory: empty, the instructions outside it
        proc = subprocess.Popen(claude_argv(model, effort, run_dir, schema_arg), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=f"{run_dir}/cwd",
                                env=claude_env(token), text=True, start_new_session=True)
        try:
            out, err = proc.communicate(text, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return 504, {"ok": False, "kind": "timeout", "error": f"no answer within {timeout:.0f}s"}
        return claude_reply(proc.returncode, out, err, round(time.monotonic() - started, 1))
    finally:
        slots.release()
        if run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)


# ─── sign-in ───────────────────────────────────────────────────────────────────
def _quiet(argv: list[str], timeout: float = 20) -> int | None:
    """A CLI's exit code, its output thrown away (`codex login status` prints a key's start when it holds one)."""
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              cwd=ENV["HOME"], env=ENV, timeout=timeout).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def _remove(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def write_secret(path: str, text: str) -> None:
    """A login file only this user can read (0600), written whole or not at all."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        _remove(tmp)
        raise


def cli_version(path: str) -> str | None:
    """`<cli> --version` -> its version number, cached until the binary changes."""
    try:
        key = (path, os.stat(path).st_mtime)
    except OSError:
        return None
    if key not in _versions:
        try:
            out = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 cwd=ENV["HOME"], env=ENV, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = re.search(r"\d+\.\d+\.\d+[\w.-]*", out)
        _versions[key] = m.group(0) if m else None
    return _versions[key]


def codex_plan() -> str | None:
    """The ChatGPT plan's name (the chatgpt_plan_type claim in the login's ID token). Nothing else is read from it."""
    try:
        with open(os.path.join(CODEX_HOME, "auth.json")) as fh:
            tok = (json.load(fh).get("tokens") or {}).get("id_token") or ""
        if isinstance(tok, dict):
            tok = tok.get("raw_jwt") or ""
        part = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        plan = (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type")
    except (OSError, ValueError, IndexError, AttributeError, TypeError):
        return None
    return plan if isinstance(plan, str) and re.fullmatch(r"[\w-]{1,32}", plan) else None


def cli_status() -> dict:
    codex_ok, claude_ok = os.access(CODEX, os.X_OK), os.access(CLAUDE, os.X_OK)
    signed_in = codex_ok and _quiet([CODEX, "login", "status"]) == 0
    return {"codex": {"installed": codex_ok, "version": cli_version(CODEX) if codex_ok else None,
                      "signed_in": signed_in, "plan": codex_plan() if signed_in else None},
            "claude": {"installed": claude_ok, "version": cli_version(CLAUDE) if claude_ok else None,
                       "signed_in": claude_ok and claude_token() is not None}}


def login_view() -> dict:
    return {k: _login[k] for k in ("state", "url", "code", "error")}


def sign_in_link(line: str) -> str | None:
    m = DEVICE_URL.search(line)
    host = (urlsplit(m.group(0)).hostname or "") if m else ""
    return m.group(0) if any(host == h or host.endswith("." + h) for h in LOGIN_HOSTS) else None


def _follow_login(proc: subprocess.Popen, shown: threading.Event) -> None:
    """Read the device sign-in's output for its link and code, then record how it ended."""
    timer = threading.Timer(LOGIN_MAX_S, _kill, (proc,))
    timer.daemon = True
    timer.start()
    tail: list[str] = []
    for raw in proc.stdout:
        line = ANSI.sub("", raw).strip()
        if not line:
            continue
        tail = (tail + [line])[-3:]
        with _login_lock:
            if _login["proc"] is not proc:
                continue
            if not _login["url"] and (url := sign_in_link(line)):
                _login["url"] = url
            if not _login["code"] and (m := DEVICE_CODE.search(line)):
                _login["code"] = m.group(0)
            if _login["url"] and _login["code"]:
                shown.set()
    code = proc.wait()
    timer.cancel()
    with _login_lock:
        if _login["proc"] is proc and _login["state"] == "pending":
            _login.update(state="done" if code == 0 else "failed", url=None, code=None,
                          error=None if code == 0 else
                          "Codex sign-in didn't finish" + (f": {tail[-1][:200]}" if tail else "."))
    shown.set()


def codex_login_start() -> tuple[int, dict]:
    """Start `codex login --device-auth` (or return the one still waiting) -> its link and code once it shows them."""
    if not os.access(CODEX, os.X_OK):
        return 503, {"state": "failed", "url": None, "code": None, "error": "The Codex CLI isn't installed here."}
    with _login_lock:
        if _login["state"] == "pending" and _login["proc"] is not None and _login["proc"].poll() is None:
            return 200, login_view()
        proc = subprocess.Popen([CODEX, "login", "--device-auth"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, cwd=ENV["HOME"], env=ENV, text=True, start_new_session=True)
        shown = threading.Event()
        _login.update(state="pending", url=None, code=None, error=None, proc=proc)
        threading.Thread(target=_follow_login, args=(proc, shown), daemon=True).start()
    shown.wait(LOGIN_WAIT_S)
    with _login_lock:
        if _login["proc"] is proc and _login["state"] == "pending" and not (_login["url"] and _login["code"]):
            _login.update(state="failed", url=None, code=None,
                          error="Codex didn't show a sign-in code. Try again in a minute.")
            _kill(proc)
        return 200, login_view()


def codex_auth_json(req: dict) -> tuple[int, dict]:
    """Sign Codex in with an auth.json from `codex login` run on another computer."""
    raw = req.get("auth_json")
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_LOGIN_BODY:
        return 400, {"ok": False, "error": "Paste the whole auth.json file."}
    try:
        doc = json.loads(raw)
    except ValueError:
        return 400, {"ok": False, "error": "That isn't valid JSON. Paste the whole auth.json file."}
    tokens = doc.get("tokens") if isinstance(doc, dict) else None
    if not isinstance(tokens, dict) or not (tokens.get("refresh_token") or tokens.get("access_token")):
        return 400, {"ok": False, "error": "That isn't a ChatGPT sign-in. Use the auth.json in ~/.codex on the computer "
                                           "where you ran codex login."}
    if not os.access(CODEX, os.X_OK):
        return 503, {"ok": False, "error": "The Codex CLI isn't installed here."}
    path = os.path.join(CODEX_HOME, "auth.json")
    write_secret(path, raw.strip() + "\n")
    if _quiet([CODEX, "login", "status"]) != 0:
        _remove(path)
        return 400, {"ok": False, "error": "Codex didn't accept that auth.json. Run codex login again and paste the new "
                                           "file."}
    return 200, {"ok": True, "error": None}


def codex_logout() -> tuple[int, dict]:
    with _login_lock:
        if _login["proc"] is not None and _login["proc"].poll() is None:
            _kill(_login["proc"])
        _login.update(state="idle", url=None, code=None, error=None, proc=None)
    if os.access(CODEX, os.X_OK):
        _quiet([CODEX, "logout"])
    _remove(os.path.join(CODEX_HOME, "auth.json"))
    return 200, {"ok": True}


def claude_login(req: dict) -> tuple[int, dict]:
    """Keep a token from `claude setup-token` once one tiny call with it works; otherwise delete it and say why."""
    token = req.get("token").strip() if isinstance(req.get("token"), str) else ""
    if not TOKEN_SHAPE.fullmatch(token):
        return 400, {"ok": False, "error": "That doesn't look like a token from claude setup-token."}
    if not os.access(CLAUDE, os.X_OK):
        return 503, {"ok": False, "error": "The Claude Code CLI isn't installed here."}
    write_secret(CLAUDE_TOKEN, token + "\n")
    _, body = run_claude({"model": "claude-haiku", "effort": "low", "instructions": "Reply OK.", "input": "Reply OK",
                          "schema": None, "timeout_s": VERIFY_S, "queue_s": 30})
    if body.get("ok") or body.get("kind") == "limit":      # a plan at its limit still took the token
        return 200, {"ok": True, "error": None}
    _remove(CLAUDE_TOKEN)
    detail = str(body.get("error") or "").replace(token, "[token]")
    if body.get("kind") == "timeout":
        why = "Claude didn't answer the check in time, so the token wasn't saved. Try again."
    elif body.get("kind") == "busy":
        why = "The gateway is busy with agent calls, so the token wasn't checked. Try again in a minute."
    elif AUTH_ERROR.search(detail):
        why = "Claude didn't accept that token. Run claude setup-token again and paste the new token."
    else:
        why = f"The check call with that token failed, so it wasn't saved: {detail[:200]}"
    return 200, {"ok": False, "error": why}


def claude_logout() -> tuple[int, dict]:
    _remove(CLAUDE_TOKEN)
    return 200, {"ok": True}


def window(w: dict | None) -> dict | None:
    if not w or w.get("usedPercent") is None:
        return None
    return {"used_percent": w.get("usedPercent"), "window_mins": w.get("windowDurationMins"),
            "resets_at": w.get("resetsAt")}


def read_limits(timeout: float = 25.0) -> dict:
    """Every usage window through `codex app-server` (JSON-RPC over stdio): account/rateLimits/read."""
    os.makedirs(WORK, exist_ok=True)
    proc = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd=WORK, env=ENV, start_new_session=True)
    try:
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"clientInfo": {"name": "albatross", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}]
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in msgs).encode())
        proc.stdin.flush()
        fd, buf = proc.stdout.fileno(), b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") != 2:
                    continue
                if "error" in msg:
                    raise RuntimeError(str(msg["error"])[:300])
                return limits_body((msg.get("result") or {}).get("rateLimits") or {})
        raise RuntimeError(f"no rate-limit answer within {timeout:.0f}s")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def limits_body(rl: dict) -> dict:
    return {"windows": [w for w in (window(rl.get("primary")), window(rl.get("secondary"))) if w],
            "plan": rl.get("planType"), "limit_reached": rl.get("rateLimitReachedType")}


def limits() -> tuple[int, dict]:
    with _limits_lock:
        if _limits["body"] is not None and time.monotonic() - _limits["at"] < LIMITS_TTL_S:
            return 200, _limits["body"]
        try:
            body = read_limits()
        except Exception as e:  # noqa: BLE001 - reported to the caller, which then keeps the lane off
            return 502, {"ok": False, "kind": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}"}
        _limits.update(at=time.monotonic(), body=body)
        return 200, body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self) -> str:             # Unix socket peers have no address
        return "unix"

    def log_message(self, fmt: str, *args) -> None:   # one line per request goes to the journal from _reply
        pass

    def _reply(self, status: int, body: dict, note: str) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        print(f"{self.command} {self.path} {status} {note}", file=sys.stderr, flush=True)

    def _body(self, limit: int) -> dict | None:
        """The request's JSON object; None once an error reply went out."""
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0 or size > limit:
            self._reply(413, {"ok": False, "kind": "bad_request", "error": f"body of {size} bytes"}, "")
            return None
        try:
            req = json.loads(self.rfile.read(size))
        except ValueError:
            req = None
        if not isinstance(req, dict):
            self._reply(400, {"ok": False, "kind": "bad_request", "error": "body is not a JSON object"}, "")
            return None
        return req

    def _drain(self) -> None:                    # a body nobody reads would spoil the next request on the connection
        size = int(self.headers.get("Content-Length") or 0)
        if 0 < size <= MAX_LOGIN_BODY:
            self.rfile.read(size)

    def _not_found(self) -> None:
        self._reply(404, {"ok": False, "kind": "bad_request", "error": "not found"}, "")

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._reply(200, {"ok": True, "slots": SLOTS}, "")
        elif self.path == "/v1/limits":
            status, body = limits()
            self._reply(status, body, f"windows={[(w['window_mins'], w['used_percent']) for w in body.get('windows', [])]}")
        elif self.path == "/v1/status":
            body = cli_status()
            self._reply(200, body, f"codex={body['codex']['signed_in']} claude={body['claude']['signed_in']}")
        elif self.path == "/v1/login/codex":
            with _login_lock:
                body = login_view()
            self._reply(200, body, f"state={body['state']}")
        else:
            self._not_found()

    def do_POST(self) -> None:
        # Sign-in replies are logged by outcome only: never a token, an auth.json or a sign-in code.
        if self.path == "/v1/login/codex/start":
            self._drain()
            status, body = codex_login_start()
            self._reply(status, body, f"state={body['state']}")
            return
        login = {"/v1/login/codex/auth-json": codex_auth_json, "/v1/login/claude": claude_login}.get(self.path)
        if login is not None:
            if (req := self._body(MAX_LOGIN_BODY)) is not None:
                status, body = login(req)
                self._reply(status, body, f"ok={body['ok']}")
            return
        if self.path != "/v1/exec":
            self._not_found()
            return
        if (req := self._body(MAX_BODY)) is None:
            return
        status, body = run_exec(req)
        u = body.get("usage") or {}
        self._reply(status, body, f"model={req.get('model')} effort={req.get('effort')} secs={body.get('secs')} "
                                  f"in={u.get('input_tokens')} cached={u.get('cached_input_tokens')} "
                                  f"out={u.get('output_tokens')} kind={body.get('kind', 'ok')}")

    def do_DELETE(self) -> None:
        signout = {"/v1/login/codex": codex_logout, "/v1/login/claude": claude_logout}.get(self.path)
        self._drain()
        if signout is None:
            self._not_found()
            return
        status, body = signout()
        self._reply(status, body, "signed out")


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def server_bind(self) -> None:
        try:
            os.unlink(self.server_address)
        except FileNotFoundError:
            pass
        super().server_bind()
        os.chmod(self.server_address, 0o660)


def main() -> None:
    os.makedirs(WORK, mode=0o700, exist_ok=True)
    for stale in os.listdir(WORK):                    # run folders a killed gateway left behind
        shutil.rmtree(os.path.join(WORK, stale), ignore_errors=True)
    with Server(SOCK, Handler) as srv:
        print(f"subscriptions gateway on {SOCK}, {SLOTS} slots", file=sys.stderr, flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
