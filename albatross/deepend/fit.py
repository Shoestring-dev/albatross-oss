"""Culture fit (A3 in docs/SPEC-DEEP-END.md): does this coin's meme land with the trenches right now?

For each token that crosses the bar, one OpenRouter call with the model's own web search finds where the meme comes
from (pop culture, a viral post, a person, an animal, a trend), whether that reference is live right now, how it
lands with the audience (degen traders, Gen Z humour, the crypto-Twitter metas of the moment) and whether this
contract is the original or a copycat. What the model can't look up is computed here and handed to it: the token's
own story (name, ticker, description, links and image from DexScreener, pump.fun and, once built, chain.jupiter_intel),
the same-name contracts we have seen (which is oldest, which is deepest), the group calls, the latest X read
(xintel.section_x) and the trenches meta from the hourly market pulse (social_snapshots scope 'market').

Evidence gate (CLAUDE.md: LLM outputs stored as data must be grounded): a citation survives only when its URL is
one the call's own web search returned (the url_citation annotations); the others are dropped and kept on the
record. A report left with no citation is still stored, with confidence 'low', and served labelled "(unsourced)".

Triggers: a warm token (deepend/watch.py) that >= 2 groups called, or with >= $25k liquidity, or trending now, and
without a report in the last 24 h (so a token that stays warm is refreshed daily): never-researched first, then hot
before warm, the most-called and the deepest, at most DEEP_FIT_PER_HOUR research calls an hour. research_now()
serves an on-demand request. Budget: DEEP_FIT_DAILY_USD (default $3) of OpenRouter spend a day, counted in
agent_runs like every agent; the job keeps to an even pace through the day. Measured 2026-09-30 on six staging
tokens: $0.017-0.040 a report (mean $0.032), 25-45 s; 140 of 317 warm tokens crossed the bar at once, so $3 covers
the ~90 most called and deepest a day.
"""

import asyncio
import base64
import hashlib
import inspect
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import orjson
from pydantic import BaseModel, Field, ValidationError

from albatross import controls, db, github_guard
from albatross.config import settings
from albatross.deepend import cfg, watch
from albatross.http import client
from albatross.llm import codex, openrouter
from albatross.venues import dexscreener

log = logging.getLogger(__name__)

AGENT = "deep_fit"
# Chosen 2026-09-30 on staging tokens (tests/fixtures/deep_fit): Gemini 3.8 Flash reads the image and the inputs
# well and judges the reference soberly; OpenRouter's web search tool on the Perplexity engine finds the same-day
# story (a blog post, a White House order, a species announcement) with real URLs and excerpts, at $0.005 a search
# and 1-3 searches a report. Google's own search (Gemini native) found the same stories with more colour but ran
# 4-8 searches at $0.014 (up to $0.12 a report) behind redirect links; Exa missed the same-day story; Grok 4.7
# native cost $0.10 and missed it; Claude Sonnet 5.5 cost $0.06 and answered empty. Switch the model with
# LLM_MODEL_OVERRIDES=deep_fit=<model> (it must support tool calls).
MODEL = "google/gemini-3.8-flash"
# GitHub is left out of every search, and dropped from the links, the image and the citations (nothing hosted on
# GitHub reaches a model; github_guard.py).
SEARCH = {"type": "openrouter:web_search",
          "parameters": {"engine": "perplexity", "max_results": 5, "max_uses": 3,
                         "excluded_domains": github_guard.SEARCH_EXCLUDE}}
REASONING = "medium"                    # low was ~$0.005 cheaper and ~10 s faster, and a little flatter
DAILY_USD = cfg.env_float("DEEP_FIT_DAILY_USD", 3.0)
PER_HOUR = cfg.env_int("DEEP_FIT_PER_HOUR", 6)
PER_RUN = 2                             # the job runs every 10 minutes; a research call takes 10-60 s
GROUPS, LIQUIDITY = 2, 25_000.0         # the bar (or trending now)
FRESH = timedelta(hours=24)             # younger reports are fresh; older ones are refreshed while the token is warm
RECENT = timedelta(hours=1)             # an on-demand request inside this reuses the last report
BACKOFF = timedelta(hours=6)            # a token whose research failed waits this long before the job retries it
DEADLINE_S = 180
MAX_TOKENS = 8000
PULSE_MAX_AGE = timedelta(hours=12)
IMAGE_MAX_BYTES = 1_500_000
IMAGE_HOSTS = ("cdn.dexscreener.com", "dd.dexscreener.com", "ipfs.io", "cf-ipfs.com", "gateway.pinata.cloud",
               "storage.googleapis.com", "arweave.net", "static.jup.ag")
IMAGE_HOST_SUFFIXES = (".mypinata.cloud", ".ipfs.dweb.link", ".ipfs.w3s.link", ".ipfs.nftstorage.link")
PUMP_URL = "https://frontend-api-v3.pump.fun/coins-v2/{}"      # undocumented; best effort, pump.fun mints only


# ─── the report ──────────────────────────────────────────────────────────────
class Citation(BaseModel):
    title: str = ""
    url: str


