"""The CEO: the top of the agent hierarchy, on ChatGPT models.

Twice a day it reads what every desk has to show for itself: the paper books in dollars, the dossier report, every
model agent's graded verdict (or that it has none) and rung, each agent's cost and failures over the last 24 hours,
the jobs that failed and the ChatGPT allowance. It writes a short memo with findings and at most five recommended
actions (cut, pause, resume, promote, change a budget, look into something).

It changes nothing itself: the recommendations are for the owner, who acts on the Output page or by asking. It makes no
market calls, so there is nothing of its own to grade; its cost is tracked like every agent's. It reads graded
records only, never another agent's conclusions. Guards, in code:
- every number it writes must match a number in its inputs (rounding allowed); a sentence or finding that doesn't
  is left out and the memo says how many were. This checks that a number exists, not that it is pinned on the right
  book: the memo is a starting point for the owner, not a record anything else reads;
- every action must name an agent, job or book it was shown, and only a graded agent can be recommended for a cut,
  a pause or a promotion (an agent marked too early or unreviewed can only be looked into or kept).
GPT-6.1 Sol at high effort on the ChatGPT plan (llm/codex.py).
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

import orjson
from pydantic import BaseModel, Field

from albatross import controls, db, tracker
from albatross.agents import cull, graph
from albatross.config import settings
from albatross.llm import codex, openrouter

log = logging.getLogger(__name__)

AGENT = "ceo"
DAILY_USD = 1.0
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
GRADED = {"earns", "no_skill", "harmful"}
NEEDS_GRADE = {"cull": {"no_skill", "harmful"}, "pause": {"no_skill", "harmful"}, "promote": {"earns"}}
FOR_BOOKS = {"keep", "investigate"}

SYSTEM = """You are the CEO of Albatross, a crypto research desk run by AI agents. Twice a day you read what each
desk has to show for itself and write a short memo for the owner, who decides.

The desks: chart and flow analysts and a quant model call direction per coin; an ensemble weights them by their graded
record; a Strategist turns that into trade ideas, which a critic reviews; an Oracle researches the web; the Deep End
scores memecoin calls from Telegram groups. Nothing trades: every call is paper-traded and graded after costs. An
agent that shows no skill moves down a ladder (active, watch, probation, paused, retired).

How to read the inputs:
- paper_books: each book's rule says what it buys and how long it holds. pnl_usd is the result of its finished trades
  (equity minus the starting capital for the factor books); now_pnl_usd adds the open ones at their latest price.
  unit says what median and diff_per_trade are in (% of the stake, or R for the Strategist). interval_95 is the
  range the difference could plausibly be in. A verdict of too_early means the sample is too small: it is not
  evidence either way, so say so instead of reading meaning into it. A book's id starts with "book:".
- agents: verdict is earns, no_skill, harmful, too_early or unreviewed. Unreviewed means the agent makes no market
  calls, so there is nothing to grade: judge it on cost and failures only, and never suggest cutting it for lacking
  a grade. The rung is where it stands on the ladder: on probation it still runs and is graded but its calls no
  longer count in the ensemble; paused, it runs on a tenth of the coins at a tenth of its budget on purpose, so its
  record keeps growing (calls and a small cost from a paused agent are expected, not a fault); retired, it stops.
