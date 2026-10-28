"""The ChatGPT-subscription lane: agent calls through Codex on the ChatGPT plan (added 2026-10-02/03:
cheap models, 5% of the weekly allowance kept for the account holder's own use).

The gateway (deploy/codex_gateway.py) runs `codex exec` on the host as the locked-down `codex` user and listens on a
Unix socket bind-mounted into the ingest container. openrouter.structured() sends a call here first when the agent's
model is one Codex serves (TWINS) and falls back to the same model on OpenRouter whenever this lane is off, paced
off, over the agent's budget or fails, so an agent never stops because the subscription did.

Costs are counted in OpenAI's Codex credits (its rate card; 1 credit = $0.04): the plan's weekly allowance is drawn
in the same units, its size isn't published, and agent_runs.credits against the codex_limits readings measures it.
"""

import asyncio
import base64
import copy
import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import orjson
from pydantic import BaseModel, Field, ValidationError

from albatross import db, github_guard
from albatross.config import settings

log = logging.getLogger(__name__)

# OpenRouter model -> the same model on the subscription.
TWINS = {"openai/gpt-6-luna": "gpt-6-luna", "openai/gpt-6.1-sol": "gpt-6.1-sol"}
# Credits per 1M tokens: input, cached input, output (reasoning tokens are part of output). GPT-5.5, the search model,
# isn't on the rate card we have: its API price ($5 / $0.50 / $30) at $0.04 a credit, as the card's other rows are.
RATES = {"gpt-6-luna": (2.5, 0.25, 12.5), "gpt-6.1-sol": (50.0, 2.5, 250.0), "gpt-5.5": (125.0, 12.5, 750.0)}
SEARCH_MODEL = "gpt-5.5"               # the one model this CLI gives web search without code mode (2026-10-05)
SEARCH_CREDITS = 0.25                  # a web search ($0.01, OpenAI's search price as OpenRouter lists it)
# An agent may use this many times its daily dollar budget of the plan's allowance (at list price); its dollars on
# OpenRouter stay capped at the budget itself, so a day the plan runs dry can't cost more than it always could.
PLAN_BUDGET_MULTIPLE = 10.0
CREDIT_USD = 0.04
EFFORTS = {"low", "medium", "high"}
STALE = timedelta(minutes=20)          # an allowance reading older than this turns the lane off
WEEK_MINS = 10080
EXPERIMENT_SHARE = 0.8                 # shadows stop at 80% of the pace line, so the regular agents keep the plan
# Web research stops at 90%: when the plan runs tight it gives way before the regular agents, as it is the cheapest to
# move (a fit report costs ~$0.03 on OpenRouter, ~3 credits here), and after the shadows (which cost nothing when cut).
SEARCH_SHARE = 0.9
SEARCH_SLOTS = 2                       # long research calls hold at most 2 of the gateway's 8 slots
_search_slots: asyncio.Semaphore | None = None
# A GitHub link in any spelling, with or without its scheme: every domain github_guard names, and its subdomains.
GITHUB_LINK = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)*(?:" + "|".join(re.escape(d) for d in github_guard.DOMAINS)
                         + r")(?![a-z0-9-])(?:[/?#][^\s\"'<>]*)?", re.IGNORECASE)
LIMIT_COOLDOWN_S = 15 * 60             # after the plan reports its limit, try again this much later
QUEUE_S = 90                           # the gateway's own wait for a free slot (CODEX_GATEWAY_QUEUE_S)
SUFFIX = "\n\nRespond with a single JSON object matching this JSON Schema:\n"
_blocked_until = 0.0
_experiments_blocked_until = 0.0       # a shadow's limit error rests the shadows only
EXPERIMENT_SLOTS = 2                   # shadows share 2 of the gateway's 8 slots; with research's 2, the regular
                                       # agents always have 4
_experiment_slots: asyncio.Semaphore | None = None


def search_slots() -> asyncio.Semaphore:
    global _search_slots
    if _search_slots is None:
        _search_slots = asyncio.Semaphore(SEARCH_SLOTS)
    return _search_slots


def experiment_slots() -> asyncio.Semaphore:
    global _experiment_slots
    if _experiment_slots is None:                 # created in the running loop (ingest has one for its whole life)
        _experiment_slots = asyncio.Semaphore(EXPERIMENT_SLOTS)
    return _experiment_slots


class Unavailable(RuntimeError):
    """This call can't go through the subscription; the caller falls back to OpenRouter."""