class Report(BaseModel):
    score: int = Field(ge=0, le=10, description="culture fit right now, 0 none .. 10 perfect; never a price call")
    summary: str = Field(description="2-3 sentences: the verdict and the main reasons")
    meme: str = Field(description="the origin and reference: what the name, ticker and image point to")
    reference_status: Literal["live", "fading", "evergreen", "dead", "unclear"] = Field(
        description="is the reference itself live right now (this week or month), fading, evergreen, dead, or unclear")
    meta: str = Field(description="how it fits what is running in the trenches right now")
    metas: list[str] = Field(default_factory=list, description="up to 3 short lowercase meta tags, e.g. ai, animals, horror")
    audience: str = Field(description="who it lands with and how: degen traders, Gen Z humour, crypto Twitter")
    originality: str = Field(description="original or copycat, from the same-name contracts in the inputs")
    virality: list[str] = Field(default_factory=list, description="up to 4 short factors that help it spread")
    risks: list[str] = Field(default_factory=list,
                             description="up to 4 short risks: trademark, seasonal, offensive, dead meme, copycat...")
    citations: list[Citation] = Field(default_factory=list,
                                      description="the web pages behind the factual claims, exact URLs from your search")


LABELS = ((9, "strong fit"), (7, "good fit"), (5, "some fit"), (3, "weak fit"), (0, "no fit"))


def label(score: int) -> str:
    return next(name for floor, name in LABELS if score >= floor)


SYSTEM = """You are the culture-fit researcher on The Deep End, a memecoin evidence desk. Traders in the trenches
(Solana and EVM micro-caps, degen Telegram groups, crypto Twitter, Gen Z humour) ask you one thing: does this
coin's meme resonate with that audience right now? You judge cultural fit, never price: no buy or sell language.

Research with your web search before you answer:
1. Origin and reference: what the name, ticker, description, links and image point to (a film or show, a viral
   post or clip, a person, an animal, a news event, a trend, crypto-native lore, or nothing identifiable). Search
   the reference itself, not only the ticker; coins launched today are rarely indexed, their reference usually is.
2. Is the reference live right now (in the news or trending this week or month), fading, evergreen, or dead?
3. How it lands: with degen traders, with Gen Z humour, and with the crypto-Twitter metas running now (AI, animals,
   political, celebrity, seasonal, horror, gaming, ...). The trenches meta and the X read in the inputs are what
   our own sensors saw in the last hours; use them for "what is running now".
4. Originality: the same-name contracts in the inputs are counted by us from every contract our groups called.
   Trust those numbers and say whether this one is the original, the deepest, or a copycat.

Scoring (fit only): 0-2 no identifiable reference, or a dead one; 3-4 a weak or niche reference, off-meta;
5-6 a recognisable reference with some pull, or on-meta but generic; 7-8 a strong, instantly legible reference
that is live or rides a running meta; 9-10 a reference exploding right now, clip-friendly, and this is the original.
Copycats of a running coin rarely score above 5.

Citations: cite the pages behind your factual claims about the reference, with their title and their exact URL as
your search returned it. Pages your search did not return are checked and dropped, so never write a URL from
memory. When you can't pin the reference down, say "unclear" rather than guess. Keep every text field short and
plain: one or two sentences, no hype."""

PROMPT = """Today is {today} UTC. Research this token's culture fit.

TOKEN AND CONTEXT (computed by us; times are UTC):
{inputs}"""


# ─── inputs ──────────────────────────────────────────────────────────────────
TOKEN_SQL = """
SELECT coalesce(s.symbol, t.symbol) AS symbol, s.name, s.launchpad, s.created_at, s.liquidity_usd, s.mcap_usd,
       s.volume_h24_usd, t.first_seen
FROM (SELECT $1::text AS chain, $2::text AS address) k
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN LATERAL (SELECT t.symbol, t.first_seen FROM micro_tokens t
                   WHERE t.address = k.address AND coalesce(t.resolved_chain, t.chain) = k.chain
                   ORDER BY t.first_seen LIMIT 1) t ON true
WHERE s.token IS NOT NULL OR t.first_seen IS NOT NULL"""

SAME_NAME_SQL = """
WITH toks AS (
  SELECT DISTINCT ON (coalesce(t.resolved_chain, t.chain), t.address)
         coalesce(t.resolved_chain, t.chain) AS chain, t.address, coalesce(s.symbol, t.symbol) AS symbol, s.name,
         coalesce(s.created_at, t.first_seen) AS created_at, s.liquidity_usd
  FROM micro_tokens t LEFT JOIN micro_state s ON s.chain = coalesce(t.resolved_chain, t.chain) AND s.token = t.address
  WHERE lower(coalesce(s.symbol, t.symbol)) = lower($1) OR lower(s.name) = lower($2)
  ORDER BY coalesce(t.resolved_chain, t.chain), t.address, t.first_seen)
SELECT toks.*, c.n_calls, c.n_groups, c.first_call
FROM toks LEFT JOIN LATERAL (
  SELECT count(*) AS n_calls, count(DISTINCT coalesce(c.group_id::text, c.group_name)) AS n_groups,
         min(c.called_at) AS first_call
  FROM micro_calls c WHERE c.address = toks.address) c ON true
ORDER BY toks.created_at NULLS LAST
LIMIT 40"""

