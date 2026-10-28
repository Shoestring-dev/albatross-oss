"""Subscription-backed CLIs (Grok Build) in headless mode.

These run on the host where the user is logged in, never inside Docker. Usage counts
against the subscription's pool rather than an API bill, so limits are per-call, not $.
The Oracle researches through OpenRouter (llm/openrouter.py research()) or the ChatGPT plan.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError

from albatross import controls, db

log = logging.getLogger(__name__)

GROK = shutil.which("grok") or str(Path.home() / ".grok" / "bin" / ("grok.exe" if os.name == "nt" else "grok"))
# An empty scratch dir OUTSIDE the repo, so the CLIs can never walk up into it (or its secrets).
SANDBOX = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache") / "albatross" / "cli-sandbox"


class CallLimitExceeded(RuntimeError):
    pass


async def calls_today(agent: str) -> int:
    pool = await db.pool()
    return await pool.fetchval(
        "SELECT count(*) FROM agent_runs WHERE agent = $1 AND started_at >= date_trunc('day', now())", agent
    )


async def calls_last_7d(agent: str) -> int:
    pool = await db.pool()
    return await pool.fetchval(
        "SELECT count(*) FROM agent_runs WHERE agent = $1 AND started_at > now() - interval '7 days'", agent
    )


def last_json_object(text: str) -> dict | None:
    """Grok sometimes emits a partial object followed by the full one; take the last complete object."""
    decoder = json.JSONDecoder()
    found, i = None, 0
    while (start := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
            found, i = obj, end
        except ValueError:
            i = start + 1
    return found


async def _run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        # stdin closed: a CLI otherwise waits for piped input on every call
        *argv, cwd=SANDBOX, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def grok_structured[T: BaseModel](
    agent: str, schema: type[T], prompt: str, *, max_calls_per_day: int, max_calls_per_7d: int | None = None,
    model: str | None = None, timeout: float = 420, extra_args: list[str] | None = None,
) -> tuple[T, int]:
    if await controls.paused(agent):
        raise CallLimitExceeded(f"{agent} is paused on the Output page")
    if (n := await calls_today(agent)) >= max_calls_per_day:
        raise CallLimitExceeded(f"{agent}: {n}/{max_calls_per_day} calls today")
    # SuperGrok's pool resets weekly; a rolling 7-day cap holds whatever day the reset falls on.
    if max_calls_per_7d is not None and (n := await calls_last_7d(agent)) >= max_calls_per_7d:
        raise CallLimitExceeded(f"{agent}: {n}/{max_calls_per_7d} calls in the last 7 days")
    # --sandbox strict: kernel-enforced (Landlock) reads limited to the empty sandbox dir, system paths
    # and ~/.grok, so nothing Grok is told to do (e.g. by a prompt-injected X post) can read the repo,
    # its secrets or ~/.ssh. Grok refuses to start rather than run unsandboxed. Trace upload,
    # telemetry and workspace snapshots are also pinned off in ~/.grok/config.toml on the host.
    argv = [GROK, "-p", prompt, "--json-schema", json.dumps(schema.model_json_schema()),
            "--cwd", str(SANDBOX), "--sandbox", "strict", "--max-turns", "10", *(extra_args or [])]
    if model:
        argv += ["-m", model]
    return await _record(agent, f"grok:{model or 'default'}", schema, prompt, argv, timeout, _parse_grok)


def _parse_grok(stdout: str) -> tuple[str | None, dict]:
    envelope = json.loads(stdout)
    # Grok's headless envelope carries the response in "text"; structuredOutput is null whenever the
    # model emits more than one JSON object, which it often does (a draft, then the final answer).
    payload = envelope.get("structuredOutput") or last_json_object(envelope.get("text") or "")
    usage = envelope.get("usage") or {}
    meta = {
        "input_tokens": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0),
        "output_tokens": usage.get("output_tokens"),
        "equiv_cost_usd": envelope.get("total_cost_usd"),
    }
    return (json.dumps(payload) if payload is not None else None), meta


async def _record(agent, model, schema, prompt, argv, timeout, parse):
    started = time.monotonic()
    parsed, meta, error, out = None, {}, None, ""
    try:
        code, out, err = await _run(argv, timeout)
        if code != 0:
            error = f"exit {code}: {err.strip()[-300:] or out.strip()[-300:]}"
        else:
            raw, meta = parse(out)
            if raw is None:
                error = "no JSON object in output"
            else:
                parsed = schema.model_validate_json(raw)
    except (ValidationError, ValueError) as e:
        error = f"{type(e).__name__}: {str(e)[:300]}"
    except TimeoutError:
        error = f"timeout after {timeout}s"

    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error,
                                   input_hash, output)
           VALUES ($1, $2, $3, $4, $5, NULL, $6, $7, $8, $9) RETURNING id""",
        agent, model, int((time.monotonic() - started) * 1000), meta.get("input_tokens"),
        meta.get("output_tokens"), parsed is not None, error,
        hashlib.sha256(prompt.encode()).hexdigest()[:16],
        json.dumps({"result": parsed.model_dump() if parsed else None,
                    "subscription_equiv_cost_usd": meta.get("equiv_cost_usd"),
                    # Keep the raw tail on failure so bad outputs can be diagnosed.
                    "raw_tail": None if parsed else out[-4000:]}),
    )
    if parsed is None:
        raise RuntimeError(f"{agent} failed: {error}")
    return parsed, run_id