def twin(model: str) -> str | None:
    return TWINS.get(model)


def credits(model: str, usage: dict, searches: int = 0) -> float | None:
    rate = RATES.get(model)
    if rate is None or not usage:
        return None
    total = usage.get("input_tokens") or 0
    cached = min(usage.get("cached_input_tokens") or 0, total)
    out = usage.get("output_tokens") or 0
    return ((total - cached) * rate[0] + cached * rate[1] + out * rate[2]) / 1e6 + searches * SEARCH_CREDITS


def strict(schema: dict) -> dict | None:
    """A pydantic JSON schema in the strict form OpenAI's structured outputs accept: every object closed and every
    property required (types unchanged, so a field with a default must be filled; an Optional one may be null), no
    defaults. None when strict mode can't express it (a free-form dict); the call then asks for JSON in the prompt."""
    s = copy.deepcopy(schema)
    ok = True

    def fix(node: dict) -> None:
        nonlocal ok
        node.pop("default", None)
        if node.get("type") == "object" or "properties" in node:
            if "properties" not in node or isinstance(node.get("additionalProperties"), dict):
                ok = False
            node["additionalProperties"] = False
            node["required"] = list((node.get("properties") or {}).keys())
            for sub in (node.get("properties") or {}).values():
                if isinstance(sub, dict):
                    fix(sub)
        for key in ("items", "not"):
            if isinstance(node.get(key), dict):
                fix(node[key])
        for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
            for sub in node.get(key) or []:
                if isinstance(sub, dict):
                    fix(sub)
        for sub in (node.get("$defs") or {}).values():
            if isinstance(sub, dict):
                fix(sub)

    fix(s)
    return s if ok else None


class PageCheck(BaseModel):
    url: str
    loaded: bool = Field(description="true only when the page opened with real content: not an error, a 'not found', "
                                     "a login wall or an empty page")
    title: str = Field(description="the page's own title as it shows it; \"\" when it didn't load. Never guess one.")


class Checked(BaseModel):
    pages: list[PageCheck]


CONFIRM_MAX = 30
CONFIRM_RULES = """Open each page listed below, one at a time, exactly as written, with your web tool. Search for nothing
else and open nothing else. For each page say whether it loaded with real content (not an error, a "page not found",
a login wall or an empty page) and give the page's own title as it shows it. Never guess a title: a page that didn't
load has loaded false and title ""."""
CITED = re.compile(r"https?://[^\s\"'<>\\]+")


def _norm(url: str) -> str:
    """A URL in comparable form: no scheme, no www., no fragment, no utm_ tags, no end slash."""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not k.lower().startswith("utm_")])
    return (u.netloc.lower().removeprefix("www.") + u.path.rstrip("/") + (f"?{q}" if q else "")).rstrip(".,;")


def _strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def _clean(url: str) -> str:
    """A URL as found in prose: no trailing punctuation or backtick, and a closing bracket only when it closes one in
    the URL."""
    url = url.rstrip(".,;:!?`")
    while (url.endswith(")") and url.count("(") < url.count(")")) or (url.endswith("]") and url.count("[") < url.count("]")):
        url = url[:-1].rstrip(".,;:!?`")
    return url


def titles_agree(claimed: str, shown: str) -> bool:
    """A page's own title backs the title an answer gave it: half the claimed title's words (3+ letters) are in it.
    Nothing claimed, nothing to check. A second-pass call can be steered by another page it opens, so a confirmed
    page must also look like the page the first answer described."""
    words = {w for w in re.findall(r"[a-z0-9]{3,}", (claimed or "").lower())}
    if not words:
        return True
    seen = set(re.findall(r"[a-z0-9]{3,}", (shown or "").lower()))
    return 2 * len(words & seen) >= len(words)


def unopened(answer: BaseModel, opened: list[str]) -> list[str]:
    """Every URL an answer cites (in any of its text fields) that its search didn't open."""
    seen = {_norm(u) for u in opened}
    cited = [_clean(m) for s in _strings(answer.model_dump()) for m in CITED.findall(s)]
    return [u for u in dict.fromkeys(cited) if u and _norm(u) not in seen]


