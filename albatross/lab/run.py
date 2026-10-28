"""The strategy lab's job (hourly): new strategies in, every queued one tested, the paper ones graded, the checkpoints
ruled, the free paper slots filled. Code decides everything here; the strategy researcher only queues configs.

Each strategy is judged on three windows of the same data, never fitted to the later ones:
  discovery  the older history: mean > 0 with t >= the bar, which rises with how many strategies of the family have
             been tried (stats.t_needed), so that testing more can't manufacture a winner
  holdout    the recent days or weeks: positive, and at least as good as the baseline on the same side
  forward    on paper from the day it got a slot: retired early when clearly losing, ruled at its checkpoint. A
             champion must stay positive with t >= the bar for the 20 paper slots and beat the baseline over the same
             days. Champions are evidence, not orders: real money stays the owner's decision.

The clusters behind every t: one per UTC day for memecoins (the whole market moves together day to day), one per
holding period for factor rank. A paper slot goes only to a survivor that clears the bar as it stands now, so one
tested early against a lower bar waits like the rest.

Memecoins also get a second record beside "does it make money": the same days' difference from the baseline (every
group call held 24 h). A rule that loses less than the baseline on most days is loss avoidance, the one kind of edge
the memecoin data has shown (Alpha › Loss avoidance reads it). No memecoin strategy goes on paper before the older
data holds MEME_MIN["days"] days.

Rules revised after the review of 2026-10-09 (migration 043 re-queued everything tested before).
"""

import asyncio
import logging
import random
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

import orjson

from albatross import db
from albatross.lab import evolve, factor, meme, stats

log = logging.getLogger(__name__)

JOB = "strategy_lab"
PAPER_SLOTS = {"meme": 12, "factor": 8}            # 20 at once
NEW_PER_RUN = {"meme": 6, "factor": 4}             # ~240 new strategies a day from code, besides the researcher's
PARENTS = 10                                       # mutations and crossovers come from the best this many
MEME_START = datetime(2026, 9, 30, tzinfo=UTC)     # Sep 29 (39 calls, +201% on average) would sit in every test
MEME_MIN = {"discovery": 100, "holdout": 50, "forward": 100, "days": 20, "forward_days": 14}
FACTOR_MIN = {"discovery": 20, "holdout": 8, "forward": 12}
KILL_MIN = {"meme": 50, "factor": 4}               # forward trades before a losing strategy can be retired early
MEME_CHECKPOINT, FACTOR_CHECKPOINT = timedelta(days=14), timedelta(days=42)
SETTLE = timedelta(days=1)                         # a memecoin trade's 24 h
FACTOR_HOLDOUT = timedelta(days=182)               # 26 weeks: 11 periods couldn't tell anything apart
CHAMPION_BAR = stats.t_needed(sum(PAPER_SLOTS.values()))
BASELINE = {"meme": "every group call, held 24 h",
            "long_short": "factor rank as it runs today, long and short",
            "long_only": "factor rank as it runs today, long only",
            "long_hedged": "factor rank as it runs today, long, hedged with BTC and ETH"}


def _j(v) -> str | None:
    return None if v is None else orjson.dumps(v, default=str).decode()


def _t(v) -> str:
    return "–" if v is None else f"{v:.2f}"


def _load(v):
    return orjson.loads(v) if isinstance(v, str | bytes) else v


def side(family: str, cfg) -> str:
    return "meme" if family == "meme" else cfg.side