CALLS_SQL = """
SELECT coalesce(c.group_name, c.group_id::text, 'unknown') AS grp, count(*) AS n, min(c.called_at) AS first_at
FROM micro_calls c JOIN micro_tokens t ON t.chain = c.chain AND t.address = c.address
WHERE c.address = $2 AND coalesce(t.resolved_chain, t.chain) = $1
GROUP BY 1 ORDER BY 3"""

PULSE_SQL = """SELECT ts, narratives FROM social_snapshots WHERE scope = 'market' AND source = 'grok_x'
               ORDER BY ts DESC LIMIT 1"""


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if t else None


def _age(t: datetime | None, now: datetime) -> str | None:
    if not t:
        return None
    m = max(0, int((now - t).total_seconds() // 60))
    return f"{m // 1440}d {m % 1440 // 60}h" if m >= 1440 else f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def _usd(v: float | None) -> str | None:
    if v is None:
        return None
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.0f}K" if v >= 1e3 else f"${v:.0f}"


def copycats(me: dict, rows: list[dict]) -> dict:
    """Same-ticker and same-name contracts among everything our groups called, and where this one ranks by age and
    by liquidity (deterministic, so the model only puts it in words). me: chain, address, symbol, name, created_at,
    liquidity_usd."""
    sym, nm = (me.get("symbol") or "").strip().lower(), (me.get("name") or "").strip().lower()
    others = [r for r in rows if (r["chain"], r["address"]) != (me["chain"], me["address"])]
    older = [r for r in others if r["created_at"] and me.get("created_at") and r["created_at"] < me["created_at"]]
    deeper = [r for r in others if (r["liquidity_usd"] or 0) > (me.get("liquidity_usd") or 0)]

    def brief(r: dict) -> dict:
        same = [k for k, v, mine in (("ticker", r["symbol"], sym), ("name", r["name"], nm))
                if mine and (v or "").strip().lower() == mine]
        return {"chain": r["chain"], "address": r["address"][:6] + "…" + r["address"][-4:], "symbol": r["symbol"],
                "name": r["name"], "same": same, "created": _iso(r["created_at"]),
                "liquidity": _usd(r["liquidity_usd"]), "calls": r["n_calls"] or 0, "groups": r["n_groups"] or 0,
                "first_call": _iso(r["first_call"])}

    oldest = sorted(others, key=lambda r: r["created_at"] or datetime.max.replace(tzinfo=UTC))[:4]
    deepest = sorted(others, key=lambda r: -(r["liquidity_usd"] or 0))[:4]
    shown = oldest + [r for r in deepest if r not in oldest]
    return {
        "same_ticker": sum(1 for r in others if sym and (r["symbol"] or "").strip().lower() == sym),
        "same_name": sum(1 for r in others if nm and (r["name"] or "").strip().lower() == nm),
        "this_is_oldest": not older, "this_is_deepest": not deeper,
        "older_contracts": len(older), "deeper_contracts": len(deeper),
        "others": [brief(r) for r in shown],
    }


async def _maybe(fn, *args):
    """Another builder's function, when it exists: its absence or failure costs one input, never the report."""
    if fn is None:
        return None
    try:
        r = fn(*args)
        return await r if inspect.isawaitable(r) else r
    except Exception as e:  # noqa: BLE001
        log.info("%s: %s(%s) failed: %s", AGENT, getattr(fn, "__name__", fn), args[-1], e)
        return None


def _link(links: dict, kind: str, url) -> None:
    if (isinstance(url, str) and url.startswith(("http://", "https://")) and not github_guard.hosted(url)
            and url not in links.values()):
        kind = {"twitter": "x"}.get(kind, kind)
        if kind in ("x", "telegram", "website") and kind not in links:
            links[kind] = url
        else:
            links.setdefault("other", [])
            if url not in links["other"] and len(links["other"]) < 4:
                links["other"].append(url)


async def profile(chain: str, address: str, launchpad: str | None) -> dict:
    """The token's own story (name, description, links, image) from whichever sources know it: builder A's Jupiter
    intel once it exists, DexScreener's token info, and pump.fun's coin record for pump.fun mints."""
    out: dict = {"name": None, "symbol": None, "description": None, "image": None, "links": {}, "nsfw": None,
                 "sources": []}
    from albatross.deepend import chain as chain_intel
    j = await _maybe(getattr(chain_intel, "jupiter_intel", None), chain, address)
    if isinstance(j, dict):
        out["sources"].append("jupiter")
        out["name"], out["symbol"] = j.get("name"), j.get("symbol")
        out["description"] = j.get("description") or None
        out["image"] = j.get("logo") or j.get("icon") or j.get("image")
        for kind, url in (j.get("links") or {}).items():
            _link(out["links"], kind, url)
        for kind in ("twitter", "telegram", "website", "discord", "tiktok", "instagram", "otherUrl"):
            _link(out["links"], kind, j.get(kind))
    try:
        pairs, _ = await dexscreener.tokens(chain, [address])
        mine = [p for p in pairs if (p.get("baseToken") or {}).get("address", "").lower() == address.lower()]
        if mine:
            out["name"] = out["name"] or mine[0]["baseToken"].get("name")
            out["symbol"] = out["symbol"] or mine[0]["baseToken"].get("symbol")
        if info := next((p["info"] for p in mine if p.get("info")), None):
            out["sources"].append("dexscreener")
            out["image"] = out["image"] or info.get("imageUrl")
            for w in info.get("websites") or []:
                _link(out["links"], "website", w.get("url"))
            for s in info.get("socials") or []:
                _link(out["links"], s.get("type") or "other", s.get("url"))
    except Exception as e:  # noqa: BLE001
        log.info("%s: dexscreener %s: %s", AGENT, address, e)
    if chain == "solana" and (launchpad == "pump.fun" or address.endswith("pump")):
        try:
            r = await client().get(PUMP_URL.format(address), timeout=8.0)
            if r.status_code == 200 and isinstance(p := r.json(), dict):
                out["sources"].append("pump.fun")
                out["name"], out["symbol"] = out["name"] or p.get("name"), out["symbol"] or p.get("symbol")
                out["description"] = out["description"] or (p.get("description") or "").strip() or None
                out["image"] = out["image"] or p.get("image_uri")
                out["nsfw"] = p.get("nsfw")
                for kind in ("twitter", "telegram", "website"):
                    _link(out["links"], kind, p.get(kind))
        except Exception as e:  # noqa: BLE001
            log.info("%s: pump.fun %s: %s", AGENT, address, e)
    if out["description"]:
        out["description"] = out["description"][:600]
    if github_guard.hosted(out["image"]):
        out["image"] = None
    return out


def image_ok(url: str | None) -> bool:
    """A token's metadata is written by whoever launched it: fetch its image only over https from known hosts."""
    try:
        u = urlsplit(url or "")
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host in IMAGE_HOSTS or host.endswith(IMAGE_HOST_SUFFIXES))