async def confirm(agent: str, urls: list[str], *, daily_budget_usd: float) -> dict[str, str]:
    """A second pass on the plan for pages an answer cited without opening (its search saw them in results only):
    {url as the caller wrote it: the page's own title} for each page that the gateway saw opened AND that this fresh
    call, with no stake in the first answer, reports loaded with real content and a title. An open alone proves
    nothing (a made-up URL "opens" too, tested), so a page that 404s, shows an error or gets no title is left out.
    GitHub links are never asked for. Logged as <agent>_confirm, so it doesn't use the agent's own call cap."""
    asked = list(dict.fromkeys(u.strip() for u in urls if u and u.strip().startswith(("http://", "https://"))
                               and not github_guard.hosted(u)))[:CONFIRM_MAX]
    if not asked:
        return {}
    checked, _run_id, opened, _github = await search(f"{agent}_confirm", Checked, CONFIRM_RULES, "\n".join(asked),
                                                     effort="low", deadline_s=min(420, 60 + 20 * len(asked)),
                                                     daily_budget_usd=daily_budget_usd)
    seen = {_norm(u) for u in opened}
    said = {_norm(p.url): " ".join(p.title.split())[:300] for p in checked.pages if p.loaded and p.title.strip()}
    return {u: said[_norm(u)] for u in asked if _norm(u) in seen and _norm(u) in said}


def mask(text: str | None) -> str | None:
    """Text to store with every GitHub link replaced (a search answer may cite one it never opened)."""
    return GITHUB_LINK.sub("[github link]", text) if text else text


def first_object(text: str) -> str:
    """The first complete JSON object in the text (without a strict schema Luna sometimes adds a stray token after
    the closing brace, measured 2026-10-02)."""
    i = text.find("{")
    if i < 0:
        return text
    obj, _ = json.JSONDecoder().raw_decode(text[i:])
    return orjson.dumps(obj).decode()


def pace_line(window_mins: int, resets_at: datetime, now: datetime, reserve: float, burst: float) -> float:
    """The most of the weekly allowance (percent) the lane may have used by now: an even spread of everything but
    The account holder's reserve over the window, plus a small burst so the first hours of a week aren't starved."""
    window = timedelta(minutes=window_mins)
    elapsed = (now - (resets_at - window)) / window
    cap = 100.0 - reserve
    return min(cap, cap * min(max(elapsed, 0.0), 1.0) + burst)


async def latest_limits() -> dict | None:
    pool = await db.pool()
    row = await pool.fetchrow("SELECT * FROM codex_limits ORDER BY checked_at DESC LIMIT 1")
    return dict(row) if row else None


async def pace(now: datetime | None = None, experiment: bool = False, search: bool = False) -> tuple[bool, str]:
    """(open, why not): the lane is open while the week's use (the account holder's own included) is under the pace line; for an
    experiment (a shadow) under EXPERIMENT_SHARE of it, for web research under SEARCH_SHARE."""
    now = now or datetime.now(UTC)
    if not settings.codex_enabled:
        return False, "the subscription lane is switched off (CODEX_ENABLED)"
    if time.monotonic() < _blocked_until:
        return False, "the plan reported its usage limit; waiting before trying again"
    if experiment and time.monotonic() < _experiments_blocked_until:
        return False, "a shadow hit the plan's limit; shadows wait before trying again"
    lim = await latest_limits()
    if lim is None or now - lim["checked_at"] > STALE:
        return False, "no recent reading of the weekly allowance"
    if lim["limit_reached"]:
        return False, f"the plan reports its limit reached ({lim['limit_reached']})"
    if (lim.get("other_percent") or 0) >= 100.0 - settings.codex_reserve_pct:
        return False, f"a shorter usage window is {lim['other_percent']:.0f}% used"
    line = pace_line(lim["window_mins"], lim["resets_at"], now, settings.codex_reserve_pct, settings.codex_burst_pct)
    if experiment:
        line *= EXPERIMENT_SHARE
    elif search:
        line *= SEARCH_SHARE
    if lim["used_percent"] >= line:
        which = "experiments’" if experiment else "research" if search else "pace"
        return False, f"{lim['used_percent']:.0f}% of the week used, over the {which} line of {line:.0f}%"
    return True, ""


async def plan_usd_today(agent: str) -> float:
    """An agent's use of the plan today, in dollars at list price (credits at $0.04)."""
    pool = await db.pool()
    return await pool.fetchval(
        """SELECT coalesce(sum(credits), 0) * $2 FROM agent_runs
           WHERE agent = $1 AND started_at >= date_trunc('day', now())""", agent, CREDIT_USD)


async def api_usd_today(agent: str) -> float:
    """An agent's OpenRouter dollars today: the money its budget caps."""
    pool = await db.pool()
    return await pool.fetchval(
        "SELECT coalesce(sum(cost_usd), 0) FROM agent_runs WHERE agent = $1 AND started_at >= date_trunc('day', now())",
        agent)


