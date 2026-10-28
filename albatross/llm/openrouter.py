"""OpenRouter chat client. Every call is logged to agent_runs with tokens and USD cost,
and refused once an agent exceeds its daily budget."""

import asyncio
import hashlib
import logging
import time

import httpx
import orjson
from pydantic import BaseModel, ValidationError

from albatross import controls, db, github_guard, keys
from albatross.config import settings
from albatross.http import client, retrying
from albatross.llm import claude, codex

log = logging.getLogger(__name__)

URL = "https://openrouter.ai/api/v1/chat/completions"
FLASH = "deepseek/deepseek-v4.1-flash"
# The research agents (the Oracle, its events calendar): an OpenAI model, so running them on the ChatGPT plan through
# Codex is a model change, not a rewrite.
RESEARCH = "openai/gpt-6.1-sol"
# A ChatGPT subscription (2026-10-03): these two are served through Codex first (llm/codex.py) and only fall back
# to OpenRouter. Luna is the worker model (a twentieth of Sol's price per token, and no reasoning tokens at low effort,
# measured 2026-10-02); Sol is for the few lead jobs. On OpenRouter Luna also costs less than DeepSeek V4.1 Flash.
LUNA = "openai/gpt-6-luna"
SOL = RESEARCH
# Shadows (graded experiments) run only on the ChatGPT plan, except on a model that costs pennies on OpenRouter.
SHADOW_FALLBACK_OK = {LUNA}
SHADOW_FALLBACK_USD = 2.0             # the daily cap, at list price, for a shadow that may reach OpenRouter


def shadow_terms(model: str, budget_usd: float) -> tuple[bool, float]:
    """(plan_only, daily budget) for a shadow on `model`."""
    if model in SHADOW_FALLBACK_OK:
        return False, min(budget_usd, SHADOW_FALLBACK_USD)
    return True, budget_usd
# Routers choose the model and reasoning effort themselves and take no reasoning parameter.
ROUTERS = {"typesafe/jev-router", "openrouter/auto"}


def overrides() -> dict[str, str]:
    """settings.llm_model_overrides "agent=model,agent=model" -> {agent: model}."""
    out = {}
    for part in (settings.llm_model_overrides or "").split(","):
        agent, _, model = part.partition("=")
        if agent.strip() and model.strip():
            out[agent.strip()] = model.strip()
    return out


def model_for(agent: str, default: str) -> str:
    return overrides().get(agent, default)


def recorded(requested: str, answered: str | None) -> str:
    """What agent_runs.model stores: the model asked for, and for a router the one that actually answered."""
    return requested if not answered or answered == requested else f"{requested} -> {answered}"


class CallLimitExceeded(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class NotConfigured(BudgetExceeded, keys.NotConfigured):
    """No model can take the call: no OpenRouter key, and no plan that could. Callers skip it like a spent budget, and
    tracked() records the job as skipped, so a fresh install with no keys is quiet."""


def no_key(agent: str, why: str) -> NotConfigured:
    """The error for a call only OpenRouter could take, with no OpenRouter key set."""
    if settings.agents_on in ("chatgpt", "claude"):
        return NotConfigured(f"{agent}: {why}, and no OpenRouter key is set to fall back on")
    return NotConfigured(f"{agent}: no AI is set up. Add an OpenRouter key or connect a plan on the Setup page "
                         "(OPENROUTER_API_KEY is not set)")


async def spent_today(agent: str) -> float:
    """OpenRouter dollars today: what an agent's daily budget caps. Its use of the ChatGPT plan has its own cap
    (llm/codex.py PLAN_BUDGET_MULTIPLE times the budget, at list price)."""
    return await codex.api_usd_today(agent)


@retrying
async def _post(body: dict) -> dict:
    r = await client().post(
        URL, json=body, timeout=httpx.Timeout(120.0),
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}", "X-Title": "Albatross"},
    )
    r.raise_for_status()
    return r.json()