async def image_part(url: str | None) -> dict | None:
    """The token's image as an inline data URL, so the model sees the meme itself (known hosts, no redirects,
    at most 1.5 MB)."""
    if not image_ok(url):
        return None
    try:
        async with client().stream("GET", url, timeout=10.0, follow_redirects=False) as r:
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if r.status_code != 200 or ctype not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                return None
            data = b""
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > IMAGE_MAX_BYTES:
                    return None
        return {"type": "image_url", "image_url": {"url": f"data:{ctype};base64,{base64.b64encode(data).decode()}"}}
    except Exception as e:  # noqa: BLE001
        log.info("%s: image %s: %s", AGENT, url, e)
        return None


def _x_brief(x: dict | None) -> dict | None:
    if not isinstance(x, dict) or x.get("status") not in ("fresh", "stale"):
        return None
    return {"as_of": _iso(x.get("as_of")) if isinstance(x.get("as_of"), datetime) else x.get("as_of"),
            **{k: x.get(k) for k in ("mood_label", "posts_h", "unique_authors", "summary", "why_it_matters", "kols")},
            "notable": [{"author": n.get("author"), "followers": n.get("followers"), "stance": n.get("stance"),
                         "text": (n.get("text") or "")[:240]} for n in (x.get("notable") or [])[:4]],
            "account": x.get("account")}


def _pulse_brief(row, now: datetime) -> dict | None:
    """The latest market pulse: CT's mood, headlines and trending coins, plus whatever trenches meta it carries
    (builder C adds a memecoin-meta search to it; passed through as it comes, capped)."""
    if row is None or now - row["ts"] > PULSE_MAX_AGE:
        return None
    n = orjson.loads(row["narratives"]) if isinstance(row["narratives"], (str, bytes)) else (row["narratives"] or {})
    out = {"as_of": _iso(row["ts"]), "age": _age(row["ts"], now), "mood": n.get("market_mood"),
           "trending": [f"{t.get('ticker')}: {t.get('why')}" for t in n.get("trending") or []][:8],
           "headlines": [h.get("text") for h in n.get("headlines") or []][:5]}
    extra = {k: v for k, v in n.items()
             if k not in ("market_mood", "trending", "headlines", "coins_read", "agent_run_id") and v}
    if extra:
        out["trenches"] = orjson.dumps(extra).decode()[:4000]
    return out