- cost_24h: api_usd is money spent; allowance_usd is what flat-fee subscription use (Grok, ChatGPT) would cost at
  list price, not extra money; failed counts calls that failed (a try on the ChatGPT plan that failed and was
  retried on OpenRouter isn't counted).
- chatgpt_allowance: used_percent counts all use on the owner's account, their own included; pace_line_percent is the most
  the account may have used by now before the agents stop using the plan and go to OpenRouter;
  reserved_for_owner_percent is kept free for the account holder at the end of the week.

Say what works, what doesn't, what costs money without earning it, and what is broken. Recommend at most five
actions, each on an id exactly as given, with the reason in one sentence, and the same action again as `plain`: one
short sentence the owner can act on, in everyday words, with no ids, no jargon and at most 25 words (it is what Home shows
on the action's button row; your reason sits one click away). Quote only numbers that appear in the
inputs, as written or rounded. No predictions and no trading advice. Plain, direct English, no slogans."""


class Finding(BaseModel):
    topic: str = Field(description="A few words.")
    finding: str = Field(description="One or two sentences.")


class Action(BaseModel):
    kind: Literal["cull", "pause", "resume", "promote", "budget", "investigate", "keep"]
    target: str = Field(description="The id of an agent, job or book exactly as given in the inputs.")
    why: str = Field(description="One sentence.")
    plain: str = Field(default="", description="The same action for the owner in one short everyday sentence: what to do and "
                                                "why, no ids or jargon, at most 25 words.")


class Memo(BaseModel):
    summary: str = Field(description="At most 120 words.")
    findings: list[Finding]
    actions: list[Action]


def _r(v, nd=2):
    return round(v, nd) if isinstance(v, float) else v


def _book(b: dict) -> dict:
    v, vs = b.get("verdict") or {}, b.get("vs_baseline") or {}
    unit = {"%": "% of the stake", "R": "R", "equity": "dollars of equity"}.get(b.get("unit"), b.get("unit"))
    out = {"id": f"book:{b['key']}", "name": b["name"], "lane": b["lane"], "rule": b.get("rule"),
           "since": b.get("since"), "unit": unit, "verdict": v.get("status"), "verdict_words": v.get("words")}
    for k in ("stake_usd", "capital_usd", "taken", "closed", "open", "pnl_usd", "now_pnl_usd", "median", "equity_usd",
              "benchmark_usd"):
        if b.get(k) is not None:
            out[k] = _r(b[k])
    if b.get("win_rate") is not None:
        out["win_rate_pct"] = round(b["win_rate"] * 100, 1)
    if vs:
        out["vs"] = {"diff_per_trade": _r(vs.get("value")), "interval_95": [_r(vs.get("lo")), _r(vs.get("hi"))],
                     "against": vs.get("against")}
    return out


async def digest(now: datetime) -> dict:
    """Everything the memo may use, as precomputed numbers in the units the owner reads."""
    pool = await db.pool()
    books = await pool.fetchrow("SELECT report FROM paper_reports WHERE key = 'books'")
    dossier = await pool.fetchrow("SELECT report FROM paper_reports WHERE key = 'dossier'")
    rep = orjson.loads(books["report"]) if books else {}
    dos = orjson.loads(dossier["report"]) if dossier else {}
    rungs = await controls.rungs()
    ev = await cull.evidence()
    agents = []
    for a in sorted(({k for k, kind in graph.KIND.items() if kind == graph.MODEL} | set(ev)) - cull.EXEMPT - {AGENT}):
        e = ev.get(a)
        v = cull.judge(e) if e else "unreviewed"
        agents.append({"id": a, "name": graph.LABEL.get(a, a), "rung": rungs.get(a, "active"), "verdict": v,
                       "evidence": cull.words(e, v) if e else "makes no market calls, so it has no graded record"})
    costs = [{"id": a["agent"], "name": graph.LABEL.get(a["agent"], a["agent"]), "calls": a["calls"],
              "failed": a["failed"], "api_usd": _r(float(a["cost_usd"])), "allowance_usd": _r(float(a["pool_usd"])),
              "credits": _r(float(a["credits"]), 1)} for a in await tracker.agent_stats(24)]
    jobs = [{"id": r["job"], "failed_runs": r["failed"], "runs": r["runs"], "last_error": (r["last_error"] or "")[:160]}
            for r in await pool.fetch(
                """SELECT job, count(*) AS runs, count(*) FILTER (WHERE status = 'failed') AS failed,
                          (array_agg(error ORDER BY started_at DESC) FILTER (WHERE status = 'failed'))[1] AS last_error
                   FROM job_runs WHERE started_at > $1 GROUP BY job
                   HAVING count(*) FILTER (WHERE status = 'failed') > 0 ORDER BY 3 DESC""", now - timedelta(hours=24))]
    lim = await codex.latest_limits()
    allowance = None
    if lim:
        allowance = {"used_percent": lim["used_percent"],
                     "pace_line_percent": round(codex.pace_line(lim["window_mins"], lim["resets_at"], now,
                                                                settings.codex_reserve_pct, settings.codex_burst_pct), 1),
                     "reserved_for_owner_percent": settings.codex_reserve_pct,
                     "resets": lim["resets_at"].strftime("%b %d %H:%M UTC")}
    return {
        "now": now.strftime("%Y-%m-%d %H:%M UTC"),
        "paper_books": [_book(b) for b in rep.get("books", [])],
        "dossier_report": {"verdict": (dos.get("verdict") or {}).get("status"),
                           "words": (dos.get("verdict") or {}).get("words")} if dos else None,
        "agents": agents,
        "cost_24h": costs,
        "jobs_with_failures_24h": jobs,
        "chatgpt_allowance": allowance,
    }


def pool_of(d: dict) -> list[float]:
    """Every number in the inputs, from values and from the words in them (verdicts, evidence); the timestamp of the
    run and the ids are left out, so they can't vouch for a number."""
    out: list[float] = []

    def walk(node, key=None):
        if isinstance(node, bool) or key in ("now", "id"):
            return
        if isinstance(node, (int, float)):
            out.append(abs(float(node)))
        elif isinstance(node, str):
            out.extend(float(t.replace(",", "")) for t in NUMBER.findall(node))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
    walk(d)
    return out


def grounded(text: str, pool: list[float]) -> bool:
    """Every number in the text matches one in the inputs, as written or rounded to the digits it shows."""
    for tok in NUMBER.findall(text):
        x = float(tok.replace(",", ""))
        dp = len(tok.split(".")[1]) if "." in tok else 0
        if not any(abs(round(v, dp) - x) < 1e-9 or (dp == 0 and abs(v - x) <= 0.5) for v in pool):
            return False
    return True


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def check(memo: Memo, d: dict) -> tuple[Memo, dict[str, int]]:
    """The memo with every ungrounded sentence, finding and action left out, and how many of each were."""
    pool = pool_of(d)
    summary = [s for s in sentences(memo.summary) if grounded(s, pool)]
    findings = [f for f in memo.findings if grounded(f.topic + " " + f.finding, pool)]
    verdict = {a["id"]: a["verdict"] for a in d["agents"]}
    agent_ids = verdict.keys() | {c["id"] for c in d["cost_24h"]}
    book_ids = {b["id"] for b in d["paper_books"]}
    job_ids = {j["id"] for j in d["jobs_with_failures_24h"]}

    def allowed(a: Action) -> bool:
        if not grounded(a.why, pool):
            return False
        if a.target in book_ids:
            return a.kind in FOR_BOOKS
        if a.target in agent_ids:
            return a.kind not in NEEDS_GRADE or verdict.get(a.target) in NEEDS_GRADE[a.kind]
        return a.target in job_ids and a.kind in ("investigate", "resume", "keep")
    # a plain label that quotes a number the inputs don't have is dropped; Home then shows the reason instead
    actions = [a.model_copy(update={"plain": a.plain if grounded(a.plain, pool) else ""})
               for a in memo.actions[:5] if allowed(a)]
    left = {"sentences": len(sentences(memo.summary)) - len(summary), "findings": len(memo.findings) - len(findings),
            "actions": len(memo.actions) - len(actions)}
    return Memo(summary=" ".join(summary), findings=findings, actions=actions), left


def markdown(memo: Memo, names: dict[str, str], left: dict[str, int]) -> str:
    out = [memo.summary.strip() or "_The summary was left out: it quoted numbers that aren't in the inputs._", ""]
    if memo.findings:
        out += ["## Findings", *[f"- **{f.topic.strip().rstrip('.')}.** {f.finding.strip()}" for f in memo.findings],
                ""]
    if memo.actions:
        out += ["## Recommended",
                *[f"- {a.kind.capitalize()}: {names.get(a.target, a.target)}. {a.why.strip()}" for a in memo.actions], ""]
    parts = [f"{n} {what if n != 1 else what[:-1]}" for what, n in left.items() if n]
    if parts:
        out += [(f"_Left out: {', '.join(parts)}, for quoting numbers that aren't in the inputs or for an action it "
                 "can't recommend (only a graded agent can be cut, paused or promoted)._"), ""]
    out.append("_Recommendations only: nothing changes until the owner acts on them._")
    return "\n".join(out)


async def run() -> int:
    """Job ceo (00:55 and 12:55 UTC, after the paper books' :49 pass) -> 1 when a memo was written."""
    now = datetime.now(UTC)
    d = await digest(now)
    memo, run_id = await openrouter.structured(AGENT, Memo, SYSTEM, "INPUTS:\n" + orjson.dumps(d, default=str).decode(),
                                               model=openrouter.SOL, daily_budget_usd=DAILY_USD, temperature=None,
                                               reasoning_effort="high", max_tokens=32000, deadline_s=420)
    kept, left = check(memo, d)
    names = {b["id"]: b["name"] for b in d["paper_books"]} | {a["id"]: a["name"] for a in d["agents"]} | {
        c["id"]: c["name"] for c in d["cost_24h"]}
    pool = await db.pool()
    meta = {"actions": [{"kind": a.kind, "target": a.target, "name": names.get(a.target, a.target), "why": a.why.strip(),
                         "plain": a.plain.strip()} for a in kept.actions]}
    await pool.execute("INSERT INTO reports (kind, title, body_md, signal_ids, meta) VALUES ($1, $2, $3, $4, $5::jsonb)",
                       "ceo", f"{now:%b} {now.day}, {now:%H:%M} UTC", markdown(kept, names, left), [],
                       orjson.dumps(meta).decode())
    log.info("ceo: memo written (%d findings, %d actions, left out %s, run %s)", len(kept.findings), len(kept.actions),
             left, run_id)
    return 1
