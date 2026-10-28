"""The Claude-subscription lane: agent calls through Claude Code's headless mode on the account holder's Claude plan,
when AGENTS_ON=claude and CLAUDE_ENABLED is on (added 2026-10-27).

The gateway (deploy/codex_gateway.py, in the `subs` container) runs the CLI with every tool off, the agent's own
instructions as the whole system prompt and its answer shaped by --json-schema. openrouter.structured() sends every
agent here first, on the model CLAUDE_MODEL names, and falls back to the agent's own model when this lane is off,
resting after the plan's limit, over the agent's budget or fails, so an agent never stops because the plan did.

The plan's allowance can't be read ahead the way Codex's can, so there is no pacer: the lane stops when the plan says
its limit is reached and waits until the reset time it names (LIMIT_COOLDOWN_S when it names none). Claude Code reports
each call's list price (total_cost_usd); it is stored as the run's subscription_equiv_cost_usd, the way the Grok lane
stores its own, and an agent's use of the plan is capped at PLAN_BUDGET_MULTIPLE times its daily budget at that price.
"""

import hashlib
import logging
import time

import httpx
import orjson
from pydantic import BaseModel, ValidationError

from albatross import db
from albatross.config import settings
from albatross.llm import codex
from albatross.llm.cli import last_json_object

log = logging.getLogger(__name__)

MODELS = {"haiku", "sonnet", "opus"}           # Claude Code's aliases for the newest of each
EFFORTS = {"low", "medium", "high"}
# An agent may use this many times its daily dollar budget of the plan (at list price), as on the ChatGPT plan; its
# OpenRouter dollars stay capped at the budget itself.
PLAN_BUDGET_MULTIPLE = codex.PLAN_BUDGET_MULTIPLE
LIMIT_COOLDOWN_S = 15 * 60                     # after the plan's limit, when it names no reset time
MAX_COOLDOWN_S = 7 * 24 * 3600                 # a reset time further out than a week is a misread
QUEUE_S = 90                                   # the gateway's own wait for a free slot (CODEX_GATEWAY_QUEUE_S)
_blocked_until = 0.0                           # time.time()


class Unavailable(RuntimeError):
    """This call can't go through the Claude plan; the caller falls back."""


def gateway_model(alias: str | None) -> str | None:
    """settings.claude_model -> the gateway's model name, or None for an alias it doesn't serve."""
    alias = (alias or "").strip().lower()
    return f"claude-{alias}" if alias in MODELS else None


def cooldown(resets_at, now: float | None = None) -> float:
    """time.time() until which the lane rests after a limit: the plan's reset time when it named a sane one."""
    now = time.time() if now is None else now
    try:
        at = float(resets_at)
    except (TypeError, ValueError):
        return now + LIMIT_COOLDOWN_S
    return at if now < at <= now + MAX_COOLDOWN_S else now + LIMIT_COOLDOWN_S


async def plan_usd_today(agent: str) -> float:
    """An agent's use of the Claude plan today, in dollars at list price."""
    pool = await db.pool()
    return await pool.fetchval(
        """SELECT coalesce(sum((output->>'subscription_equiv_cost_usd')::float), 0) FROM agent_runs
           WHERE agent = $1 AND model LIKE 'claude:%' AND started_at >= date_trunc('day', now())""", agent)


async def _admit(agent: str, daily_budget_usd: float) -> None:
    if not settings.claude_enabled:
        raise Unavailable("the Claude lane is switched off (CLAUDE_ENABLED)")
    if time.time() < _blocked_until:
        raise Unavailable("the Claude plan reported its usage limit; waiting until it resets")
    try:
        spent = await plan_usd_today(agent)
    except Exception as e:  # noqa: BLE001 - a lane we can't check is a closed lane, never a failed agent call
        raise Unavailable(f"can't check the plan's use: {type(e).__name__}: {str(e)[:160]}") from e
    cap = daily_budget_usd * PLAN_BUDGET_MULTIPLE
    if spent >= cap:
        raise Unavailable(f"{agent} used ${spent:.2f} of the Claude plan today, its ${cap:.2f} cap at list price")


def masked(node):
    """Every string in a stored answer with its GitHub links replaced (codex.mask), the JSON around them intact."""
    if isinstance(node, str):
        return codex.mask(node)
    if isinstance(node, dict):
        return {k: masked(v) for k, v in node.items()}
    if isinstance(node, list):
        return [masked(v) for v in node]
    return node


def parse[T: BaseModel](schema: type[T], text: str) -> T:
    """The answer as `schema`: the last complete JSON object in the text (a stray draft before it is dropped)."""
    obj = last_json_object(text)
    if obj is None:
        raise ValueError("no JSON object in the answer")
    return schema.model_validate(obj)


async def structured[T: BaseModel](agent: str, schema: type[T], system: str, user: str, *, model: str, effort: str,
                                   deadline_s: float, daily_budget_usd: float,
                                   queue_s: float | None = None) -> tuple[T, int]:
    """One call on the Claude plan -> (parsed, agent_run_id). model: the gateway's name (claude-sonnet). Raises
    Unavailable (the caller falls back) when the lane is off or resting, the agent is past its cap, or the call fails;
    every call that ran is an agent_runs row with its tokens and list price."""
    global _blocked_until
    await _admit(agent, daily_budget_usd)
    queue = QUEUE_S if queue_s is None else min(queue_s, QUEUE_S)
    body = {"model": model, "effort": effort if effort in EFFORTS else "low", "instructions": system, "input": user,
            "schema": schema.model_json_schema(), "timeout_s": deadline_s, "queue_s": queue}
    input_hash = hashlib.sha256(orjson.dumps([system, user])).hexdigest()[:16]
    started = time.monotonic()
    try:
        status, resp = await codex.gateway("POST", "/v1/exec", body, timeout=deadline_s + queue + 15)
    except httpx.HTTPError as e:
        raise Unavailable(f"gateway unreachable: {type(e).__name__}: {str(e)[:160]}") from e
    usage = resp.get("usage") or {}
    if status in (400, 404, 413, 503) and not usage:               # refused, busy or not signed in: nothing ran
        raise Unavailable(f"{resp.get('kind') or 'failed'}: {str(resp.get('error') or status)[:300]}")
    raw = resp.get("text")
    parsed, error = None, None
    if status == 200 and raw:
        try:
            parsed = parse(schema, raw)
        except (ValidationError, ValueError) as e:
            error = f"{type(e).__name__}: {str(e)[:400]}"
    else:
        error = f"{resp.get('kind') or 'failed'}: {str(resp.get('error') or status)[:400]}"
        if resp.get("kind") == "limit":
            _blocked_until = cooldown(resp.get("resets_at"))
    output = {"result": parsed.model_dump(mode="json") if parsed else None,
              "subscription_equiv_cost_usd": resp.get("cost_usd"),
              "raw_tail": None if parsed or not raw else raw[-4000:]}       # a bad answer, kept to diagnose it
    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error,
                                   input_hash, output)
           VALUES ($1, $2, $3, $4, $5, NULL, $6, $7, $8, $9) RETURNING id""",
        agent, f"claude:{model.removeprefix('claude-')}", int((time.monotonic() - started) * 1000),
        usage.get("input_tokens"), usage.get("output_tokens"), parsed is not None, error, input_hash,
        orjson.dumps(masked(output)).decode())
    if parsed is None:
        raise Unavailable(error or "no answer")
    return parsed, run_id