async def inputs(chain: str, address: str, now: datetime | None = None) -> dict | None:
    """Everything the researcher gets about one token, or None for a token nobody can name."""
    now = now or datetime.now(UTC)
    pool = await db.pool()
    tok = dict(await pool.fetchrow(TOKEN_SQL, chain, address) or {}) or dict.fromkeys(
        ("symbol", "name", "launchpad", "created_at", "liquidity_usd", "mcap_usd", "volume_h24_usd", "first_seen"))
    prof = await profile(chain, address, tok["launchpad"])
    tok["symbol"], tok["name"] = tok["symbol"] or prof["symbol"], tok["name"] or prof["name"]
    if not (tok["symbol"] or tok["name"]):
        return None
    same = [dict(r) for r in await pool.fetch(SAME_NAME_SQL, tok["symbol"], tok["name"])]
    groups = [dict(r) for r in await pool.fetch(CALLS_SQL, chain, address)]
    pulse = await pool.fetchrow(PULSE_SQL)
    from albatross.deepend import xintel
    x = await _maybe(getattr(xintel, "section_x", None), chain, address)
    created = tok["created_at"] or tok["first_seen"]
    return {
        "token": {"chain": chain, "address": address, "symbol": tok["symbol"], "name": tok["name"],
                  "launchpad": tok["launchpad"], "created": _iso(created), "age": _age(created, now),
                  "description": prof["description"], "links": prof["links"], "image": prof["image"],
                  "nsfw_flag": prof["nsfw"], "liquidity": _usd(tok["liquidity_usd"]), "mcap": _usd(tok["mcap_usd"]),
                  "volume_24h": _usd(tok["volume_h24_usd"]), "profile_sources": prof["sources"]},
        "copycats": copycats({"chain": chain, "address": address, **tok, "created_at": created}, same),
        "group_calls": {"calls": sum(g["n"] for g in groups), "groups": len(groups),
                        "first_call": _iso(groups[0]["first_at"]) if groups else None,
                        "by_group": [{"group": g["grp"], "calls": g["n"], "first": _iso(g["first_at"])}
                                     for g in groups[:8]]},
        "x_read": _x_brief(x),
        "trenches_meta": _pulse_brief(pulse, now),
    }


def messages(inp: dict, image: dict | None, now: datetime) -> list[dict]:
    body = dict(inp, token={k: v for k, v in inp["token"].items() if k != "image"})
    text = PROMPT.format(today=now.strftime("%Y-%m-%d %H:%M"),
                         inputs=orjson.dumps(body, option=orjson.OPT_INDENT_2).decode())
    if image:
        text += "\n\nThe token's image is attached."
    schema = orjson.dumps(Report.model_json_schema()).decode()
    return [{"role": "system", "content": SYSTEM + "\n\nAnswer with a single JSON object matching this JSON Schema:\n"
             + schema},
            {"role": "user", "content": [{"type": "text", "text": text}, *([image] if image else [])]}]


# ─── the evidence gate ───────────────────────────────────────────────────────
def norm(url: str) -> str:
    """A URL in comparable form: no scheme, host lowercased without www., no fragment, no utm_ tags, no end slash."""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not k.lower().startswith("utm_")])
    return (u.netloc.lower().removeprefix("www.") + u.path.rstrip("/") + (f"?{q}" if q else "")).rstrip(".,;")


def evidence(resp: dict) -> dict[str, dict]:
    """{normalised url: {url, title}} of every web result the call's own searches returned (OpenRouter lists each
    one as a url_citation annotation, with the excerpt the model read)."""
    out: dict[str, dict] = {}
    msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
    for a in msg.get("annotations") or []:
        c = a.get("url_citation") or {}
        if a.get("type") == "url_citation" and c.get("url") and not github_guard.hosted(c["url"]):
            out.setdefault(norm(c["url"]), {"url": c["url"], "title": c.get("title") or ""})
    return out


def gate(cites: list[Citation], seen: dict[str, dict]) -> tuple[list[Citation], list[str]]:
    """Keep the citations whose URL the call's own search returned; the rest are dropped (and reported)."""
    kept, dropped, keys = [], [], set()
    for c in cites:
        k = norm(c.url)
        if c.url.strip().startswith(("http://", "https://")) and k in seen and k not in keys:
            keys.add(k)
            kept.append(Citation(title=(c.title or seen[k]["title"] or urlsplit(c.url).netloc)[:200], url=c.url.strip()))
        else:
            dropped.append(c.url)
    return kept, dropped


# ─── the call ────────────────────────────────────────────────────────────────
def parse(raw: str | None) -> Report:
    """The JSON object in the answer (models with search sometimes wrap it in a code fence or a sentence)."""
    if not raw or not raw.strip():
        raise ValueError("empty answer")
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j < i:
        raise ValueError(f"no JSON object in the answer: {raw[:200]!r}")
    return Report.model_validate_json(raw[i:j + 1])