def _transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(uds=settings.codex_gateway_sock)


async def gateway(method: str, path: str, body: dict | None = None, timeout: float = 30) -> tuple[int, dict]:
    async with httpx.AsyncClient(transport=_transport(), timeout=httpx.Timeout(timeout)) as c:
        r = await c.request(method, f"http://codex{path}", json=body)
    try:
        out = r.json()
    except ValueError:
        out = None
    if not isinstance(out, dict):
        return r.status_code, {"ok": False, "kind": "failed", "error": r.text[:300]}
    return r.status_code, out


def weekly(body: dict) -> tuple[dict | None, float | None]:
    """The gateway's windows -> (the weekly one, the fullest other one's percent). The pacer spreads the week; any
    shorter window (none on a Pro plan in October 2026) only closes the lane when it nears full."""
    wins = [w for w in body.get("windows") or [] if w.get("used_percent") is not None and w.get("window_mins")]
    week = max((w for w in wins if w["window_mins"] >= WEEK_MINS), key=lambda w: w["window_mins"], default=None)
    others = [float(w["used_percent"]) for w in wins if w is not week]
    return week, max(others, default=None)


async def read_limits() -> int:
    """Job codex_limits (every 5 min): the plan's usage windows from the gateway into codex_limits -> rows written.
    No weekly window, no row: the reading goes stale and the lane closes."""
    if not settings.codex_enabled:
        return 0
    status, body = await gateway("GET", "/v1/limits", timeout=60)
    week, other = weekly(body) if status == 200 else (None, None)
    if week is None:
        raise RuntimeError(f"gateway limits: HTTP {status}, no weekly window: {str(body.get('error') or body)[:200]}")
    await db.upsert("codex_limits", ["checked_at", "used_percent", "window_mins", "resets_at", "plan", "limit_reached",
                                     "other_percent"],
                    [(datetime.now(UTC), float(week["used_percent"]), int(week["window_mins"]),
                      datetime.fromtimestamp(int(week["resets_at"]), UTC), body.get("plan"),
                      body.get("limit_reached"), other)], ["checked_at"])
    return 1


async def structured[T: BaseModel](agent: str, schema: type[T], system: str, user: str, *, model: str, effort: str,
                                   deadline_s: float, daily_budget_usd: float, queue_s: float | None = None,
                                   experiment: bool = False, not_after: float | None = None) -> tuple[T, int]:
    """One call on the subscription -> (parsed, agent_run_id). Raises Unavailable (the caller falls back) when the
    lane is off or paced off, the agent is past its budget, or the call fails; every attempt that reached the gateway
    is an agent_runs row with its tokens and credits. An experiment (a shadow) first waits for one of its 2 slots and
    is admitted only then: still before not_after (time.monotonic()), still under the experiments' line and its
    budget, so a call that queued for minutes can't run past either."""
    if not experiment:
        await _admit(agent, daily_budget_usd, experiment, not_after)
        return (await _call(agent, schema, system, user, TWINS[model], effort, deadline_s, queue_s, experiment))[:2]
    async with experiment_slots():
        await _admit(agent, daily_budget_usd, experiment, not_after)
        return (await _call(agent, schema, system, user, TWINS[model], effort, deadline_s, queue_s, experiment))[:2]


async def search[T: BaseModel](agent: str, schema: type[T], system: str, user: str, *, effort: str,
                               deadline_s: float, daily_budget_usd: float, image: tuple[bytes, str] | None = None,
                               queue_s: float | None = None) -> tuple[T, int, list[str], int]:
    """A web-researched answer on the plan (GPT-5.5 with its live search; since
    2026-10-05) -> (parsed, agent_run_id, the pages the search opened, how many of them were on GitHub). The opened
    pages are the evidence: a citation the agent keeps must be one of them. GitHub pages are dropped from it (the
    search can't be kept off GitHub, accepted on this account; nothing of them is stored). image:
    (bytes, extension), shown to the model. Raises Unavailable like structured()."""
    extra = {"search": True}
    if image:
        extra |= {"image_b64": base64.b64encode(image[0]).decode(), "image_ext": image[1]}
    async with search_slots():
        await _admit(agent, daily_budget_usd, False, None, search=True)
        parsed, run_id, resp = await _call(agent, schema, system, user, SEARCH_MODEL, effort, deadline_s, queue_s,
                                           False, extra)
    opened = [u for u in resp.get("opened") or [] if isinstance(u, str)]
    kept = [u for u in opened if not github_guard.hosted(u)]
    if len(kept) < len(opened):             # its answer may draw on GitHub pages: discarded, never stored
        pool = await db.pool()
        await pool.execute("UPDATE agent_runs SET ok = false, error = $2, output = NULL WHERE id = $1", run_id,
                           f"discarded: the search opened {len(opened) - len(kept)} GitHub page(s)")
        raise Unavailable(f"{agent}: the search opened GitHub pages; discarded")
    return parsed, run_id, kept, 0