# ─── the windows and the statistics ──────────────────────────────────────────
def meme_windows(calls: list[dict], now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """Discovery and holdout from MEME_START: the holdout is the newest quarter of the settled days (at least three)."""
    first, end = max(calls[0]["logged_at"], MEME_START), now - SETTLE
    days = max(1, (end - first).days)
    cut = end - timedelta(days=max(3, round(days / 4)))
    return {"discovery": (first, cut), "holdout": (cut, end)}


def by_day(trades: list[tuple]) -> dict:
    out = defaultdict(list)
    for v, _, d in trades:
        out[d].append(v)
    return {d: sum(vs) / len(vs) for d, vs in out.items()}


def paired(trades: list[tuple], base: list[tuple]) -> dict | None:
    """The strategy's day mean minus the baseline's, over the days both traded: {days, mean, t, share_up}."""
    a, b = by_day(trades), by_day(base)
    diffs = [a[d] - b[d] for d in sorted(a) if d in b]
    s = stats.clustered(diffs, list(range(len(diffs))))
    if not s:
        return None
    return {"days": s["n"], "mean": s["mean"], "t": s["t"], "share_up": round(sum(x > 0 for x in diffs) / len(diffs), 3)}


def meme_stats(cfg: meme.MemeConfig, calls: list[dict], start: datetime, end: datetime,
               base: list[tuple] | None = None) -> dict | None:
    t = meme.trades(cfg, calls, start, end)
    s = stats.clustered([v for v, _, _ in t], [d for _, _, d in t])
    if s is not None and base is not None:
        s["vs_base"] = paired(t, base)
    return s


def factor_stats(ps: list[tuple[date, float]], start: date, end: date) -> dict | None:
    w = factor.window(ps, start, end)
    return stats.clustered([v for v, _ in w], [c for _, c in w])


# ─── the rules ───────────────────────────────────────────────────────────────
def verdict(family: str, disc: dict | None, hold: dict | None, base_hold: dict | None, bar: float) -> tuple[str, str]:
    """(survivor | rejected, why)."""
    need = MEME_MIN if family == "meme" else FACTOR_MIN
    unit = "trades" if family == "meme" else "periods"
    if not disc or disc["n"] < need["discovery"]:
        return "rejected", f"too few {unit} in the older data ({disc['n'] if disc else 0} of {need['discovery']})"
    if disc["mean"] <= 0 or (disc["t"] or 0) < bar:
        return "rejected", f"older data: {disc['mean']:+.2f}% a trade, t {_t(disc['t'])}; needs a positive mean and t of at least {bar:.2f}"
    if family == "meme" and disc["clusters"] < need["days"]:
        return "rejected", f"passed the older data on {disc['clusters']} days; it needs {need['days']} before it can be trusted"
    if not hold or hold["n"] < need["holdout"]:
        return "rejected", f"too few {unit} in the recent data ({hold['n'] if hold else 0} of {need['holdout']})"
    if hold["mean"] <= 0:
        return "rejected", f"recent data: {hold['mean']:+.2f}% a trade, not positive"
    if base_hold and hold["mean"] < base_hold["mean"]:
        return "rejected", f"recent data: {hold['mean']:+.2f}% a trade, behind the baseline's {base_hold['mean']:+.2f}%"
    return "survivor", f"older data t {_t(disc['t'])} against a bar of {bar:.2f}; recent data {hold['mean']:+.2f}% a trade"


def score(family: str, disc: dict | None) -> float | None:
    """What ranks strategies as parents and for paper slots: the older data's t, once there are enough trades for it
    to mean something (three lucky trades can show a t of 40)."""
    need = (MEME_MIN if family == "meme" else FACTOR_MIN)["discovery"]
    return disc["t"] if disc and disc["n"] >= need and disc["t"] is not None else None


def ruling(family: str, fwd: dict | None, base_fwd: dict | None, paper_at: datetime, now: datetime) -> tuple[str, str] | None:
    """A paper strategy's fate today: (champion | retired, why), or None to keep it on paper."""
    if fwd and fwd["n"] >= KILL_MIN[family] and fwd["mean"] < 0 and (fwd["t"] or 0) <= -2:
        return "retired", f"losing on paper: {fwd['mean']:+.2f}% a trade, t {_t(fwd['t'])}"
    due = paper_at + (MEME_CHECKPOINT if family == "meme" else FACTOR_CHECKPOINT)
    if family == "meme":
        enough = fwd and fwd["n"] >= MEME_MIN["forward"] and fwd["clusters"] >= MEME_MIN["forward_days"]
    else:
        enough = fwd and fwd["n"] >= FACTOR_MIN["forward"]
    if now < due or not enough:
        return None
    if fwd["mean"] > 0 and (fwd["t"] or 0) >= CHAMPION_BAR and (not base_fwd or fwd["mean"] > base_fwd["mean"]):
        return "champion", f"on paper {fwd['mean']:+.2f}% a trade, t {_t(fwd['t'])} against a bar of {CHAMPION_BAR:.2f}, ahead of the baseline"
    return "retired", (f"didn't hold up on paper: {fwd['mean']:+.2f}% a trade, t {_t(fwd['t'])}; needs a positive mean, t "
                       f"of at least {CHAMPION_BAR:.2f} and a lead over the baseline")


# ─── new strategies ──────────────────────────────────────────────────────────
INSERT = """INSERT INTO lab_strategies (family, config, config_hash, name, origin, parent_ids, idea)
            VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7) ON CONFLICT (config_hash) DO NOTHING"""


async def queue(family: str, cfg, origin: str, parents: list[int] | None = None, idea: str | None = None) -> bool:
    """Queue a strategy for testing; False when that exact strategy was tried before."""
    d, h = evolve.canonical(family, cfg)
    name = (meme.name if family == "meme" else factor.name)(cfg)
    pool = await db.pool()
    return (await pool.execute(INSERT, family, _j(d), h, name, origin, parents or [], idea)).endswith(" 1")


def candidate(family: str, parents: list[dict], rng: random.Random):
    """(config, origin, parent ids): a mutation or crossover of the best so far 60% of the time, else a random one."""
    if parents and rng.random() < 0.6:
        a = rng.choice(parents)
        ca = evolve.parse(family, _load(a["config"]))
        if rng.random() < 0.3 and len(parents) > 1:
            b = rng.choice([p for p in parents if p["id"] != a["id"]])
            cb = evolve.parse(family, _load(b["config"]))
            cross = evolve.cross_meme if family == "meme" else evolve.cross_factor
            return cross(ca, cb, rng), "crossover", [a["id"], b["id"]]
        mutate = evolve.mutate_meme if family == "meme" else evolve.mutate_factor
        return mutate(ca, rng), "mutation", [a["id"]]
    return (evolve.random_meme if family == "meme" else evolve.random_factor)(rng), "random", []


async def generate(rows: list[dict], rng: random.Random) -> int:
    """This run's new strategies from code. A duplicate of one tried before, or a config a mutation broke, is skipped."""
    n = 0
    for family, want in NEW_PER_RUN.items():
        parents = sorted((r for r in rows if r["family"] == family and r["score"] is not None),
                         key=lambda r: -r["score"])[:PARENTS]
        got = 0
        for _ in range(want * 5):
            if got >= want:
                break
            try:
                cfg, origin, ids = candidate(family, parents, rng)
            except ValueError:
                continue
            if await queue(family, cfg, origin, ids):
                got += 1
        n += got
    return n


# ─── testing (CPU only, off the event loop) ──────────────────────────────────
def baselines(calls: list[dict], frames, now: datetime) -> dict:
    """Each baseline's trades or periods, and its windows."""
    mw = meme_windows(calls, now)
    fcut, fend = (now - FACTOR_HOLDOUT).date(), now.date()
    out = {"mw": mw, "fcut": fcut, "fend": fend, "first_factor": factor.EPOCH,
           "meme": {k: meme.trades(meme.MemeConfig(), calls, *w) for k, w in mw.items()}}
    for s in ("long_short", "long_only", "long_hedged"):
        out[s] = factor.periods(factor.FactorConfig(side=s), frames)
    return out


def base_stats(b: dict, key: str, window: str) -> dict | None:
    if key == "meme":
        t = b["meme"][window]
        return stats.clustered([v for v, _, _ in t], [d for _, _, d in t])
    start, end = (b["first_factor"], b["fcut"]) if window == "discovery" else (b["fcut"], b["fend"])
    return factor_stats(b[key], start, end)


def test_queued(queued: list[dict], tried: dict[str, int], calls: list[dict], frames, now: datetime) -> tuple[list, dict]:
    """(one update per queued strategy, the run's summary of windows and baselines)."""
    b = baselines(calls, frames, now)
    mw, fcut, fend = b["mw"], b["fcut"], b["fend"]
    out = []
    for r in queued:
        f = r["family"]
        tried[f] += 1
        bar = stats.t_needed(tried[f])
        try:
            cfg = evolve.parse(f, _load(r["config"]))
        except ValueError as e:
            out.append((r["id"], "rejected", None, None, bar, None, f"not a valid strategy: {str(e)[:160]}"))
            continue
        if f == "meme":
            disc = meme_stats(cfg, calls, *mw["discovery"], base=b["meme"]["discovery"])
            hold = meme_stats(cfg, calls, *mw["holdout"], base=b["meme"]["holdout"])
        else:
            ps = factor.periods(cfg, frames)
            disc, hold = factor_stats(ps, b["first_factor"], fcut), factor_stats(ps, fcut, fend)
        status, why = verdict(f, disc, hold, base_stats(b, side(f, cfg), "holdout"), bar)
        out.append((r["id"], status, disc, hold, bar, score(f, disc), why))
    summary = {"meme": {"discovery": list(mw["discovery"]), "holdout": list(mw["holdout"]),
                        "baseline": {"name": BASELINE["meme"], "holdout": base_stats(b, "meme", "holdout")}},
               "factor": {"discovery": [b["first_factor"], fcut], "holdout": [fcut, fend],
                          "baseline": {"name": BASELINE["long_short"], "holdout": base_stats(b, "long_short", "holdout")},
                          "baselines": {s: {"name": BASELINE[s], "holdout": base_stats(b, s, "holdout")}
                                        for s in ("long_short", "long_only", "long_hedged")}}}
    return out, summary


def grade_paper(papers: list[dict], calls: list[dict], frames, now: datetime) -> list:
    """(id, forward record with its baseline, ruling or None) per paper or champion strategy."""
    fend = now.date()
    base = {s: factor.periods(factor.FactorConfig(side=s), frames) for s in ("long_short", "long_only", "long_hedged")}
    out = []
    for r in papers:
        f, cfg = r["family"], evolve.parse(r["family"], _load(r["config"]))
        if f == "meme":
            start = max(r["paper_at"], MEME_START)
            bt = meme.trades(meme.MemeConfig(), calls, start, now - SETTLE)
            fwd = meme_stats(cfg, calls, start, now - SETTLE, base=bt)
            base_fwd = stats.clustered([v for v, _, _ in bt], [d for _, _, d in bt])
        else:
            fwd = factor_stats(factor.periods(cfg, frames), r["paper_at"].date(), fend)
            base_fwd = factor_stats(base[cfg.side], r["paper_at"].date(), fend)
        rule = ruling(f, fwd, base_fwd, r["paper_at"], now) if r["status"] == "paper" else None
        out.append((r["id"], {**(fwd or {"n": 0}), "baseline": base_fwd}, rule))
    return out


# ─── the job ─────────────────────────────────────────────────────────────────
async def run(seed: int | None = None) -> dict:
    """strategy_lab -> {"rows": strategies tested, "new", "tested", "survivors", "to_paper", "champions", "retired"}."""
    rng, now, pool = random.Random(seed), datetime.now(UTC), await db.pool()
    for family, cfg in evolve.seeds():
        await queue(family, cfg, "seed")
    out = {"new": await generate([dict(r) for r in await pool.fetch(
        "SELECT id, family, config, score FROM lab_strategies WHERE score IS NOT NULL")], rng),
        "tested": 0, "survivors": 0, "to_paper": 0, "champions": 0, "retired": 0}

    calls = await meme.load()
    frames = await factor.load()
    if not calls or frames[0].empty:
        log.info("%s: no data yet", JOB)
        return {"rows": 0, **out}
    tried = {f: await pool.fetchval("SELECT count(*) FROM lab_strategies WHERE family = $1 AND status <> 'queued'", f)
             for f in PAPER_SLOTS}
    queued = [dict(r) for r in await pool.fetch("SELECT * FROM lab_strategies WHERE status = 'queued' ORDER BY id")]
    results, summary = await asyncio.to_thread(test_queued, queued, tried, calls, frames, now)
    for id_, status, disc, hold, bar, sc, why in results:
        await pool.execute(
            """UPDATE lab_strategies SET status = $2, tested_at = now(), discovery = $3::jsonb, holdout = $4::jsonb,
                 t_needed = $5, score = $6, reason = $7 WHERE id = $1 AND status = 'queued'""",
            id_, status, _j(disc), _j(hold), bar, sc, why)
        out["tested"] += 1
        out["survivors"] += status == "survivor"

    papers = [dict(r) for r in await pool.fetch("SELECT * FROM lab_strategies WHERE status IN ('paper', 'champion')")]
    for id_, fwd, rule in await asyncio.to_thread(grade_paper, papers, calls, frames, now):
        await pool.execute(
            """UPDATE lab_strategies SET forward = $2::jsonb, status = coalesce($3, status),
                 decided_at = CASE WHEN $3::text IS NULL THEN decided_at ELSE now() END,
                 reason = coalesce($4, reason) WHERE id = $1""",
            id_, _j(fwd), rule[0] if rule else None, rule[1] if rule else None)
        if rule:
            out["champions" if rule[0] == "champion" else "retired"] += 1

    for f, slots in PAPER_SLOTS.items():                # free slots: the best survivors that clear today's bar
        held = await pool.fetchval("SELECT count(*) FROM lab_strategies WHERE family = $1 AND status = 'paper'", f)
        if slots > held:
            got = await pool.fetch(
                """UPDATE lab_strategies SET status = 'paper', paper_at = now() WHERE id IN (
                     SELECT id FROM lab_strategies WHERE family = $1 AND status = 'survivor' AND score >= $3
                     ORDER BY score DESC NULLS LAST, id LIMIT $2) RETURNING id""", f, slots - held,
                stats.t_needed(max(tried[f], 1)))
            out["to_paper"] += len(got)

    for f in PAPER_SLOTS:
        summary[f]["tried"] = tried[f]
        summary[f]["bar"] = stats.t_needed(max(tried[f], 1))
    await db.upsert("paper_reports", ["key", "computed_at", "report"],
                    [("lab", now, _j({"families": summary, "run": out, "slots": PAPER_SLOTS,
                                      "champion_bar": CHAMPION_BAR, "min_days": MEME_MIN["days"]}))], ["key"])
    log.info("%s: %s", JOB, out)
    return {"rows": out["tested"], **out}