async def _attempt(msgs: list[dict], *, model: str, input_hash: str):
    """One call -> (report or None, evidence, run_id, error, transient, response). usage.cost includes the searches."""
    started = time.monotonic()
    body = {"model": model, "messages": msgs, "max_tokens": MAX_TOKENS, "tools": [SEARCH],
            "usage": {"include": True}}              # temperature left at the default, as Google advises for Gemini 3
    if REASONING and model not in openrouter.ROUTERS:
        body["reasoning"] = {"effort": REASONING}
    resp: dict = {}
    report, error, transient, raw = None, None, False, None
    try:
        resp = await asyncio.wait_for(openrouter._post(body), timeout=DEADLINE_S)
        choice = resp["choices"][0]
        raw = choice["message"].get("content")
        try:
            if choice.get("finish_reason") == "length":
                raise ValueError(f"output truncated at max_tokens={MAX_TOKENS}")
            report = parse(raw)
        except ValidationError as e:                 # the wrong shape: garbled JSON is retried, a schema slip isn't
            error = f"ValidationError: {str(e)[:400]}"
            transient = any(err.get("type", "").startswith("json_") for err in e.errors())
        except ValueError as e:                      # empty, cut off, or no JSON at all: worth one retry
            error, transient = f"ValueError: {str(e)[:400]}", True
    except (KeyError, IndexError, TypeError) as e:
        error = f"{type(e).__name__}: {str(e)[:400]}"
    except httpx.HTTPStatusError as e:
        error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        transient = e.response.status_code in (400, 429, 500, 502, 503, 504)
    except TimeoutError:
        error, transient = f"no response within {DEADLINE_S}s", True
    except httpx.HTTPError as e:
        error, transient = f"{type(e).__name__}: {str(e)[:200]}", True
    seen = evidence(resp) if resp else {}
    usage = resp.get("usage") or {}
    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error,
                                   input_hash, output)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        AGENT, openrouter.recorded(model, resp.get("model")), int((time.monotonic() - started) * 1000),
        usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost"), report is not None, error,
        input_hash,
        orjson.dumps({"report": report.model_dump(), "evidence": [v["url"] for v in seen.values()]}).decode()
        if report else (orjson.dumps({"raw": raw[:4000]}).decode() if raw else None))
    return report, seen, run_id, error, transient, resp


async def ask(msgs: list[dict], model: str):
    """-> (report, evidence, agent_run_id, cost_usd). One retry for transient failures, the second without the
    image (a provider that can't read it answers 400). Raises BudgetExceeded past the day's budget."""
    if not settings.openrouter_api_key:
        raise openrouter.no_key(AGENT, "the ChatGPT plan can't research now")
    if await controls.paused(AGENT):
        raise openrouter.BudgetExceeded(f"{AGENT} is paused on the Output page")
    if (spent := await openrouter.spent_today(AGENT)) >= (daily := await daily_usd()):
        raise openrouter.BudgetExceeded(f"{AGENT} spent ${spent:.2f} of ${daily:.2f} today")
    input_hash = hashlib.sha256(orjson.dumps(msgs)).hexdigest()[:16]
    cost = 0.0
    for attempt in (1, 2):
        report, seen, run_id, error, transient, resp = await _attempt(msgs, model=model, input_hash=input_hash)
        cost += float((resp.get("usage") or {}).get("cost") or 0)
        if report is not None:
            return report, seen, run_id, cost
        if not transient or attempt == 2:
            break
        log.warning("%s: attempt 1 failed (%s); retrying once", AGENT, error)
        if error.startswith("HTTP 400"):
            msgs = without_image(msgs)
    raise RuntimeError(f"{AGENT} call failed: {error}")


async def ask_on_plan(inp: dict, image: dict | None, now: datetime):
    """-> (report, evidence, agent_run_id, cost_usd 0) from the plan's search: the same prompt and image, the pages the
    search opened as the evidence the gate checks. Raises BudgetExceeded when paused; codex.Unavailable when the plan
    can't take it (paced off, over its cap, failed), and the caller goes to OpenRouter."""
    if await controls.paused(AGENT):
        raise openrouter.BudgetExceeded(f"{AGENT} is paused on the Output page")
    calls = inp.get("group_calls") or {}
    public = {**inp, "group_calls": {k: v for k, v in calls.items() if k != "by_group"}}    # group names stay ours
    msgs = messages(public, None, now)
    text = msgs[1]["content"][0]["text"] + ("\n\nThe token's image is attached." if image else "")
    report, run_id, opened, _github = await codex.search(
        AGENT, Report, SYSTEM + "\n\n" + openrouter.SEARCH_RULES, text, effort=REASONING, deadline_s=DEADLINE_S + 120,
        daily_budget_usd=await daily_usd(), image=image_bytes(image))
    seen = {norm(u): {"url": u, "title": ""} for u in opened}
    claimed = {norm(c.url): c.title for c in report.citations}
    for u, page_title in (await openrouter.opened_now(AGENT, report, opened, await daily_usd())).items():
        if codex.titles_agree(claimed.get(norm(u), ""), page_title):     # the page the answer described
            seen[norm(u)] = {"url": u, "title": page_title}
    return report, seen, run_id, 0.0


def image_bytes(image: dict | None) -> tuple[bytes, str] | None:
    """An image part's data URL -> (bytes, extension) for the plan's gateway."""
    url = ((image or {}).get("image_url") or {}).get("url") or ""
    head, _, data = url.partition(",")
    ext = {"data:image/png;base64": "png", "data:image/jpeg;base64": "jpg", "data:image/webp;base64": "webp",
           "data:image/gif;base64": "gif"}.get(head)
    return (base64.b64decode(data), ext) if ext and data else None


