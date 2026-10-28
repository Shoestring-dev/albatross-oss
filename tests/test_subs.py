"""Agents on your own ChatGPT or Claude plan: the gateway's Claude backend and sign-in (deploy/codex_gateway.py), the
Claude lane (albatross/llm/claude.py), AGENTS_ON routing in openrouter.structured() and the Setup page's API
(albatross/api/subs.py). Fake CLIs and a fake gateway: no network, no database, no real login."""

import asyncio
import base64
import importlib.util
import json
import os
import socket
import shutil
import stat
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from albatross import db
from albatross.api import subs
from albatross.config import settings
from albatross.deepend import fit, reddit
from albatross.llm import claude, codex, openrouter

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("subs_gateway", ROOT / "deploy" / "codex_gateway.py")
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

TOKEN = "sk-ant-oat01-" + "x" * 40          # a made-up token in the shape `claude setup-token` prints


class Answer(BaseModel):
    x: int


def script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """The gateway's user: a home of its own, a work folder, the token file and auth.json inside the home."""
    h = tmp_path / "home"
    (h / "work").mkdir(parents=True)
    monkeypatch.setattr(gw, "ENV", {"HOME": str(h), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    monkeypatch.setattr(gw, "WORK", str(h / "work"))
    monkeypatch.setattr(gw, "CODEX_HOME", str(h / ".codex"))
    monkeypatch.setattr(gw, "CLAUDE_TOKEN", str(h / ".albatross" / "claude-token"))
    monkeypatch.setattr(gw, "_login", {"state": "idle", "url": None, "code": None, "error": None, "proc": None})
    monkeypatch.setattr(gw, "_versions", {})
    return h


def result(**kw) -> str:
    """One line of `claude -p --output-format json`, as Claude Code 2.1.312 prints it."""
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "",
            "total_cost_usd": 0.0123, "api_error_status": None,
            "usage": {"input_tokens": 1200, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 100,
                      "output_tokens": 40}}
    return json.dumps(base | kw)


def fake_claude(tmp_path: Path, reply: str, log: Path | None = None) -> str:
    """A claude that records its argv, env, cwd and stdin, then prints `reply`."""
    log = log or tmp_path / "claude-call.json"
    return script(tmp_path / "claude", f"""#!{sys.executable}
import json, os, sys
json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "files": os.listdir("."),
           "stdin": sys.stdin.read()}}, open({str(log)!r}, "w"))
print({reply!r})
""")


# ─── the gateway: Claude runs ────────────────────────────────────────────────────
def test_claude_runs_with_every_tool_mcp_and_settings_file_off():
    a = gw.claude_argv("claude-sonnet", "low", "/w/c1", '{"type": "object"}')
    assert a[:2] == [gw.CLAUDE, "-p"]
    pairs = {a[i]: a[i + 1] for i in range(len(a) - 1) if a[i].startswith("--")}
    assert pairs["--output-format"] == "json" and pairs["--model"] == "sonnet" and pairs["--effort"] == "low"
    assert pairs["--tools"] == "" and pairs["--setting-sources"] == "" and pairs["--max-turns"] == "1"
    assert pairs["--system-prompt-file"] == "/w/c1/instructions.md" and pairs["--permission-mode"] == "dontAsk"
    assert pairs["--json-schema"] == '{"type": "object"}'
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in a
    assert "--mcp-config" not in a and "--bare" not in a and "--dangerously-skip-permissions" not in a
    assert "--json-schema" not in gw.claude_argv("claude-haiku", "low", "/w/c1")
    assert gw.claude_argv("claude-opus", "high", "/w/c1")[5] == "opus"


def test_claude_sees_only_its_own_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-never-pass")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-should-never-pass")
    env = gw.claude_env(TOKEN)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "ANTHROPIC_API_KEY" not in env and "OPENROUTER_API_KEY" not in env
    for k in ("DISABLE_TELEMETRY", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        assert env[k] == "1"
    assert set(env) == set(gw.ENV) | set(gw.CLAUDE_QUIET) | {"CLAUDE_CODE_OAUTH_TOKEN"}


def test_a_structured_answer_comes_back_in_the_codex_shape():
    status, body = gw.claude_reply(0, result(structured_output={"x": 7}, result='{"x":7}'), "", 2.5)
    assert status == 200 and json.loads(body["text"]) == {"x": 7} and body["cost_usd"] == 0.0123
    assert body["usage"] == {"input_tokens": 4300, "cached_input_tokens": 3000, "output_tokens": 40}
    assert body["opened"] == [] and body["searches"] == 0 and body["secs"] == 2.5
    assert gw.claude_reply(0, result(result="OK"), "", 1)[1]["text"] == "OK"        # no schema: the text


@pytest.mark.parametrize("res,kind", [
    (result(is_error=True, api_error_status=401, result="Failed to authenticate. API Error: 401 Invalid bearer token"),
     "failed"),
    (result(is_error=True, api_error_status=429, result="API Error: Server is temporarily limiting requests "
                                                        "(not your usage limit) · rate limited"), "failed"),
    (result(is_error=True, api_error_status=429, result="You've hit your limit · resets 3pm (UTC)"), "limit"),
    (result(is_error=True, result="Claude AI usage limit reached|1793000000"), "limit"),
    (result(is_error=True, subtype="error_max_turns", result=None, errors=["Reached maximum number of turns (1)"]),
     "failed"),
])
def test_claude_errors_are_classified(res, kind):
    status, body = gw.claude_reply(0, res, "", 1.0)               # Claude Code exits 0 even on an API error
    assert status == 502 and body["ok"] is False and body["kind"] == kind and body["error"]


def test_a_run_that_printed_nothing_reports_stderr():
    status, body = gw.claude_reply(1, "", "boom: no such file\n", 0.3)
    assert status == 502 and body == {"ok": False, "kind": "failed", "error": "boom: no such file", "usage": {},
                                      "secs": 0.3, "cost_usd": None}


def test_a_limit_carries_its_reset_time():
    _, body = gw.claude_reply(0, result(is_error=True, result="Claude AI usage limit reached|1793000000"), "", 1)
    assert body["resets_at"] == 1793000000
    now = datetime(2026, 10, 27, 14, 0, tzinfo=UTC).timestamp()
    assert gw.reset_at("You've hit your limit · resets 3pm (UTC)", now) == now + 3600
    assert gw.reset_at("You've hit your limit · resets 1pm (UTC)", now) == now + 23 * 3600     # tomorrow
    assert gw.reset_at("You've hit your weekly limit · resets Oct 30, 3:30pm (UTC)", now) == \
        datetime(2026, 10, 30, 15, 30, tzinfo=UTC).timestamp()
    assert gw.reset_at("resets 12am (Europe/London)", now) == datetime(2026, 10, 28, 0, 0, tzinfo=UTC).timestamp()
    assert gw.reset_at("resets 3pm (Not/AZone)", now) == now + 3600                            # an unknown zone: UTC
    assert gw.reset_at("You've hit your limit", now) is None


def test_a_claude_run_in_an_empty_folder_with_the_prompt_on_stdin(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CLAUDE", fake_claude(tmp_path, result(structured_output={"x": 3})))
    gw.write_secret(gw.CLAUDE_TOKEN, TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-never-pass")
    status, body = gw.run_exec({"model": "claude-sonnet", "effort": "medium", "instructions": "be brief",
                                "input": "x" * 200_000, "schema": Answer.model_json_schema()})
    assert status == 200 and json.loads(body["text"]) == {"x": 3}
    call = json.loads((tmp_path / "claude-call.json").read_text())
    assert call["stdin"] == "x" * 200_000 and call["files"] == []                     # nothing in its folder
    assert call["argv"][call["argv"].index("--model") + 1] == "sonnet"
    assert json.loads(call["argv"][call["argv"].index("--json-schema") + 1]) == Answer.model_json_schema()
    prompt_file = call["argv"][call["argv"].index("--system-prompt-file") + 1]
    assert not prompt_file.startswith(call["cwd"])                                     # the instructions sit outside it
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and "ANTHROPIC_API_KEY" not in call["env"]
    assert os.listdir(gw.WORK) == []                                                   # the run folder is gone


def test_claude_refuses_what_it_cant_do_and_waits_for_a_sign_in(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CLAUDE", fake_claude(tmp_path, result(result="OK")))
    base = {"model": "claude-haiku", "effort": "low", "instructions": "i", "input": "u", "schema": None}
    assert gw.run_exec(base | {"model": "claude-fable"})[0] == 400
    assert gw.run_exec(base | {"search": True})[0] == 400
    assert gw.run_exec(base | {"image_b64": "aGk=", "image_ext": "png"})[0] == 400
    assert gw.run_exec(base | {"schema": {"p": "x" * gw.MAX_SCHEMA}})[0] == 400
    status, body = gw.run_exec(base)                                                   # no token yet
    assert status == 503 and body["error"] == "Claude isn't signed in"
    assert not (tmp_path / "claude-call.json").exists()


def test_a_stuck_claude_run_is_killed(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CLAUDE", script(tmp_path / "claude", "#!/bin/sh\nsleep 30\n"))
    gw.write_secret(gw.CLAUDE_TOKEN, TOKEN)
    status, body = gw.run_exec({"model": "claude-haiku", "effort": "low", "instructions": "i", "input": "",
                                "schema": None, "timeout_s": 1})
    assert status == 504 and body["kind"] == "timeout"


# ─── the gateway: sign-in ─────────────────────────────────────────────────────────
def test_a_claude_token_is_kept_only_once_a_call_with_it_works(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CLAUDE", fake_claude(tmp_path, result(result="OK")))
    assert gw.claude_login({"token": TOKEN}) == (200, {"ok": True, "error": None})
    assert stat.S_IMODE(os.stat(gw.CLAUDE_TOKEN).st_mode) == 0o600 and gw.claude_token() == TOKEN
    call = json.loads((tmp_path / "claude-call.json").read_text())
    assert call["argv"][call["argv"].index("--model") + 1] == "haiku" and "--json-schema" not in call["argv"]
    assert gw.claude_logout() == (200, {"ok": True}) and gw.claude_token() is None


def test_a_refused_claude_token_is_deleted_with_a_plain_reason(home, tmp_path, monkeypatch):
    bad = result(is_error=True, api_error_status=401, result=f"Failed to authenticate. API Error: 401 {TOKEN} invalid")
    monkeypatch.setattr(gw, "CLAUDE", fake_claude(tmp_path, bad))
    status, body = gw.claude_login({"token": TOKEN})
    assert body == {"ok": False, "error": "Claude didn't accept that token. Run claude setup-token again and paste "
                                          "the new token."}
    assert not os.path.exists(gw.CLAUDE_TOKEN)
    assert gw.claude_login({"token": "not a token"})[1]["ok"] is False
    assert gw.claude_login({"token": 5})[0] == 400


def test_a_claude_token_at_the_plans_limit_still_signs_in(home, tmp_path, monkeypatch):
    limit = result(is_error=True, api_error_status=429, result="You've hit your limit · resets 3pm (UTC)")
    monkeypatch.setattr(gw, "CLAUDE", fake_claude(tmp_path, limit))
    assert gw.claude_login({"token": TOKEN})[1] == {"ok": True, "error": None}


def test_other_claude_check_failures_never_echo_the_token(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CLAUDE", fake_claude(tmp_path, result(is_error=True, result=f"odd failure for {TOKEN}")))
    _, body = gw.claude_login({"token": TOKEN})
    assert body["ok"] is False and TOKEN not in body["error"] and "[token]" in body["error"]


def jwt(claims: dict) -> str:
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{part}.sig"


def fake_codex(tmp_path: Path, *, signed_in: bool = True, device: str = "", device_exit: int = 0) -> str:
    """A codex that answers --version, login status, logout and login --device-auth."""
    return script(tmp_path / "codex", f"""#!/bin/sh
case "$1 $2" in
  "--version ") echo "codex-cli 0.165.0" ;;
  "login status") {"echo 'Logged in using ChatGPT'; exit 0" if signed_in else "echo 'Not logged in'; exit 1"} ;;
  "logout ") rm -f "$HOME/.codex/auth.json"; echo "Successfully logged out" ;;
  "login --device-auth") {device or ":"}; exit {device_exit} ;;
esac
""")


AUTH = {"OPENAI_API_KEY": None, "tokens": {"id_token": jwt({"email": "someone@example.com",
                                                            "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}),
                                           "access_token": "at-secret", "refresh_token": "rt-secret"}}


def test_an_auth_json_from_elsewhere_signs_codex_in(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path))
    assert gw.codex_auth_json({"auth_json": json.dumps(AUTH)}) == (200, {"ok": True, "error": None})
    path = Path(gw.CODEX_HOME) / "auth.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and json.loads(path.read_text()) == AUTH
    assert gw.codex_plan() == "plus"                                    # the plan's name; never the email
    st = gw.cli_status()["codex"]
    assert st == {"installed": True, "version": "0.165.0", "signed_in": True, "plan": "plus"}


@pytest.mark.parametrize("raw,why", [
    ("{not json", "isn't valid JSON"),
    (json.dumps({"OPENAI_API_KEY": "sk-proj-1"}), "isn't a ChatGPT sign-in"),
    (json.dumps(["tokens"]), "isn't a ChatGPT sign-in"),
    ("", "Paste the whole"),
])
def test_a_bad_auth_json_is_refused_and_never_written(home, tmp_path, monkeypatch, raw, why):
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path))
    status, body = gw.codex_auth_json({"auth_json": raw})
    assert status == 400 and why in body["error"] and not (Path(gw.CODEX_HOME) / "auth.json").exists()


def test_an_auth_json_codex_rejects_is_removed(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path, signed_in=False))
    status, body = gw.codex_auth_json({"auth_json": json.dumps(AUTH)})
    assert status == 400 and "didn't accept" in body["error"] and not (Path(gw.CODEX_HOME) / "auth.json").exists()


DEVICE = ("printf 'Welcome to Codex\\n\\n1. Open this link in your browser and sign in to your account\\n"
          "   \\033[94mhttps://auth.openai.com/codex/device\\033[0m\\n\\n2. Enter this one-time code "
          "\\033[90m(expires in 15 minutes)\\033[0m\\n   \\033[94mLC5W-88WTZ\\033[0m\\n'")


def test_the_device_sign_in_shows_its_link_and_code_and_finishes_in_the_background(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path, device=DEVICE + "; sleep 1"))
    status, body = gw.codex_login_start()
    assert status == 200 and body == {"state": "pending", "url": "https://auth.openai.com/codex/device",
                                      "code": "LC5W-88WTZ", "error": None}
    assert gw.codex_login_start()[1] == body                      # a second start while waiting: the same code
    for _ in range(50):
        if gw.login_view()["state"] != "pending":
            break
        time.sleep(0.1)
    assert gw.login_view() == {"state": "done", "url": None, "code": None, "error": None}


def test_a_device_sign_in_that_fails_says_so(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path, device="echo 'Error: device code login is not enabled'",
                                                device_exit=1))
    status, body = gw.codex_login_start()
    assert body["state"] == "failed" and body["error"] == ("Codex sign-in didn't finish: Error: device code login "
                                                           "is not enabled")


def test_a_device_sign_in_without_a_code_gives_up(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "LOGIN_WAIT_S", 1)
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path, device="echo 'Open https://evil.example/device'; "
                                                                 "echo 'code AB12-CD34'; sleep 30"))
    body = gw.codex_login_start()[1]                              # a link to another site is never passed on
    assert body["state"] == "failed" and body["url"] is None and "didn't show a sign-in code" in body["error"]


def test_signing_codex_out_stops_a_waiting_sign_in(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path, device=DEVICE + "; sleep 30"))
    assert gw.codex_login_start()[1]["state"] == "pending"
    proc = gw._login["proc"]
    (Path(gw.CODEX_HOME)).mkdir(parents=True, exist_ok=True)
    (Path(gw.CODEX_HOME) / "auth.json").write_text("{}")
    assert gw.codex_logout() == (200, {"ok": True})
    assert proc.wait(timeout=5) is not None and gw.login_view()["state"] == "idle"
    assert not (Path(gw.CODEX_HOME) / "auth.json").exists()


