"""X search through the xAI API, on the owner's own key (settings.xai_api_key), from the ingest container.

The other lane is the Grok CLI on the host worker (llm/cli.py); llm/xsearch.py picks between them. What this follows
(docs.x.ai, read 2026-10-27: developers/tools/x-search, tools/citations, model-capabilities/text/structured-outputs,
cost-tracking, rest-api-reference/inference/responses and .../other):
- POST /v1/responses with the server-side tool {"type": "x_search"} (optionally from_date, YYYY-MM-DD, whole UTC days),
  the answer's JSON schema in text.format, reasoning.effort and max_turns. The model runs its own x_keyword_search,
  x_semantic_search and x_thread_fetch calls, and X's advanced operators (within_time:, min_faves:) work in a keyword
  query, so the agents' spelled-out searches carry over unchanged.
- response.citations lists every URL the searches returned, posts as https://x.com/<handle or i>/status/<id>. An X post
  URL in the answer whose status id isn't among them is blanked before the caller sees it, so the caller's evidence
  gate (the snowflake time check) drops it: Grok can skip a search and write plausible posts, and only a post the
  search returned counts. Inline citations are turned off: the answer is JSON.
- usage.cost_in_usd_ticks is what the call cost (10^10 ticks to the dollar), tokens and X posts fetched ($5 per 1,000)
  included. It goes to agent_runs.cost_usd with the model as "xai:<model>". Calls are capped per agent (per day and per
  7 days, as on the CLI) and in dollars across every xai: run (settings.xai_daily_usd a day).

The key is read from settings when a call runs (the Setup page can change it) and never logged: an error keeps only
the status code and xAI's own message, with anything shaped like a key cut out.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date

import httpx
from pydantic import BaseModel, ValidationError

from albatross import controls, db
from albatross.config import settings
from albatross.http import client
from albatross.llm.cli import CallLimitExceeded, calls_last_7d, calls_today, last_json_object

log = logging.getLogger(__name__)

API = "https://api.x.ai/v1"
# The fast model on the public API: "fast, reliable, strong tool calling", structured outputs, $1.25 / $2.50 per 1M
# tokens. The retired grok-4-fast and grok-4-1-fast slugs redirect to it since 2026-05-15 (Grok 4.7 Fast is only in
# Cursor and Grok Build).
DEFAULT_MODEL = "grok-4.3"
EFFORT = "low"                      # grok-4.3's default: the searches do the work
MAX_TURNS = 10                      # rounds of searching, as the CLI's --max-turns
TIMEOUT = 420
TICKS_PER_USD = 10_000_000_000
# Only for a response that reports no cost (every one should): posts and profiles at list price and tokens at the
# dearest standard text rate (grok-4.7's), so the daily cap errs high rather than low.
USD_PER_POST, USD_PER_PROFILE = 0.005, 0.010
USD_PER_M_IN, USD_PER_M_OUT = 2.00, 6.00
STATUS = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com/(?:i/web|[A-Za-z0-9_]{1,15})/status/(\d{1,20})"
                    r"(?:[/?#].*)?$")
KEY_LIKE = re.compile(r"xai-[A-Za-z0-9_\-]{4,}")


def model() -> str:
    return (settings.xai_model or "").strip() or DEFAULT_MODEL


def status_id(url) -> int | None:
    m = STATUS.match(url.strip()) if isinstance(url, str) else None
    return int(m.group(1)) if m else None


async def spent_today() -> float:
    """Dollars every xAI API call cost today (UTC): what settings.xai_daily_usd caps."""
    pool = await db.pool()
    return float(await pool.fetchval(
        """SELECT coalesce(sum(cost_usd), 0) FROM agent_runs
           WHERE model LIKE 'xai:%' AND started_at >= date_trunc('day', now())"""))


# ─── the request and the response ────────────────────────────────────────────
def request(schema: type[BaseModel], prompt: str, *, system: str, model: str, effort: str = EFFORT,
            max_turns: int = MAX_TURNS, from_date: date | None = None) -> dict:
    tool: dict = {"type": "x_search"}
    if from_date is not None:
        tool["from_date"] = from_date.isoformat()
    return {
        "model": model,
        "input": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "tools": [tool],
        "text": {"format": {"type": "json_schema", "name": schema.__name__, "schema": schema.model_json_schema(),
                            "strict": True}},
        "reasoning": {"effort": effort},
        "max_turns": max_turns,
        "include": ["no_inline_citations"],         # no [[1]](url) links inside the JSON
        "store": False,                             # nothing kept at xAI for a follow-up
    }


def answer_text(body: dict) -> str:
    """The text of the response's last message (the searches come before it as custom_tool_call items)."""
    msgs = [item for item in body.get("output") or [] if item.get("type") == "message"]
    if not msgs:
        return ""
    return "".join(c.get("text") or "" for c in msgs[-1].get("content") or [] if c.get("type") == "output_text")


