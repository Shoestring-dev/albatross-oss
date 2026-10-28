"""Backtested idea scorecard, shared by /api/ideas/scorecard, the Ideas tab and the report writer.

n counts graded outcomes only. Baselines use the SAME windows (same entry time, same exit time, same
costs): always_long, and random = a coin-flip side (the mean of always-long and always-short), whose
win rate sits below 50% because costs make both sides lose on small moves. Verdicts are computed here
so the dashboard and the reports judge a source the same way.
"""

import math

from albatross import db
from albatross.features.engine import TFS

MIN_N = 30
# Micro-cap calls (hundreds a day from busy Telegram groups, plus any bot) stay out of the LLM prompts that read
# this scorecard (reports, Oracle): one row per caller would swamp them. The dashboard and API show them.
NOT_DEX = "c.market <> 'dex'"
Z = 1.96                    # rough 95% band on a mean R; ideas overlap in time, so real bands are wider
BASE = ("win_rate", "avg_r", "avg_net_return_pct")
_G = "FILTER (WHERE o.status = 'graded')"
_ORDER = {tf: i for i, tf in enumerate(TFS)}


async def rows(where: str = "TRUE", args: tuple = (), by_tf: bool = True) -> list[dict]:
    """One row per policy x source_id x call status (proposed/vetoed) x call_type, and x tf when by_tf, over
    the outcomes matching `where` (SQL on o = trade_call_outcomes, c = trade_calls). call_type is the
    micro-cap call's kind (buy | mention | bot_entry) from its evidence, NULL for every other source."""
    pool = await db.pool()
    recs = await pool.fetch(
        f"""SELECT o.policy, c.source_id, c.status, c.evidence->>'call_type' AS call_type,
                   {"c.tf" if by_tf else "NULL::text"} AS tf,
                   count(*) {_G} AS n,
                   count(*) FILTER (WHERE o.status = 'open') AS n_open,
                   count(*) FILTER (WHERE o.status = 'ungradeable') AS n_ungradeable,
                   avg((o.net_return_pct > 0)::int::float8) {_G} AS win_rate,
                   avg(o.r_multiple) {_G} AS avg_r,
                   stddev_samp(o.r_multiple) {_G} / sqrt(nullif(count(o.r_multiple) {_G}, 0)) AS avg_r_stderr,
                   sum(o.r_multiple) {_G} AS total_r,
                   avg(o.net_return_pct) {_G} AS avg_net_return_pct,
                   avg((o.long_net_pct > 0)::int::float8) {_G} AS long_win_rate,
                   avg(o.long_net_pct / nullif(o.risk_pct, 0)) {_G} AS long_avg_r,
                   avg(o.long_net_pct) {_G} AS long_avg_net_return_pct,
                   avg((((o.long_net_pct > 0)::int + (o.short_net_pct > 0)::int) / 2.0)::float8) {_G}
                     AS random_win_rate,
                   avg((o.long_net_pct + o.short_net_pct) / 2 / nullif(o.risk_pct, 0)) {_G} AS random_avg_r,
                   avg((o.long_net_pct + o.short_net_pct) / 2) {_G} AS random_avg_net_return_pct
            FROM trade_call_outcomes o JOIN trade_calls c ON c.id = o.call_id
            WHERE {where} GROUP BY 1, 2, 3, 4, 5""",
        *args,
    )
    out = [shape(dict(r)) for r in recs]
    return sorted(out, key=lambda r: (r["policy"], r["source_id"], r["status"], r["call_type"] or "",
                                      _ORDER.get(r["tf"], -1)))


async def open_marks(where: str = "TRUE", args: tuple = ()) -> list[dict]:
    """Ideas still open, per policy x source_id x call status, marked to the last closed candle after
    exit costs. Unrealised, so dashboard only: never mixed into rows() or the prompts that read it."""
    pool = await db.pool()
    recs = await pool.fetch(
        f"""SELECT o.policy, c.source_id, c.status, count(*) AS n, avg(o.r_multiple) AS avg_r,
                   avg(o.net_return_pct) AS avg_net_return_pct
            FROM trade_call_outcomes o JOIN trade_calls c ON c.id = o.call_id
            WHERE o.status = 'open' AND ({where}) GROUP BY 1, 2, 3""",
        *args,
    )
    return [dict(r) for r in recs]


def shape(r: dict) -> dict:
    row = {k: r[k] for k in ("policy", "source_id", "status", "tf", "n", "n_open", "n_ungradeable",
                             "win_rate", "avg_r", "avg_r_stderr", "total_r", "avg_net_return_pct")}
    row["call_type"] = r.get("call_type")
    row["always_long"] = {k: r[f"long_{k}"] for k in BASE}
    row["random"] = {k: r[f"random_{k}"] for k in BASE}
    row["note"] = ("nothing graded yet" if not r["n"] else
                   f"only {r['n']} graded: too few to tell skill from luck (need >= {MIN_N})"
                   if r["n"] < MIN_N else None)
    row["vs_baselines"] = vs_baselines(row)
    return row


def vs_baselines(r: dict) -> str:
    """Mean R against the better of always-long and random on the same windows."""
    base = [b for b in (r["always_long"]["avg_r"], r["random"]["avg_r"]) if b is not None]
    if r["n"] < MIN_N or r["avg_r"] is None or r["avg_r_stderr"] is None or not base:
        return "too few to tell"
    edge = r["avg_r"] - max(base)
    if abs(edge) <= Z * r["avg_r_stderr"]:
        return "within noise of baselines"
    return "beats baselines" if edge > 0 else "behind baselines"


def critic(rows: list[dict]) -> list[dict]:
    """Proposed minus vetoed mean R per policy and source, from tf-less rows. Positive = the ideas the
    critic let through did better than the ones it vetoed, i.e. its vetoes avoided worse trades."""
    # Micro-cap callers and the bot have no critic (call_type set): nothing to judge there.
    by = {(r["policy"], r["source_id"], r["status"]): r for r in rows if r["tf"] is None and not r.get("call_type")}
    out = []
    for (policy, src, status), p in by.items():
        if status != "proposed":
            continue
        v = by.get((policy, src, "vetoed")) or {"n": 0, "avg_r": None, "avg_r_stderr": None}
        diff = band = None
        if p["avg_r"] is not None and v["avg_r"] is not None:
            diff = p["avg_r"] - v["avg_r"]
            if p["avg_r_stderr"] is not None and v["avg_r_stderr"] is not None:
                band = Z * math.hypot(p["avg_r_stderr"], v["avg_r_stderr"])
        if min(p["n"], v["n"]) < MIN_N or band is None:
            verdict = "too few to judge the critic"
        elif abs(diff) <= band:
            verdict = "no measurable difference"
        else:
            verdict = "vetoes avoided worse trades" if diff > 0 else "vetoed ideas did better"
        out.append({"policy": policy, "source_id": src, "proposed_n": p["n"], "vetoed_n": v["n"],
                    "avg_r_diff": diff, "band": band, "verdict": verdict})
    return out