def without_image(msgs: list[dict]) -> list[dict]:
    return [{**m, "content": [p for p in m["content"] if p.get("type") == "text"]} if m["role"] == "user" else m
            for m in msgs]


# ─── research and storage ────────────────────────────────────────────────────
async def research(chain: str, address: str, why: list[str] | None = None) -> dict | None:
    """Research one token and store the report. -> the stored row, or None for a token nobody can name."""
    now = datetime.now(UTC)
    inp = await inputs(chain, address, now)
    if inp is None:
        return None
    model = openrouter.model_for(AGENT, MODEL)
    image = await image_part(inp["token"]["image"])
    try:                        # the ChatGPT plan's search first: no API spend while it has room
        report, seen, run_id, cost = await ask_on_plan(inp, image, now)
        model = f"codex:{codex.SEARCH_MODEL}"
    except codex.Unavailable as e:
        log.info("%s: the plan can't research now (%s), using OpenRouter", AGENT, str(e)[:200])
        report, seen, run_id, cost = await ask(messages(inp, image, now), model)
    kept, dropped = gate(report.citations, seen)
    confidence = "normal" if kept else "low"
    at = datetime.now(UTC)
    full = report.model_dump() | {
        "label": label(report.score), "citations": [c.model_dump() for c in kept],
        "dropped_citations": [codex.mask(u) for u in dropped],
        "evidence_urls": len(seen), "image_seen": image is not None, "why": why or [], "cost_usd": round(cost, 5),
        "inputs": inp}
    row = (chain, address, at, run_id, model, report.score, label(report.score), orjson.dumps(full).decode(),
           orjson.dumps(full["citations"]).decode(), confidence)
    await db.upsert("de_fit_reports", ["chain", "address", "researched_at", "run_id", "model", "score", "label",
                                       "report", "citations", "confidence"], [row], ["chain", "address", "researched_at"])
    log.info("%s: %s %s scored %d (%s), %d of %d citations kept, $%.4f", AGENT, chain, inp["token"]["symbol"],
             report.score, confidence, len(kept), len(report.citations), cost)
    return {"chain": chain, "address": address, "researched_at": at, "run_id": run_id, "score": report.score,
            "label": label(report.score), "report": full, "citations": full["citations"], "confidence": confidence}


LATEST_SQL = """SELECT researched_at, score, label, report, citations, confidence FROM de_fit_reports
                WHERE chain = $1 AND address = $2 ORDER BY researched_at DESC LIMIT 1"""
FIELDS = ("summary", "meme", "meta", "audience", "originality")


def section(row: dict | None, now: datetime, *, budget_left: bool = True) -> dict:
    """The dossier's "fit" block (docs/SPEC-DEEP-END.md section 4) from the latest stored report."""
    if row is None:
        return {"status": "none" if budget_left else "budget", "as_of": None, "score": None, "label": None,
                **dict.fromkeys(FIELDS), "risks": [], "citations": []}
    rep = row["report"] if isinstance(row["report"], dict) else orjson.loads(row["report"])
    cites = row["citations"] if isinstance(row["citations"], list) else orjson.loads(row["citations"] or "[]")
    return {"status": "fresh" if now - row["researched_at"] < FRESH else "stale", "as_of": row["researched_at"],
            "score": row["score"], "label": row["label"] + (" (unsourced)" if row["confidence"] == "low" else ""),
            **{k: rep.get(k) for k in FIELDS}, "risks": list(rep.get("risks") or []),
            "citations": [{"title": c.get("title"), "url": c.get("url")} for c in cites]}


async def section_fit(chain: str, address: str) -> dict | None:
    """The latest report as the dossier's "fit" block: fresh (< 24 h) or stale; without one, status 'none' (not
    researched yet) or 'budget' (today's research budget is spent)."""
    pool = await db.pool()
    row = await pool.fetchrow(LATEST_SQL, chain, address)
    left = row is not None or await openrouter.spent_today(AGENT) < await daily_usd()
    return section(dict(row) if row else None, datetime.now(UTC), budget_left=left)


_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


async def research_now(chain: str, address: str) -> dict | None:
    """On demand (a trader asked): research unless a report from the last hour exists, then serve the section.
    Past the day's budget it serves what is stored (status 'budget' when nothing is)."""
    async with _locks[(chain, address)]:
        pool = await db.pool()
        at = await pool.fetchval("SELECT max(researched_at) FROM de_fit_reports WHERE chain = $1 AND address = $2",
                                 chain, address)
        if at is None or datetime.now(UTC) - at >= RECENT:
            try:
                await research(chain, address, ["on demand"])
            except openrouter.BudgetExceeded as e:
                log.info("%s", e)
    return await section_fit(chain, address)