async def structured[T: BaseModel](
    agent: str,
    schema: type[T],
    system: str,
    user: str,
    *,
    model: str = FLASH,
    daily_budget_usd: float,
    temperature: float | None = 0.2,
    max_tokens: int = 8000,
    reasoning_effort: str | None = "low",
    deadline_s: float = 150,
    queue_s: float | None = None,
    experiment: bool = False,
    plan_only: bool = False,
    not_after: float | None = None,
) -> tuple[T, int]:
    """JSON-mode completion validated against `schema`. Returns (parsed, agent_run_id).

    One retry for the transient failures (a stuck generation, an empty answer, JSON cut off or garbled):
    about 5% of calls on 2026-09-29, spread through the day. The slowest good call in a week took 112 s,
    so 150 s is the deadline. Each attempt is its own agent_runs row. Refusals (402, 4xx) aren't retried.
    Luna and Sol go to the ChatGPT plan first (llm/codex.py); queue_s caps how long that call waits for a free slot
    there before it comes back here (an every-minute job can't wait 90 s). experiment: a shadow, which gives way to the
    regular agents on the plan. plan_only: never here; BudgetExceeded instead (a Sol shadow would cost real money).
    not_after: a shadow's end (time.monotonic()); a call that only gets its slot later goes nowhere.
    AGENTS_ON=claude (with CLAUDE_ENABLED) sends every agent to the Claude plan first (llm/claude.py, CLAUDE_MODEL);
    AGENTS_ON=chatgpt (with CODEX_ENABLED) every agent to Luna on the ChatGPT plan. When that plan can't take a call it
    falls back to the agent's own model as above; with no OpenRouter key it is BudgetExceeded instead. Shadows keep
    their own model whatever AGENTS_ON says: they are graded as that model."""
    model = model_for(agent, model)
    if await controls.paused(agent):                  # the Output page's pause: callers treat it like a spent budget
        raise BudgetExceeded(f"{agent} is paused on the Output page")
    daily_budget_usd *= await controls.scale(agent)
    effort = reasoning_effort or "low"
    lane = "openrouter" if experiment else settings.agents_on
    plan_why, tried_codex = None, False               # why the plan AGENTS_ON names couldn't take this call
    if lane == "claude":
        try:
            if not settings.claude_enabled:
                raise claude.Unavailable("the Claude lane is switched off (CLAUDE_ENABLED)")
            if (on := claude.gateway_model(settings.claude_model)) is None:
                raise claude.Unavailable(f"CLAUDE_MODEL {settings.claude_model!r} isn't haiku, sonnet or opus")
            return await claude.structured(agent, schema, system, user, model=on, effort=effort, deadline_s=deadline_s,
                                           daily_budget_usd=daily_budget_usd, queue_s=queue_s)
        except claude.Unavailable as e:
            plan_why = f"the Claude plan can't take it ({str(e)[:200]})"
    elif lane == "chatgpt":
        try:
            if not settings.codex_enabled:
                raise codex.Unavailable("the subscription lane is switched off (CODEX_ENABLED)")
            tried_codex = True
            return await codex.structured(agent, schema, system, user, model=LUNA, effort=effort,
                                          deadline_s=deadline_s, daily_budget_usd=daily_budget_usd, queue_s=queue_s)
        except codex.Unavailable as e:
            plan_why = f"the ChatGPT plan can't take it ({str(e)[:200]})"
    if plan_why:
        log.info("%s: %s; trying %s", agent, plan_why, model)
    if codex.twin(model) and tried_codex:             # the ChatGPT plan just said no: straight to OpenRouter
        temperature = None
    elif codex.twin(model):                           # the subscription first; the same model here if it can't
        if settings.codex_enabled:
            try:
                return await codex.structured(agent, schema, system, user, model=model, effort=effort,
                                              deadline_s=deadline_s, daily_budget_usd=daily_budget_usd,
                                              queue_s=queue_s, experiment=experiment, not_after=not_after)
            except codex.Unavailable as e:
                if plan_only or isinstance(e, codex.OutOfTime):
                    raise BudgetExceeded(f"{agent} runs only on the ChatGPT plan: {str(e)[:200]}") from e
                log.info("%s: subscription lane unavailable (%s), using OpenRouter", agent, str(e)[:200])
        elif plan_only:                               # the plan is switched off: nothing to log on every call
            raise BudgetExceeded(f"{agent} runs only on the ChatGPT plan, which is switched off (CODEX_ENABLED)")
        temperature = None                            # OpenAI's reasoning models take no temperature
    elif plan_only:
        raise BudgetExceeded(f"{agent}: {model} isn't served by the ChatGPT plan")
    if not settings.openrouter_api_key:
        raise no_key(agent, plan_why or f"{model} runs only on OpenRouter here")
    if (spent := await spent_today(agent)) >= daily_budget_usd:
        raise BudgetExceeded(f"{agent} spent ${spent:.2f} of ${daily_budget_usd:.2f} today")

    messages = [
        {"role": "system", "content": system + "\n\nRespond with a single JSON object matching this JSON Schema:\n"
         + orjson.dumps(schema.model_json_schema()).decode()},
        {"role": "user", "content": user},
    ]
    input_hash = hashlib.sha256(orjson.dumps(messages)).hexdigest()[:16]
    for attempt in (1, 2):
        parsed, run_id, error, transient, _ = await _attempt(agent, schema, messages, input_hash, model=model,
                                                             temperature=temperature, max_tokens=max_tokens,
                                                             reasoning_effort=reasoning_effort, deadline_s=deadline_s)
        if parsed is not None:
            return parsed, run_id
        if not transient or attempt == 2:
            break
        log.warning("%s: attempt 1 failed (%s); retrying once", agent, error)
    raise RuntimeError(f"{agent} call failed: {error}")