def cited(body: dict) -> set[int]:
    """Status ids of the X posts the searches returned: response.citations, and any url_citation annotations."""
    urls = list(body.get("citations") or [])
    for item in body.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                urls += [a.get("url") for a in c.get("annotations") or [] if a.get("type") == "url_citation"]
    ids = (status_id(u if isinstance(u, str) else (u or {}).get("url")) for u in urls)
    return {i for i in ids if i is not None}


def usage(body: dict) -> dict:
    """Tokens, X items fetched and the dollars a response reports (estimated only when it reports none)."""
    u = body.get("usage") or {}
    tools = u.get("server_side_tool_usage_details") or {}
    reasoning = (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
    inp, out = u.get("input_tokens") or 0, u.get("output_tokens") or 0
    posts, users = tools.get("x_posts_fetched") or 0, tools.get("x_users_fetched") or 0
    if u.get("cost_in_usd_ticks") is not None:
        cost = u["cost_in_usd_ticks"] / TICKS_PER_USD
    elif u.get("cost_in_nano_usd") is not None:
        cost = u["cost_in_nano_usd"] / 1e9
    else:
        cost = (posts * USD_PER_POST + users * USD_PER_PROFILE
                + inp / 1e6 * USD_PER_M_IN + (out + reasoning) / 1e6 * USD_PER_M_OUT)
    return {"input_tokens": inp, "output_tokens": out + reasoning, "reasoning_tokens": reasoning,
            "cached_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens"), "cost_usd": cost,
            "cost_reported": u.get("cost_in_usd_ticks") is not None or u.get("cost_in_nano_usd") is not None,
            "x_search_calls": tools.get("x_search_calls"), "x_posts_fetched": posts, "x_users_fetched": users}


def uncited(answer: BaseModel, ids: set[int]) -> list[str]:
    """Blank every X post URL in the answer whose status id the searches never returned -> the URLs blanked."""
    out: list[str] = []

    def bad(v) -> bool:
        return isinstance(v, str) and (i := status_id(v)) is not None and i not in ids

    def walk(obj) -> None:
        if isinstance(obj, BaseModel):
            for name, v in obj:
                if bad(v):
                    out.append(v)
                    setattr(obj, name, "")
                else:
                    walk(v)
        elif isinstance(obj, list):
            for k, v in enumerate(obj):
                if bad(v):
                    out.append(v)
                    obj[k] = ""
                else:
                    walk(v)
    walk(answer)
    return out


def redact(text: str) -> str:
    """xAI's message with the key, or anything shaped like one, cut out."""
    key = (settings.xai_api_key or "").strip()
    if key:
        text = text.replace(key, "[key]")
    return KEY_LIKE.sub("[key]", text)


def _message(r: httpx.Response) -> str:
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        msg = None
    return redact(" ".join(str(msg or r.text or r.reason_phrase).split())[:300])