# ─── the job ─────────────────────────────────────────────────────────────────
CANDIDATES_SQL = """
WITH k AS (SELECT * FROM unnest($1::text[], $2::text[]) AS k(chain, address)),
g AS (SELECT coalesce(t.resolved_chain, t.chain) AS chain, c.address,
             count(DISTINCT coalesce(c.group_id::text, c.group_name)) AS n_groups, count(*) AS n_calls
      FROM micro_calls c JOIN micro_tokens t ON t.chain = c.chain AND t.address = c.address
      WHERE c.address = ANY($2::text[]) GROUP BY 1, 2),
f AS (SELECT chain, address, max(researched_at) AS researched_at FROM de_fit_reports
      WHERE address = ANY($2::text[]) GROUP BY 1, 2)
SELECT k.chain, k.address, coalesce(g.n_groups, 0) AS n_groups, coalesce(g.n_calls, 0) AS n_calls,
       s.liquidity_usd, f.researched_at
FROM k LEFT JOIN g ON g.chain = k.chain AND g.address = k.address
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN f ON f.chain = k.chain AND f.address = k.address"""

_failed: dict[tuple[str, str], datetime] = {}


def bar(r: dict) -> list[str]:
    """Why a token deserves research (empty: it doesn't)."""
    why = []
    if (r.get("n_groups") or 0) >= GROUPS:
        why.append(f"{r['n_groups']} groups")
    if (r.get("liquidity_usd") or 0) >= LIQUIDITY:
        why.append(f"liquidity {_usd(r['liquidity_usd'])}")
    if "trending" in (r.get("why") or []):
        why.append("trending")
    return why


def due(rows: list[dict], now: datetime, room: int) -> list[dict]:
    """The tokens to research this run, most deserving first: never researched before refreshes, hot before warm,
    then the most groups and the deepest pool. A report younger than FRESH, or a failure younger than BACKOFF,
    waits."""
    out = []
    for r in rows:
        why = bar(r)
        if not why or (r.get("researched_at") and now - r["researched_at"] < FRESH):
            continue
        if (failed := _failed.get((r["chain"], r["address"]))) and now - failed < BACKOFF:
            continue
        out.append(r | {"because": why + (["refresh"] if r.get("researched_at") else [])})
    out.sort(key=lambda r: (r.get("researched_at") is not None, r.get("tier") != "hot", -(r.get("n_groups") or 0),
                            -(r.get("liquidity_usd") or 0)))
    return out[:max(0, room)]


async def candidates() -> list[dict]:
    """Warm and hot tokens (watch.py) with their group count, liquidity and last report."""
    heat = {(r["chain"], r["address"]): r for r in await watch.tokens()}
    if not heat:
        return []
    pool = await db.pool()
    rows = await pool.fetch(CANDIDATES_SQL, [k[0] for k in heat], [k[1] for k in heat])
    return [dict(r) | {"tier": heat[(r["chain"], r["address"])]["tier"], "why": heat[(r["chain"], r["address"])]["why"]}
            for r in rows]


async def daily_usd() -> float:
    """DEEP_FIT_DAILY_USD times the budget scale set on the Output page."""
    return DAILY_USD * await controls.scale(AGENT)


def paced(spent: float, now: datetime, daily: float = DAILY_USD) -> bool:
    """Is the job inside an even pace through the day's budget, with one hour's share of slack? A busy morning
    can't spend the evening's research (on-demand requests only answer to the daily budget)."""
    day = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)) / timedelta(days=1)
    return spent < daily * min(1.0, day + 1 / 24)


async def run() -> int:
    """deep_fit_research: research the tokens that crossed the bar, within the hourly cap and the day's budget."""
    # paced on its dollars and its use of the plan alike, so a busy morning can't take the evening's research
    plan = await codex.plan_usd_today(AGENT) + await codex.plan_usd_today(f"{AGENT}_confirm")      # and its 2nd pass
    spent = await openrouter.spent_today(AGENT) + plan / codex.PLAN_BUDGET_MULTIPLE
    if not paced(spent, datetime.now(UTC), await daily_usd()):
        log.info("%s: $%.2f of $%.2f spent today, ahead of pace; waiting", AGENT, spent, DAILY_USD)
        return 0
    pool = await db.pool()
    used = await pool.fetchval(
        "SELECT count(*) FROM agent_runs WHERE agent = $1 AND started_at > now() - interval '1 hour'", AGENT)
    now = datetime.now(UTC)
    todo = due(await candidates(), now, min(PER_RUN, PER_HOUR - used))
    done = 0
    for c in todo:
        key = (c["chain"], c["address"])
        try:
            if await research(*key, c["because"]):
                done += 1
                continue
            log.info("%s: nothing names %s %s yet", AGENT, *key)
        except openrouter.BudgetExceeded as e:
            log.info("%s", e)
            break
        except Exception:  # noqa: BLE001 - one token's failure must not stop the others
            log.exception("%s: research of %s %s failed", AGENT, *key)
        _failed[key] = now                  # failed, or nothing to research yet: the others go first for a while
    return done


JOBS: dict = {"deep_fit_research": run}
SCHEDULE: list = [("deep_fit_research", {"minute": "*/10", "second": 35})]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE: dict = {"deep_fit_research": 600}