async def _attempt(agent, schema, messages, input_hash, *, model, temperature, max_tokens, reasoning_effort, deadline_s,
                   tools: list | None = None):
    """One call -> (parsed or None, agent_run_id, error, transient, evidence). With tools (web search), the answer is
    the last JSON object in the text (JSON mode and server tools don't mix on every model) and evidence is every URL
    the call's own search returned; otherwise evidence is empty."""
    started = time.monotonic()
    body = {
        "model": model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True},
        **({"tools": tools} if tools else {"response_format": {"type": "json_object"}}),
    }
    if temperature is not None:
        body["temperature"] = temperature
    # Reasoning models spend max_tokens on thinking first; unbounded thinking on a big batch
    # can leave no room for the answer (content comes back null).
    if reasoning_effort and model not in ROUTERS:
        body["reasoning"] = {"effort": reasoning_effort}
    usage: dict = {}
    answered = None
    raw = None
    error = None
    parsed = None
    transient = False
    resp: dict = {}
    try:
        # OpenRouter keeps slow non-streamed requests alive with whitespace, which resets httpx's read
        # timeout forever; a stuck generation hung a Strategist run. Bound the whole call instead.
        resp = await asyncio.wait_for(_post(body), timeout=deadline_s)
        usage = resp.get("usage") or {}
        answered = resp.get("model")
        choice = resp["choices"][0]
        raw = choice["message"]["content"]
        if choice.get("finish_reason") == "length":
            transient = True
            raise ValueError(f"output truncated at max_tokens={max_tokens}")
        if not raw or not raw.strip():
            transient = True
            raise ValueError(f"empty answer (finish_reason={choice.get('finish_reason')})")
        if tools:
            from albatross.llm.cli import last_json_object
            if (obj := last_json_object(raw)) is None:
                transient = True
                raise ValueError("no JSON object in the answer")
            parsed = schema.model_validate(obj)
        else:
            parsed = schema.model_validate_json(raw)
    except ValidationError as e:
        error = f"ValidationError: {str(e)[:400]}"
        transient = any(err.get("type", "").startswith("json_") for err in e.errors())   # garbled or cut-off JSON
    except (KeyError, ValueError) as e:
        error = f"{type(e).__name__}: {str(e)[:400]}"
    except httpx.HTTPStatusError as e:
        error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        transient = e.response.status_code in (429, 500, 502, 503, 504)
    except TimeoutError:
        error = f"no response within {deadline_s:.0f}s"
        transient = True
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        transient = True

    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error,
                                   input_hash, output)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        agent, recorded(model, answered), int((time.monotonic() - started) * 1000),
        usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost"),
        parsed is not None, error, input_hash,
        parsed.model_dump_json() if parsed else (orjson.dumps({"raw": raw}).decode() if raw else None),
    )
    return parsed, run_id, error, transient, (searched(resp) if tools else set())


# ─── web research (the Oracle and its calendar) ──────────────────────────────
# The instructions a search on the ChatGPT plan works under (its answer's schema goes separately, in strict form).
SEARCH_RULES = """Search the web before you answer. Open every page you cite and cite it by the exact URL you
opened: a citation of a page you didn't open is discarded. Never open or cite anything hosted on GitHub."""
# Appended to a research prompt on the plan, where "a page your search returned" means a page it opened (the first
# calendar run on the plan cited 23 events from search results it never opened, and 21 were discarded).
PLAN_POSTSCRIPT = """

On this search a page counts as returned only once you have opened it. Before you answer, open the page behind every
URL, citation and quote you give; anything you cite without opening it is discarded."""

def web_search(max_uses: int = 3, max_results: int = 5) -> dict:
    """OpenRouter's search tool on the Perplexity engine, which honours excluded_domains (OpenAI's native search only
    takes an allowlist): nothing hosted on GitHub is ever searched (github_guard.py, since 2026-10-01)."""
    return {"type": "openrouter:web_search",
            "parameters": {"engine": "perplexity", "max_results": max_results, "max_uses": max_uses,
                           "excluded_domains": github_guard.SEARCH_EXCLUDE}}