class OutOfTime(Unavailable):
    """A shadow's run ended before this call got a slot: it goes nowhere, not even to OpenRouter."""


async def _admit(agent: str, daily_budget_usd: float, experiment: bool, not_after: float | None,
                 search: bool = False) -> None:
    if not_after is not None and time.monotonic() > not_after:
        raise OutOfTime(f"{agent}: the run's time is up")
    try:
        is_open, why = await pace(experiment=experiment, search=search)
        spent = await plan_usd_today(agent) if is_open else 0.0
    except Exception as e:  # noqa: BLE001 - a lane we can't check is a closed lane, never a failed agent call
        raise Unavailable(f"can't check the allowance: {type(e).__name__}: {str(e)[:160]}") from e
    if not is_open:
        raise Unavailable(why)
    cap = daily_budget_usd * (1.0 if experiment else PLAN_BUDGET_MULTIPLE)
    if spent >= cap:
        raise Unavailable(f"{agent} used ${spent:.2f} of the plan today, its ${cap:.2f} cap at list price")


async def _call[T: BaseModel](agent: str, schema: type[T], system: str, user: str, codex_model: str, effort: str,
                              deadline_s: float, queue_s: float | None, experiment: bool,
                              extra: dict | None = None) -> tuple[T, int, dict]:
    global _blocked_until, _experiments_blocked_until
    effort = effort if effort in EFFORTS else "low"
    js = schema.model_json_schema()
    st = strict(js)
    instructions = system if st is not None else system + SUFFIX + orjson.dumps(js).decode()
    body = {"model": codex_model, "effort": effort, "instructions": instructions, "input": user, "schema": st,
            "timeout_s": deadline_s, "queue_s": QUEUE_S if queue_s is None else min(queue_s, QUEUE_S), **(extra or {})}
    input_hash = hashlib.sha256(orjson.dumps([instructions, user])).hexdigest()[:16]
    started = time.monotonic()
    status, resp, parsed, error = 0, {}, None, None
    try:
        status, resp = await gateway("POST", "/v1/exec", body, timeout=deadline_s + body["queue_s"] + 15)
    except httpx.HTTPError as e:
        raise Unavailable(f"gateway unreachable: {type(e).__name__}: {str(e)[:160]}") from e
    usage = resp.get("usage") or {}
    raw = resp.get("text")
    if status == 200 and raw:
        try:
            parsed = schema.model_validate_json(raw if st is not None else first_object(raw))
        except (ValidationError, ValueError, TypeError) as e:          # orjson's encode error is a TypeError
            error = f"{type(e).__name__}: {str(e)[:400]}"
    else:
        error = f"{resp.get('kind') or 'failed'}: {str(resp.get('error') or status)[:400]}"
        if resp.get("kind") == "limit":
            if experiment:
                _experiments_blocked_until = time.monotonic() + LIMIT_COOLDOWN_S
            else:
                _blocked_until = time.monotonic() + LIMIT_COOLDOWN_S
    if resp.get("kind") in ("busy", "bad_request") and not usage:
        raise Unavailable(error or "gateway refused the call")         # nothing ran: no row
    touched = any(github_guard.hosted(u) for u in resp.get("opened") or [] if isinstance(u, str))
    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, credits, ok, error,
                                   input_hash, output)
           VALUES ($1, $2, $3, $4, $5, NULL, $6, $7, $8, $9, $10) RETURNING id""",
        agent, f"codex:{codex_model}", int((time.monotonic() - started) * 1000), usage.get("input_tokens"),
        usage.get("output_tokens"), credits(codex_model, usage, int(resp.get("searches") or 0)), parsed is not None,
        error, input_hash,
        None if touched else mask(parsed.model_dump_json() if parsed else (orjson.dumps({"raw": raw}).decode()
                                                                             if raw else None)))
    if parsed is None:
        raise Unavailable(error or "no answer")
    return parsed, run_id, resp