# ─── the call ────────────────────────────────────────────────────────────────
async def x_structured[T: BaseModel](
    agent: str, schema: type[T], prompt: str, *, system: str, max_calls_per_day: int,
    max_calls_per_7d: int | None = None, effort: str = EFFORT, from_date: date | None = None,
    max_turns: int = MAX_TURNS, timeout: float = TIMEOUT,
) -> tuple[T, int]:
    """grok_structured()'s role on the xAI API: one X-searched answer validated against `schema` -> (parsed,
    agent_run_id), every cited post the searches didn't return blanked. CallLimitExceeded while the agent is paused,
    past its calls today or in 7 days, with no key, or once today's xAI calls cost settings.xai_daily_usd; any other
    failure is an agent_runs row and a RuntimeError, as on the CLI."""
    if await controls.paused(agent):
        raise CallLimitExceeded(f"{agent} is paused on the Output page")
    if (n := await calls_today(agent)) >= max_calls_per_day:
        raise CallLimitExceeded(f"{agent}: {n}/{max_calls_per_day} calls today")
    if max_calls_per_7d is not None and (n := await calls_last_7d(agent)) >= max_calls_per_7d:
        raise CallLimitExceeded(f"{agent}: {n}/{max_calls_per_7d} calls in the last 7 days")
    key = (settings.xai_api_key or "").strip()
    if not key:
        raise CallLimitExceeded(f"{agent}: no xAI API key is set")
    if (spent := await spent_today()) >= (cap := settings.xai_daily_usd):
        raise CallLimitExceeded(f"{agent}: xAI calls cost ${spent:.2f} today, the daily cap is ${cap:.2f}")

    name = model()
    body = request(schema, prompt, system=system, model=name, effort=effort, max_turns=max_turns, from_date=from_date)
    started = time.monotonic()
    parsed, meta, error, raw, blanked, resp = None, {}, None, "", [], {}
    try:
        # httpx's timeout is per read; bound the whole call too.
        r = await asyncio.wait_for(client().post(
            f"{API}/responses", json=body, headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(timeout, connect=20.0)), timeout + 30)
        if r.status_code >= 400:
            error = f"HTTP {r.status_code}: {_message(r)}"
        else:
            resp = r.json()
            meta, raw = usage(resp), answer_text(resp)
            if resp.get("error"):
                error = f"xAI error: {redact(json.dumps(resp['error'])[:300])}"
            elif resp.get("status") not in (None, "completed"):
                error = f"response {resp.get('status')}: {json.dumps(resp.get('incomplete_details'))[:200]}"
            elif not raw.strip():
                error = "no answer in the response"
            else:
                try:
                    parsed = schema.model_validate_json(raw)
                except ValidationError:                  # a draft object before the answer, as Grok writes on the CLI
                    if (obj := last_json_object(raw)) is None:
                        raise
                    parsed = schema.model_validate(obj)
                blanked = uncited(parsed, cited(resp))
    except ValidationError as e:
        error = f"ValidationError: {str(e)[:300]}"
    except ValueError as e:                              # a body that isn't JSON
        error = f"{type(e).__name__}: {str(e)[:200]}"
    except (TimeoutError, httpx.TimeoutException):
        error = f"timeout after {timeout:.0f}s"
    except httpx.HTTPError as e:
        error = redact(f"{type(e).__name__}: {str(e)[:200]}")

    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error,
                                   input_hash, output)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        agent, f"xai:{name}", int((time.monotonic() - started) * 1000), meta.get("input_tokens"),
        meta.get("output_tokens"), meta.get("cost_usd"), parsed is not None, error,
        hashlib.sha256(prompt.encode()).hexdigest()[:16],
        json.dumps({"result": parsed.model_dump() if parsed else None, "response_id": resp.get("id"),
                    "uncited": blanked, **{k: meta.get(k) for k in (
                        "reasoning_tokens", "cached_tokens", "cost_reported", "x_search_calls", "x_posts_fetched",
                        "x_users_fetched")},
                    # Keep the raw tail on failure so bad outputs can be diagnosed.
                    "raw_tail": None if parsed else raw[-4000:]}, default=str),
    )
    if blanked:
        log.info("%s: %d cited posts the search never returned were dropped", agent, len(blanked))
    if parsed is None:
        raise RuntimeError(f"{agent} failed: {error}")
    return parsed, run_id


async def check_key(key: str) -> tuple[bool, str]:
    """The Setup page's Test button: a free live check of a key (GET /v1/api-key, which reads the key's own record)
    -> (works, one plain line to show)."""
    key = (key or "").strip()
    if not key:
        return False, "Paste a key first."
    try:
        r = await client().get(f"{API}/api-key", headers={"Authorization": f"Bearer {key}"},
                               timeout=httpx.Timeout(15.0))
    except httpx.HTTPError:
        return False, "Couldn't reach api.x.ai. Check this machine's internet connection."
    if r.status_code in (400, 401, 403):
        return False, "xAI rejected this key."
    if r.status_code == 429:
        return False, "xAI is limiting requests right now. Try again in a minute."
    if r.status_code >= 400:
        return False, f"xAI answered with an error (HTTP {r.status_code}). Try again later."
    try:
        info = r.json()
    except ValueError:
        return False, "xAI sent an answer that couldn't be read. Try again later."
    if info.get("api_key_disabled") or info.get("api_key_blocked"):
        return False, "xAI has turned this key off. Turn it on or make a new one at console.x.ai."
    if info.get("team_blocked"):
        return False, "xAI has blocked the team that owns this key. Check billing at console.x.ai."
    label = " ".join(str(info.get("name") or "").split())[:60]
    return True, f"Key works (name: {label})." if label else "Key works."