async def opened_now(agent: str, answer: BaseModel, opened: list[str], daily_budget_usd: float) -> dict[str, str]:
    """{page: its own title} for the pages a plan answer cited without opening that a second pass opened and found
    real (codex.confirm); empty when there are none, the agent is paused, or the plan can't take the pass (they are
    then dropped by the caller's citation check, as before)."""
    missing = codex.unopened(answer, opened)
    if not missing or await controls.paused(agent):
        return {}
    try:
        return await codex.confirm(agent, missing, daily_budget_usd=daily_budget_usd)
    except (codex.Unavailable, BudgetExceeded) as e:
        log.info("%s: no confirmation pass (%s)", agent, str(e)[:160])
        return {}


def searched(resp: dict) -> set[str]:
    """Every URL the call's own search returned (OpenRouter's url_citation annotations), never one on GitHub."""
    msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
    out = set()
    for a in msg.get("annotations") or []:
        u = str((a.get("url_citation") or {}).get("url") or "").strip().rstrip("/.,;")
        if a.get("type") == "url_citation" and u and not github_guard.hosted(u):
            out.add(u)
    return out


async def calls_today(agent: str) -> int:
    pool = await db.pool()
    return await pool.fetchval(
        "SELECT count(*) FROM agent_runs WHERE agent = $1 AND started_at >= date_trunc('day', now())", agent)


async def research[T: BaseModel](
    agent: str, schema: type[T], prompt: str, *, daily_budget_usd: float, max_calls_per_day: int,
    model: str = RESEARCH, searches: int = 3, reasoning_effort: str | None = "medium", max_tokens: int = 16000,
    deadline_s: float = 300,
) -> tuple[T, int, set[str]]:
    """A web-researched answer validated against `schema` -> (parsed, agent_run_id, evidence): evidence is every URL
    the call's own search returned (on the ChatGPT plan: every page it opened), so callers keep only citations the
    model was actually shown. Refused once the agent is paused, past max_calls_per_day or past its daily budget (each
    attempt is an agent_runs row and counts); one retry for transient failures. The ChatGPT plan's search first (no API
    spend while the plan has room), OpenRouter's GitHub-excluding search when it can't."""
    model = model_for(agent, model)
    if await controls.paused(agent):
        raise BudgetExceeded(f"{agent} is paused on the Output page")
    if (n := await calls_today(agent)) >= max_calls_per_day:
        raise CallLimitExceeded(f"{agent}: {n}/{max_calls_per_day} calls today")
    daily_budget_usd *= await controls.scale(agent)
    try:
        parsed, run_id, opened, _github = await codex.search(
            agent, schema, SEARCH_RULES, prompt + PLAN_POSTSCRIPT, effort=reasoning_effort or "medium",
            deadline_s=max(deadline_s, 420), daily_budget_usd=daily_budget_usd)
        return parsed, run_id, set(opened) | set(await opened_now(agent, parsed, opened, daily_budget_usd))
    except codex.Unavailable as e:
        log.info("%s: the plan can't research now (%s), using OpenRouter", agent, str(e)[:200])
        why = f"the ChatGPT plan can't research now ({str(e)[:200]})"
    if not settings.openrouter_api_key:
        raise no_key(agent, why)
    if (spent := await spent_today(agent)) >= daily_budget_usd:
        raise BudgetExceeded(f"{agent} spent ${spent:.2f} of ${daily_budget_usd:.2f} today")
    messages = [
        {"role": "system", "content": "Search the web before you answer, and cite pages exactly as your search returned "
         "them. End with a single JSON object matching this JSON Schema:\n"
         + orjson.dumps(schema.model_json_schema()).decode()},
        {"role": "user", "content": prompt},
    ]
    input_hash = hashlib.sha256(orjson.dumps(messages)).hexdigest()[:16]
    for attempt in (1, 2):
        parsed, run_id, error, transient, seen = await _attempt(
            agent, schema, messages, input_hash, model=model, temperature=None, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, deadline_s=deadline_s, tools=[web_search(searches)])
        if parsed is not None:
            return parsed, run_id, seen
        if not transient or attempt == 2:
            break
        log.warning("%s: attempt 1 failed (%s); retrying once", agent, error)
    raise RuntimeError(f"{agent} call failed: {error}")