def test_status_when_nothing_is_installed(home, tmp_path, monkeypatch):
    monkeypatch.setattr(gw, "CODEX", str(tmp_path / "nope"))
    monkeypatch.setattr(gw, "CLAUDE", str(tmp_path / "nope"))
    assert gw.cli_status() == {"codex": {"installed": False, "version": None, "signed_in": False, "plan": None},
                               "claude": {"installed": False, "version": None, "signed_in": False}}


def unix_get(sock: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    with httpx.Client(transport=httpx.HTTPTransport(uds=sock), timeout=10) as c:
        r = c.request(method, f"http://gw{path}", json=body)
    return r.status_code, r.json()


def test_the_socket_server_routes_and_never_logs_a_token(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gw, "CODEX", fake_codex(tmp_path))
    monkeypatch.setattr(gw, "CLAUDE", fake_claude(tmp_path, result(result="OK")))
    short = tempfile.mkdtemp(prefix="gw")                            # a Unix socket path is capped at ~100 bytes
    sock = os.path.join(short, "gw.sock")
    srv = gw.Server(sock, gw.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert stat.S_IMODE(os.stat(sock).st_mode) == 0o660
        assert unix_get(sock, "POST", "/v1/login/claude", {"token": TOKEN}) == (200, {"ok": True, "error": None})
        status, st = unix_get(sock, "GET", "/v1/status")
        assert status == 200 and st["claude"]["signed_in"] is True and st["codex"]["version"] == "0.165.0"
        assert unix_get(sock, "GET", "/v1/login/codex")[1]["state"] == "idle"
        assert unix_get(sock, "DELETE", "/v1/login/claude") == (200, {"ok": True})
        assert unix_get(sock, "DELETE", "/v1/login/nobody")[0] == 404
        assert unix_get(sock, "POST", "/v1/login/claude", None)[0] == 413
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(short, ignore_errors=True)
    err = capsys.readouterr().err
    assert "POST /v1/login/claude 200 ok=True" in err and TOKEN not in err


# ─── the Claude lane ──────────────────────────────────────────────────────────────
class Pool:
    def __init__(self, spent: float = 0.0):
        self.runs, self.spent = [], spent

    async def fetchval(self, sql, *args):
        if "INSERT INTO agent_runs" in sql:
            self.runs.append({"agent": args[0], "model": args[1], "input_tokens": args[3], "ok": args[5],
                              "error": args[6], "output": json.loads(args[8])})
            return len(self.runs)
        assert "subscription_equiv_cost_usd" in sql and "claude:%" in sql
        return self.spent


def lane(monkeypatch, reply, *, spent: float = 0.0):
    pool, sent = Pool(spent), []

    async def fake_pool():
        return pool

    async def fake_gateway(method, path, body=None, timeout=30):
        sent.append(body)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(db, "pool", fake_pool)
    monkeypatch.setattr(codex, "gateway", fake_gateway)
    monkeypatch.setattr(claude, "_blocked_until", 0.0)
    monkeypatch.setattr(settings, "claude_enabled", True)
    return pool, sent


def call_lane(**kw):
    return asyncio.run(claude.structured("t", Answer, "sys", "user", model="claude-sonnet", effort="low",
                                         deadline_s=60, daily_budget_usd=1, **kw))


OK = (200, {"ok": True, "text": '{"x": 3}', "cost_usd": 0.02,
            "usage": {"input_tokens": 4000, "cached_input_tokens": 3000, "output_tokens": 20}, "secs": 3.1})


def test_a_call_on_the_claude_plan_is_logged_with_its_list_price(monkeypatch):
    pool, sent = lane(monkeypatch, OK)
    assert call_lane() == (Answer(x=3), 1)
    assert sent[0]["model"] == "claude-sonnet" and sent[0]["instructions"] == "sys" and sent[0]["input"] == "user"
    assert sent[0]["schema"] == Answer.model_json_schema() and sent[0]["queue_s"] == claude.QUEUE_S
    assert pool.runs == [{"agent": "t", "model": "claude:sonnet", "input_tokens": 4000, "ok": True, "error": None,
                          "output": {"result": {"x": 3}, "subscription_equiv_cost_usd": 0.02, "raw_tail": None}}]


def test_a_stray_draft_before_the_answer_is_dropped(monkeypatch):
    lane(monkeypatch, (200, {"ok": True, "text": 'Draft {"x": 1} then the answer {"x": 2}', "usage": {}}))
    assert call_lane()[0] == Answer(x=2)


def test_an_answer_that_fails_the_schema_is_logged_then_falls_back(monkeypatch):
    pool, _ = lane(monkeypatch, (200, {"ok": True, "text": 'no json here', "usage": {"input_tokens": 10}}))
    with pytest.raises(claude.Unavailable, match="no JSON object"):
        call_lane()
    assert pool.runs[0]["ok"] is False and pool.runs[0]["output"]["raw_tail"] == "no json here"


def test_the_plans_limit_rests_the_lane_until_it_resets(monkeypatch):
    at = int(time.time()) + 3600
    pool, sent = lane(monkeypatch, (502, {"ok": False, "kind": "limit", "error": "You've hit your limit",
                                          "resets_at": at, "usage": {}}))
    with pytest.raises(claude.Unavailable, match="limit"):
        call_lane()
    assert claude._blocked_until == at and pool.runs[0]["ok"] is False
    with pytest.raises(claude.Unavailable, match="waiting until it resets"):
        call_lane()
    assert len(sent) == 1                                            # resting: the gateway isn't asked again


def test_the_cooldown_takes_only_a_sane_reset_time():
    now = 1_800_000_000.0
    assert claude.cooldown(now + 600, now) == now + 600
    assert claude.cooldown(None, now) == now + claude.LIMIT_COOLDOWN_S
    assert claude.cooldown(now - 5, now) == now + claude.LIMIT_COOLDOWN_S              # already past
    assert claude.cooldown(now + 30 * 86400, now) == now + claude.LIMIT_COOLDOWN_S     # a misread
    assert claude.cooldown("soon", now) == now + claude.LIMIT_COOLDOWN_S


def test_the_claude_plan_has_its_own_cap(monkeypatch):
    lane(monkeypatch, OK, spent=10.5)                                # over 10x the $1 budget at list price
    with pytest.raises(claude.Unavailable, match="cap"):
        call_lane()


def test_the_lane_is_off_until_switched_on(monkeypatch):
    _, sent = lane(monkeypatch, OK)
    monkeypatch.setattr(settings, "claude_enabled", False)
    with pytest.raises(claude.Unavailable, match="CLAUDE_ENABLED"):
        call_lane()
    assert sent == []


@pytest.mark.parametrize("reply", [(503, {"ok": False, "kind": "failed", "error": "Claude isn't signed in"}),
                                   (503, {"ok": False, "kind": "busy", "error": "all 8 slots busy"}),
                                   (400, {"ok": False, "kind": "bad_request", "error": "model"})])
def test_a_call_that_never_ran_leaves_no_row(monkeypatch, reply):
    pool, _ = lane(monkeypatch, reply)
    with pytest.raises(claude.Unavailable):
        call_lane()
    assert pool.runs == []


def test_an_unreachable_gateway_falls_back(monkeypatch):
    lane(monkeypatch, httpx.ConnectError("no socket"))
    with pytest.raises(claude.Unavailable, match="unreachable"):
        call_lane()


def test_the_model_setting_maps_to_the_gateways_names():
    assert claude.gateway_model("sonnet") == "claude-sonnet" and claude.gateway_model(" Opus ") == "claude-opus"
    assert claude.gateway_model("fable") is None and claude.gateway_model(None) is None


# ─── AGENTS_ON routing in openrouter.structured ─────────────────────────────────────
def route(monkeypatch, *, on: str, model: str, claude_reply=None, codex_reply=None, key: str | None = "k",
          experiment: bool = False, plan_only: bool = False, claude_on: bool = True, codex_on: bool = True):
    calls = []

    async def fake_claude(agent, schema, system, user, **kw):
        calls.append(("claude", kw))
        if isinstance(claude_reply, Exception):
            raise claude_reply
        return claude_reply

    async def fake_codex(agent, schema, system, user, **kw):
        calls.append(("codex", kw))
        if isinstance(codex_reply, Exception):
            raise codex_reply
        return codex_reply

    async def fake_attempt(agent, schema, messages, input_hash, **kw):
        calls.append(("openrouter", kw))
        return Answer(x=9), 7, None, False, set()

    async def spent(agent):
        return 0.0

    monkeypatch.setattr(claude, "structured", fake_claude)
    monkeypatch.setattr(codex, "structured", fake_codex)
    monkeypatch.setattr(openrouter, "_attempt", fake_attempt)
    monkeypatch.setattr(openrouter, "spent_today", spent)
    monkeypatch.setattr(settings, "openrouter_api_key", key)
    monkeypatch.setattr(settings, "agents_on", on)
    monkeypatch.setattr(settings, "claude_enabled", claude_on)
    monkeypatch.setattr(settings, "codex_enabled", codex_on)
    monkeypatch.setattr(settings, "claude_model", "sonnet")
    out = asyncio.run(openrouter.structured("t", Answer, "s", "u", model=model, daily_budget_usd=1, temperature=0.3,
                                            reasoning_effort="medium", experiment=experiment, plan_only=plan_only))
    return out, calls


def test_on_claude_every_agent_goes_to_the_claude_plan_first(monkeypatch):
    out, calls = route(monkeypatch, on="claude", model=openrouter.FLASH, claude_reply=(Answer(x=1), 5))
    assert out == (Answer(x=1), 5) and [c[0] for c in calls] == ["claude"]
    assert calls[0][1]["model"] == "claude-sonnet" and calls[0][1]["effort"] == "medium"


def test_on_claude_the_agents_own_model_takes_over_when_the_plan_cant(monkeypatch):
    out, calls = route(monkeypatch, on="claude", model=openrouter.FLASH, claude_reply=claude.Unavailable("limit"))
    assert out == (Answer(x=9), 7) and [c[0] for c in calls] == ["claude", "openrouter"]
    assert calls[1][1]["model"] == openrouter.FLASH and calls[1][1]["temperature"] == 0.3
    # an OpenAI agent falls back the way it always did: the ChatGPT plan, then OpenRouter
    out, calls = route(monkeypatch, on="claude", model=openrouter.SOL, claude_reply=claude.Unavailable("limit"),
                       codex_reply=codex.Unavailable("paced off"))
    assert [c[0] for c in calls] == ["claude", "codex", "openrouter"] and calls[1][1]["model"] == openrouter.SOL


def test_on_claude_without_an_openrouter_key_the_call_is_skipped_plainly(monkeypatch):
    with pytest.raises(openrouter.BudgetExceeded, match="the Claude plan can't take it .*no OpenRouter key is set"):
        route(monkeypatch, on="claude", model=openrouter.FLASH, claude_reply=claude.Unavailable("limit"), key=None)
    with pytest.raises(openrouter.BudgetExceeded, match="CLAUDE_ENABLED"):
        route(monkeypatch, on="claude", model=openrouter.FLASH, key=None, claude_on=False)


def test_on_chatgpt_every_agent_runs_on_luna_through_codex(monkeypatch):
    out, calls = route(monkeypatch, on="chatgpt", model=openrouter.FLASH, codex_reply=(Answer(x=2), 6))
    assert out == (Answer(x=2), 6) and [c[0] for c in calls] == ["codex"] and calls[0][1]["model"] == openrouter.LUNA
    out, calls = route(monkeypatch, on="chatgpt", model=openrouter.SOL, codex_reply=(Answer(x=2), 6))
    assert calls[0][1]["model"] == openrouter.LUNA


def test_on_chatgpt_the_agents_own_model_takes_over_once(monkeypatch):
    out, calls = route(monkeypatch, on="chatgpt", model=openrouter.SOL, codex_reply=codex.Unavailable("paced off"))
    assert [c[0] for c in calls] == ["codex", "openrouter"]                  # the plan isn't asked twice
    assert calls[1][1]["model"] == openrouter.SOL and calls[1][1]["temperature"] is None
    _, calls = route(monkeypatch, on="chatgpt", model=openrouter.FLASH, codex_reply=codex.Unavailable("paced off"))
    assert calls[1][1]["model"] == openrouter.FLASH and calls[1][1]["temperature"] == 0.3
    with pytest.raises(openrouter.BudgetExceeded, match="the ChatGPT plan can't take it"):
        route(monkeypatch, on="chatgpt", model=openrouter.FLASH, codex_reply=codex.Unavailable("paced off"), key=None)


def test_on_openrouter_nothing_changes(monkeypatch):
    _, calls = route(monkeypatch, on="openrouter", model=openrouter.FLASH, claude_reply=(Answer(x=1), 5))
    assert [c[0] for c in calls] == ["openrouter"]
    _, calls = route(monkeypatch, on="openrouter", model=openrouter.LUNA, codex_reply=(Answer(x=1), 5))
    assert [c[0] for c in calls] == ["codex"] and calls[0][1]["model"] == openrouter.LUNA
    with pytest.raises(openrouter.NotConfigured, match="OPENROUTER_API_KEY is not set"):     # skipped, never failed
        route(monkeypatch, on="openrouter", model=openrouter.FLASH, key=None)


def test_a_shadow_keeps_its_own_model_whatever_agents_on_says(monkeypatch):
    for on in ("claude", "chatgpt"):
        with pytest.raises(openrouter.BudgetExceeded, match="only on the ChatGPT plan"):
            route(monkeypatch, on=on, model=openrouter.SOL, codex_reply=codex.Unavailable("experiments' line"),
                  experiment=True, plan_only=True)
        _, calls = route(monkeypatch, on=on, model=openrouter.SOL, codex_reply=(Answer(x=1), 5), experiment=True,
                         plan_only=True)
        assert [c[0] for c in calls] == ["codex"] and calls[0][1]["model"] == openrouter.SOL
        assert calls[0][1]["experiment"] is True


def test_web_research_without_a_key_is_skipped_on_a_plan(monkeypatch):
    async def no_plan(*a, **kw):
        raise codex.Unavailable("paced off")

    async def none_yet(agent):
        return 0
    monkeypatch.setattr(codex, "search", no_plan)
    monkeypatch.setattr(openrouter, "calls_today", none_yet)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    monkeypatch.setattr(settings, "agents_on", "claude")
    with pytest.raises(openrouter.BudgetExceeded, match="the ChatGPT plan can't research now"):
        asyncio.run(openrouter.research("oracle", Answer, "p", daily_budget_usd=1, max_calls_per_day=5))
    monkeypatch.setattr(settings, "agents_on", "chatgpt")
    with pytest.raises(openrouter.BudgetExceeded):
        asyncio.run(fit.ask([], "m"))


def test_the_reddit_scout_runs_on_the_chatgpt_plan_without_a_key(monkeypatch):
    reached = []

    class P:
        async def fetch(self, *a):
            return []

    async def fake_pool():
        return P()

    async def zero():
        return 0

    def room(done, now, daily):
        reached.append(True)
        return 0
    monkeypatch.setattr(db, "pool", fake_pool)
    monkeypatch.setattr(reddit, "reads_today", zero)
    monkeypatch.setattr(reddit, "daily", zero)
    monkeypatch.setattr(reddit, "room", room)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    monkeypatch.setattr(settings, "codex_enabled", False)
    assert asyncio.run(reddit.scan()) == 0 and reached == []                  # nothing could read: it doesn't try
    monkeypatch.setattr(settings, "codex_enabled", True)
    assert asyncio.run(reddit.scan()) == 0 and reached == [True]
    with pytest.raises(openrouter.BudgetExceeded):                            # the plan said no: stop, mark no coin
        asyncio.run(reddit.ask([], "m"))


# ─── the Setup page's API ─────────────────────────────────────────────────────────
WRITE = {"X-Albatross-Action": "1"}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(settings, "site_password_hash", None)
    monkeypatch.setattr(settings, "allowed_hosts", "localhost")
    monkeypatch.setattr(settings, "codex_enabled", True)
    monkeypatch.setattr(settings, "claude_enabled", False)
    monkeypatch.setattr(settings, "claude_model", "opus")
    monkeypatch.setattr(settings, "agents_on", "claude")
    app = FastAPI()
    app.include_router(subs.router)
    return TestClient(app, base_url="http://localhost:8787")


def gateway_answers(monkeypatch, answers: dict):
    sent = []

    async def fake(method, path, body=None, timeout=30):
        sent.append((method, path, body))
        reply = answers[(method, path)]
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(codex, "gateway", fake)
    return sent


def test_without_the_gateway_the_panel_says_how_to_start_it(api, monkeypatch):
    down = httpx.ConnectError("[Errno 2] No such file or directory")
    gateway_answers(monkeypatch, {("GET", "/v1/status"): down, ("GET", "/v1/login/codex"): down})
    r = api.get("/api/setup/subs")
    assert r.status_code == 200 and r.json() == {
        "gateway": {"reachable": False, "error": "The subscriptions gateway isn't running.",
                    "how": "docker compose --profile subscriptions up -d"},
        "chatgpt": {"installed": False, "version": None, "signed_in": False, "plan": None, "enabled": True,
                    "login": {"state": "idle", "url": None, "code": None, "error": None}},
        "claude": {"installed": False, "version": None, "signed_in": False, "enabled": False, "model": "opus"},
        "agents_on": "claude"}


def test_the_panel_shows_both_plans(api, monkeypatch):
    gateway_answers(monkeypatch, {
        ("GET", "/v1/status"): (200, {"codex": {"installed": True, "version": "0.165.0", "signed_in": True,
                                                "plan": "plus"},
                                      "claude": {"installed": True, "version": "2.1.312", "signed_in": True}}),
        ("GET", "/v1/login/codex"): (200, {"state": "pending", "url": "https://auth.openai.com/codex/device",
                                           "code": "LC5W-88WTZ", "error": None})})
    body = api.get("/api/setup/subs").json()
    assert body["gateway"] == {"reachable": True, "error": None, "how": "docker compose --profile subscriptions up -d"}
    assert body["chatgpt"] == {"installed": True, "version": "0.165.0", "signed_in": True, "plan": "plus",
                               "enabled": True, "login": {"state": "pending", "url": "https://auth.openai.com/codex/device",
                                                          "code": "LC5W-88WTZ", "error": None}}
    assert body["claude"] == {"installed": True, "version": "2.1.312", "signed_in": True, "enabled": False,
                              "model": "opus"}


def test_an_old_gateway_is_named_as_such(api, monkeypatch):
    old = (404, {"ok": False, "kind": "bad_request", "error": "not found"})
    gateway_answers(monkeypatch, {("GET", "/v1/status"): old, ("GET", "/v1/login/codex"): old})
    body = api.get("/api/setup/subs").json()
    assert body["gateway"]["reachable"] is False and "older version" in body["gateway"]["error"]


def test_signing_in_goes_through_the_gateway(api, monkeypatch):
    pending = {"state": "pending", "url": "https://auth.openai.com/codex/device", "code": "LC5W-88WTZ", "error": None}
    sent = gateway_answers(monkeypatch, {
        ("POST", "/v1/login/codex/start"): (200, pending),
        ("POST", "/v1/login/codex/auth-json"): (400, {"ok": False, "error": "That isn't valid JSON. Paste the whole "
                                                                            "auth.json file."}),
        ("POST", "/v1/login/claude"): (200, {"ok": True, "error": None}),
        ("DELETE", "/v1/login/codex"): (200, {"ok": True}),
        ("DELETE", "/v1/login/claude"): (200, {"ok": True})})
    assert api.post("/api/setup/subs/chatgpt/login", json={}, headers=WRITE).json() == pending
    r = api.post("/api/setup/subs/chatgpt/auth-json", json={"auth_json": "{bad"}, headers=WRITE)
    assert r.json() == {"ok": False, "error": "That isn't valid JSON. Paste the whole auth.json file."}
    r = api.post("/api/setup/subs/claude/token", json={"token": TOKEN}, headers=WRITE)
    assert r.json() == {"ok": True, "error": None} and TOKEN not in r.text
    assert api.request("DELETE", "/api/setup/subs/chatgpt", json={}, headers=WRITE).json() == {"ok": True}
    assert api.request("DELETE", "/api/setup/subs/claude", json={}, headers=WRITE).json() == {"ok": True}
    assert [s[:2] for s in sent] == [("POST", "/v1/login/codex/start"), ("POST", "/v1/login/codex/auth-json"),
                                     ("POST", "/v1/login/claude"), ("DELETE", "/v1/login/codex"),
                                     ("DELETE", "/v1/login/claude")]
    assert sent[2][2] == {"token": TOKEN}


def test_a_write_needs_the_action_header_and_a_local_host(api, monkeypatch):
    sent = gateway_answers(monkeypatch, {})
    assert api.post("/api/setup/subs/claude/token", json={"token": TOKEN}).status_code == 403
    assert api.request("DELETE", "/api/setup/subs/claude", json={}).status_code == 403
    other = TestClient(api.app, base_url="http://evil.example")
    assert other.get("/api/setup/subs").status_code == 403
    assert other.post("/api/setup/subs/chatgpt/login", json={}, headers=WRITE).status_code == 403
    assert sent == []


def test_writes_with_the_gateway_down_say_how_to_start_it(api, monkeypatch):
    down = httpx.ConnectError("no socket")
    gateway_answers(monkeypatch, {("POST", "/v1/login/codex/start"): down, ("POST", "/v1/login/claude"): down,
                                  ("DELETE", "/v1/login/claude"): httpx.ReadTimeout("slow")})
    body = api.post("/api/setup/subs/chatgpt/login", json={}, headers=WRITE).json()
    assert body == {"state": "failed", "url": None, "code": None,
                    "error": "The subscriptions gateway isn't running. Start it with: docker compose --profile "
                             "subscriptions up -d"}
    assert api.post("/api/setup/subs/claude/token", json={"token": TOKEN}, headers=WRITE).json()["ok"] is False
    assert api.request("DELETE", "/api/setup/subs/claude", json={}, headers=WRITE).json() == {
        "ok": False, "error": "The subscriptions gateway didn't answer in time."}


def test_the_real_socket_is_used_when_nothing_is_mocked(api, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "codex_gateway_sock", str(tmp_path / "missing.sock"))
    body = api.get("/api/setup/subs").json()
    assert body["gateway"]["reachable"] is False and body["gateway"]["error"] == subs.NOT_RUNNING
    short = tempfile.mkdtemp(prefix="gw")                            # a socket file nothing listens on
    s = socket.socket(socket.AF_UNIX)
    s.bind(os.path.join(short, "s.sock"))
    s.close()
    monkeypatch.setattr(settings, "codex_gateway_sock", os.path.join(short, "s.sock"))
    try:
        assert api.get("/api/setup/subs").json()["gateway"]["error"] == subs.NOT_RUNNING
    finally:
        shutil.rmtree(short, ignore_errors=True)
