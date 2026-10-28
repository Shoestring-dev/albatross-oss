"""Paper books: what following each kind of call would have made or lost, in dollars, after every cost. Nothing is
traded. Every 15 minutes (job paper_books) each book is rebuilt from scratch out of the ledger (trade_calls and the
backtester's graded outcomes), the dossier history and the daily candles, and the result is stored whole in
paper_reports for Calls › Paper books. Why: docs/SPEC-LEDGER.md §9 and docs/PLAN-LEARNING-LOOP.md (Phase 1, the Deep
End track).

The books, set on 2026-10-02. The Deep End rules were fixed without looking at the result of any call that had a
dossier score (82 had finished by then); the score cut-off came from the scores alone. Factor rank was chosen on its
history (about ten factors tried), so only its live weeks count.
  The Deep End: $100 a trade at the call's ledger price, priced by the DEX cost model (sim.simulate_dex: pool fee, price
  impact on both legs, gas), sold 24 h later unless noted. One position per coin: a call on a coin the book still holds
  is skipped, so a coin called by five groups is one trade, not five. A trade counts in a card's dollars once the call's
  24 h are graded, in every book alike: a book that sells early (take_profit) would otherwise bank its winners while
  the other books' twins are still open. Until then it is open, marked at its latest price ("if sold now"). A call
  whose candles stop before its 24 h (the coin stopped trading, or our price source failed; the grader gives up two
  days later) counts at its last price, in every book: leaving those out would drop exactly the pump-then-die calls.
    calls        every Telegram call: the baseline the others are measured against
    clean        calls whose dossier showed no red flag when the call came in
    score        calls whose dossier scored SCORE_MIN or more when the call came in (the top quarter of calls then)
    take_profit  every call, sold at the first minute's close that nets 2x (sim.TAKE_PROFIT_PCT), else at 24 h
    fomo         FOMO ranked traders' buys (ranks 1-150)
    bot          the bot's real-money entries, priced and held the same way: a reference, not the bot's own
                 exits or result
  Big coins:
    factor       factor rank (strategy/factor_rank.py): each day a seventh of $10,000 opens, half long the strongest
                 fifth and half short the weakest, equal weight, and closes 7 days later. It trades at the first 1h
                 close after the ranking lands (01:00 UTC; the ranked day's close where history has no 1h candle,
                 before March 2026), with Hyperliquid fees and slippage both ways (sim.side_cost) and funding while
                 held
    factor_long  the same slices all long the strongest fifth, next to the same slices in every ranked coin
    strategist   every idea the Strategist kept (not the vetoed ones), $100 risked on each: out at its stop, first
                 target or horizon (the stop_target outcome)
  Added 2026-10-02 after the first results, so they count only entries from START_V2:
    score_tp     calls whose dossier scored SCORE_MIN or more, sold at 2x (the two least-bad rules so far, together)
    fade_quant   the quant model's 1h calls on coins Hyperliquid lists (it also calls OKX-only coins, whose costs we
                 don't model), taken the other way, held for the grader's 6-bar window,
                 $100 a trade after a round trip of Hyperliquid fees and slippage; set against the same side on an
                 average listed coin
                 the model called in the same hour, so the market's drift isn't counted as skill, whichever side the
                 trade is on. Entries use the grader's window, from the
                 close of the call's candle; the call lands about 8 minutes after that close, as for every graded
                 source. Funding is left out (6 hours).
  A dossier rule sees the dossier only as it stood when the call was logged (the last de_dossier_history row at or
  before it), and only a scored one (at least 5 lines known): a call on a coin we hadn't composed yet is outside those
  books. Calls the backtester couldn't price are left out of every book and counted.

A verdict needs a sample first: MIN_TRADES counted trades over MIN_DAYS days (memecoins), MIN_IDEAS closed ideas over
MIN_DAYS days (Strategist), MIN_WEEKS weeks (factor rank: the learning loop's first checkpoint). Intervals are 95%
bootstraps over days (a day's memecoins rise and fall together), and over coins in the dossier report. Each Deep End
book is set against what it should beat, on the same days: the dossier rules against every call that had a scored
dossier, selling at 2x against holding the same calls 24 h, FOMO and the bot against every call since they began.

The dossier report asks whether the dossier picks winners, on graded Telegram calls with a scored dossier at the call.
Set in advance: (1) calls without a red flag against calls with one, (2) the rank correlation of the score with the
24 h return. Score bands, single flags and the per-line table are exploratory: with this many comparisons, some will
differ by chance.
"""

import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
import orjson

from albatross import db
from albatross.backtest import sim

log = logging.getLogger(__name__)

JOB = "paper_books"
STAKE_USD = sim.DEX_REF_NOTIONAL_USD     # a memecoin trade: the size the DEX cost model prices
RISK_USD = 100.0                         # a Strategist idea: what its stop loses
FACTOR_USD = 10_000.0
HOLD_DAYS = 7
FACTOR_VENUE = "hyperliquid"             # strategy/factor_rank.py ranks Hyperliquid's daily candles
SCORE_MIN = 85                           # calls at or above it were the top quarter by score when this was set
SCORE_BANDS = ((70, "under 70"), (80, "70 to 79"), (85, "80 to 84"), (None, "85 or more"))
MIN_TRADES, MIN_DAYS = 100, 14
MIN_IDEAS = 60
MIN_WEEKS = 6
MIN_CALLS, MIN_SIDE, MIN_GROUP = 500, 100, 30
BOOT = 1000
DAY, HOUR = timedelta(days=1), timedelta(hours=1)


@dataclass(frozen=True)
class Book:
    key: str
    lane: str                            # deep | majors
    name: str
    rule: str
    baseline: str | None = None
    start: datetime | None = None        # entries before this don't count: a book set after seeing results
    hold: str | None = "24 hours"        # how long a trade is held before it counts (None: stop, target or horizon)


START_V2 = datetime(2026, 10, 3, tzinfo=UTC)


BOOKS = [
    Book("calls", "deep", "Every Telegram call",
         "Buy $100 of every coin called in our Telegram groups and sell it 24 hours later."),
    Book("clean", "deep", "Nothing flagged",       # "No red flag" until 2026-10-06: a check that never ran counts here
         "Only the calls whose dossier showed no failed red-flag check when the call came in. A check that hadn't run "
         "counts as not failed, so some of these calls were never fully checked.", "calls"),
    Book("score", "deep", f"Score {SCORE_MIN} or more",
         f"Only the calls whose dossier scored {SCORE_MIN} or more when the call came in (the top quarter).", "calls"),
    Book("take_profit", "deep", "Sold at 2x",
         "Every call, sold as soon as a sale nets twice the stake after costs, else at 24 hours.", "calls"),
    Book("score_tp", "deep", f"Score {SCORE_MIN}+, sold at 2x",
         f"The calls whose dossier scored {SCORE_MIN} or more, sold as soon as a sale nets twice the stake, else at 24 "
         "hours. Picked on Oct 2 after the first results, so it counts only from Oct 3.", "calls", START_V2),
    Book("fomo", "deep", "FOMO ranked traders",
         "Buy $100 of every coin a FOMO ranked trader (ranks 1 to 150) buys and sell it 24 hours later.", "calls"),
    Book("bot", "deep", "The bot (reference)",
         "The bot's real-money entries, priced and held 24 hours like the rest: not the bot's own exits or result.",
         "calls"),
    Book("factor", "majors", "Factor rank, long and short",
         "Each day a seventh of $10,000 opens, half long the strongest fifth and half short the weakest, held a week."),
    Book("factor_long", "majors", "Factor rank, long only",
         "The same slices all long the strongest fifth, next to the same slices spread over every ranked coin."),
    Book("strategist", "majors", "Strategist's ideas",
         "Every idea the Strategist kept, $100 risked on each, out at its stop, first target or horizon.", hold=None),
    Book("fade_quant", "majors", "The opposite of the quant model (1h)",
         "Each 1h call of the quant model on a coin Hyperliquid lists, taken the other way and held 6 hours, $100 a "
         "trade after its fees and slippage. "
         "It enters at the close of the call's candle, about 8 minutes before the call itself exists, as every graded "
         "source does. Set on Oct 2 because its calls were wrong more often than chance; it counts only from Oct 3.",
         None, START_V2, hold="6 hours"),
]
BY_KEY = {b.key: b for b in BOOKS}


# ─── intervals ───────────────────────────────────────────────────────────────
def _sums(values: list[float], clusters: list, ids: list) -> tuple[np.ndarray, np.ndarray]:
    pos = {c: i for i, c in enumerate(ids)}
    s, n = np.zeros(len(ids)), np.zeros(len(ids))
    for v, c in zip(values, clusters):
        s[pos[c]] += v
        n[pos[c]] += 1
    return s, n


def _band(est: float, draws: list[float], n: int, k: int) -> dict:
    lo, hi = np.percentile(draws, [2.5, 97.5]) if draws else (math.nan, math.nan)
    return {"value": round(float(est), 4), "lo": round(float(lo), 4), "hi": round(float(hi), 4), "n": n, "clusters": k}


def mean_interval(values: list[float], clusters: list, seed: int = 7) -> dict | None:
    """The mean of values with a 95% interval, resampling whole clusters (days or coins) with replacement."""
    if not values:
        return None
    ids = sorted(set(clusters))
    s, n = _sums(values, clusters, ids)
    rng, k = np.random.default_rng(seed), len(ids)
    draws = []
    for _ in range(BOOT):
        i = rng.integers(0, k, k)
        draws.append(s[i].sum() / n[i].sum())
    return _band(s.sum() / n.sum(), draws, len(values), k)


def diff_interval(a: list[float], a_cl: list, b: list[float], b_cl: list, seed: int = 7) -> dict | None:
    """mean(a) - mean(b) with a 95% interval; a resampled cluster brings its values from both sides."""
    if not a or not b:
        return None
    ids = sorted(set(a_cl) | set(b_cl))
    sa, na = _sums(a, a_cl, ids)
    sb, nb = _sums(b, b_cl, ids)
    rng, k = np.random.default_rng(seed), len(ids)
    draws = []
    for _ in range(BOOT):
        i = rng.integers(0, k, k)
        if na[i].sum() and nb[i].sum():
            draws.append(sa[i].sum() / na[i].sum() - sb[i].sum() / nb[i].sum())
    return _band(sa.sum() / na.sum() - sb.sum() / nb.sum(), draws, len(a) + len(b), k)


def _ranks(a) -> np.ndarray:
    """Average ranks (ties share theirs)."""
    a = np.asarray(a, float)
    r = np.empty(len(a))
    r[a.argsort(kind="mergesort")] = np.arange(len(a))
    _, inv, counts = np.unique(a, return_inverse=True, return_counts=True)
    return (np.bincount(inv, weights=r) / counts)[inv]


def rank_corr(x: list[float], y: list[float], clusters: list, seed: int = 7) -> dict | None:
    """Spearman's rank correlation of x with y, with a 95% interval resampling whole clusters (each resample ranked
    afresh)."""
    if len(x) < 3:
        return None
    xa, ya = np.asarray(x, float), np.asarray(y, float)
    ids = sorted(set(clusters))
    pos = {c: i for i, c in enumerate(ids)}
    rows: list[list[int]] = [[] for _ in ids]
    for i, c in enumerate(clusters):
        rows[pos[c]].append(i)
    members = [np.array(r) for r in rows]

    def spearman(idx: np.ndarray) -> float:
        rx, ry = _ranks(xa[idx]), _ranks(ya[idx])
        return float(np.corrcoef(rx, ry)[0, 1]) if rx.std() > 0 and ry.std() > 0 else math.nan

    rng, k = np.random.default_rng(seed), len(ids)
    draws = [v for _ in range(BOOT)
             if math.isfinite(v := spearman(np.concatenate([members[j] for j in rng.integers(0, k, k)])))]
    est = spearman(np.arange(len(xa)))
    return _band(est, draws, len(x), k) if math.isfinite(est) else None


def cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def clear_above(i: dict | None) -> bool:
    return bool(i) and i["lo"] > 0


def clear_below(i: dict | None) -> bool:
    return bool(i) and i["hi"] < 0


# ─── the Deep End books ──────────────────────────────────────────────────────
DEX_SQL = """
SELECT c.id, c.logged_at, c.horizon_until, c.source_kind, c.scope,
       o.status, o.net_return_pct, o.exit_at, o.exit_reason,
       t.status AS tp_status, t.net_return_pct AS tp_net, t.exit_at AS tp_exit_at, t.exit_reason AS tp_reason,
       h.at AS dossier_at, h.score, h.red_flags, h.statuses, s.symbol
FROM trade_calls c
LEFT JOIN trade_call_outcomes o ON o.call_id = c.id AND o.policy = 'horizon'
LEFT JOIN trade_call_outcomes t ON t.call_id = c.id AND t.policy = 'take_profit'
LEFT JOIN LATERAL (
  SELECT d.at, d.score, d.red_flags,
         (SELECT jsonb_object_agg(x->>'key', x->>'status') FROM jsonb_array_elements(d.checklist) x) AS statuses
  FROM de_dossier_history d
  WHERE d.chain = split_part(c.scope, ':', 1) AND d.address = split_part(c.scope, ':', 2) AND d.at <= c.logged_at
  ORDER BY d.at DESC LIMIT 1) h ON true
LEFT JOIN micro_state s ON s.chain = split_part(c.scope, ':', 1) AND s.token = split_part(c.scope, ':', 2)
WHERE c.market = 'dex' AND c.source_kind IN ('caller', 'fomo', 'bot')
ORDER BY c.logged_at, c.id"""

PICK = {
    "calls": lambda c: c["source_kind"] == "caller",
    "clean": lambda c: c["source_kind"] == "caller" and c["score"] is not None and not c["red_flags"],
    "score": lambda c: c["source_kind"] == "caller" and c["score"] is not None and c["score"] >= SCORE_MIN,
    "take_profit": lambda c: c["source_kind"] == "caller",
    "fomo": lambda c: c["source_kind"] == "fomo",
    "bot": lambda c: c["source_kind"] == "bot",
    "scored": lambda c: c["source_kind"] == "caller" and c["score"] is not None,     # what the dossier rules pick from
    "score_tp": lambda c: (c["source_kind"] == "caller" and c["score"] is not None and c["score"] >= SCORE_MIN
                           and c["logged_at"] >= START_V2),
}


def outcome(c: dict, policy: str) -> tuple[str, float | None, datetime | None]:
    """-> (closed | stalled | open | unpriced, net %, exit time). stalled: the candles stopped before the 24 h (the coin
    stopped trading, or our price source failed), so the grader gave up (ideas.GIVE_UP) and kept its last mark, which
    counts as the sale. An open trade's net is its latest mark (None before any candle)."""
    status, net, at, reason = ((c["tp_status"], c["tp_net"], c["tp_exit_at"], c["tp_reason"]) if policy == "take_profit"
                               else (c["status"], c["net_return_pct"], c["exit_at"], c["exit_reason"]))
    if status == "graded":
        return "closed", net, at
    if status == "ungradeable":
        return ("stalled", net, at) if reason == "no_candles" and net is not None else ("unpriced", None, None)
    return "open", net, None                      # open, or logged too recently to have an outcome row


def done(c: dict) -> bool:
    """The call's 24 h are resolved: graded, or given up at its last mark."""
    return outcome(c, "horizon")[0] in ("closed", "stalled")


def take(calls: list[dict], pick, policy: str) -> tuple[list[dict], Counter]:
    """The book's trades, in call order: one position per coin (a call on a coin still held is skipped; an open one is
    held until its horizon at the latest), unpriced calls left out."""
    trades, skipped, until = [], Counter(), {}
    for c in calls:
        if not pick(c):
            continue
        if until.get(c["scope"], c["logged_at"]) > c["logged_at"]:
            skipped["holding"] += 1
            continue
        state, net, exit_at = outcome(c, policy)
        if state == "unpriced":
            skipped["unpriced"] += 1
            continue
        until[c["scope"]] = exit_at or c["horizon_until"]
        tp_state, tp_net, _ = outcome(c, "take_profit")
        trades.append({"call_id": c["id"], "coin": c["scope"], "symbol": c.get("symbol"), "at": c["logged_at"],
                       "exit_at": exit_at, "state": "closed" if state == "stalled" else state, "value": net,
                       "stalled": state == "stalled",
                       "settled": state in ("closed", "stalled") and done(c),          # the call's 24 h are resolved
                       "tp": tp_net if tp_state in ("closed", "stalled") else None})
    return trades, skipped


def _day(t: datetime) -> date:
    return t.astimezone(UTC).date()


def curve(trades: list[dict], dollars, step: timedelta) -> list[list]:
    """Realized P&L to date, by exit time in `step` buckets: [[bucket start, cumulative dollars]], from the first entry."""
    closed = sorted((t for t in trades if t["settled"] and t["value"] is not None), key=lambda t: t["exit_at"])
    if not trades:
        return []
    start = min(t["at"] for t in trades)
    epoch = datetime(2000, 1, 1, tzinfo=UTC)

    def bucket(t: datetime) -> datetime:
        return epoch + ((t - epoch) // step) * step
    out, cum = [[bucket(start), 0.0]], 0.0
    for t in closed:
        cum += dollars(t["value"])
        b = bucket(t["exit_at"])
        if out[-1][0] == b:
            out[-1][1] = round(cum, 2)
        else:
            out.append([b, round(cum, 2)])
    return out


def drawdown(trades: list[dict], dollars) -> float:
    """The worst fall of realized P&L from its running peak, in dollars (0 or negative)."""
    peak = cum = worst = 0.0
    for t in sorted((t for t in trades if t["settled"] and t["value"] is not None), key=lambda t: t["exit_at"]):
        cum += dollars(t["value"])
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return round(worst, 2)


def in_use(trades: list[dict], stake: float, now: datetime) -> float:
    """The most money (or risk) out at once: stakes from entry to exit, open ones until now."""
    ev = sorted([(t["at"], stake) for t in trades] + [(t["exit_at"] or now, -stake) for t in trades],
                key=lambda e: (e[0], e[1]))          # an exit and an entry at the same moment: the exit first
    cur = most = 0.0
    for _, d in ev:
        cur += d
        most = max(most, cur)
    return round(most, 2)


def first_read(entries: list[datetime], min_n: int, min_days: int, today: date, lag: timedelta = DAY) -> date | None:
    """When min_n graded trades over min_days days of entries should be in: the min_n-th entry (or, short of it, the
    pace of entries so far) and the min_days-th day of entries, each plus `lag` to be graded. None: no entries yet."""
    if not entries:
        return None
    entries = sorted(entries)
    days = sorted({_day(t) for t in entries})
    if len(entries) >= min_n:
        by_n = _day(entries[min_n - 1] + lag)
    else:
        elapsed = max((today - days[0]).days, 1)
        by_n = today + timedelta(days=math.ceil((min_n - len(entries)) * elapsed / len(entries))) + lag
    by_days = (days[min_days - 1] if len(days) >= min_days else days[0] + timedelta(days=min_days - 1)) + lag
    return max(by_n, by_days, today)


def verdict(per_trade: dict | None, n: int, days: int, when: date | None, min_n: int, min_days: int, unit: str) -> dict:
    if per_trade is None or n < min_n or days < min_days:
        short = [f"{n:,} of the {min_n} {'closed ideas' if unit == 'R' else 'counted trades'} needed" if n < min_n else None,
                 f"{days} of the {min_days} days needed" if days < min_days else None]
        return {"status": "too_early", "first_read": when,               # the page shows "Too early" beside the words
                "words": cap(f"{' and '.join(x for x in short if x)}.") + (f" First read around {when:%b %-d}." if when else "")}
    v, lo, hi = per_trade["value"], per_trade["lo"], per_trade["hi"]
    fmt = (lambda x: f"{x:+.2f}R") if unit == "R" else (lambda x: f"{x:+.1f}%")
    if lo > 0:
        return {"status": "making", "words": f"Making money: {fmt(v)} a trade (95% interval {fmt(lo)} to {fmt(hi)})."}
    if hi < 0:
        return {"status": "losing", "words": f"Losing money: {fmt(v)} a trade (95% interval {fmt(lo)} to {fmt(hi)})."}
    return {"status": "unclear", "words": f"Can't tell yet: {fmt(v)} a trade, but the 95% interval runs {fmt(lo)} to {fmt(hi)}."}


def summarize(book: Book, trades: list[dict], skipped: Counter, now: datetime, *, unit: str, stake: float,
              step: timedelta, min_n: int) -> dict:
    """The page's numbers for one trade-by-trade book. unit: '%' (value = net % of the stake) or 'R' (value = R)."""
    dollars = (lambda v: stake * v / 100) if unit == "%" else (lambda v: stake * v)
    closed = [t for t in trades if t["settled"] and t["value"] is not None]          # counted
    live = [t for t in trades if not t["settled"]]                                    # open, or sold but not counted yet
    marked = [t for t in live if t["value"] is not None]
    days = sorted({_day(t["at"]) for t in closed})
    per_trade = mean_interval([t["value"] for t in closed], [_day(t["at"]) for t in closed])
    first = _day(min(t["at"] for t in trades)) if trades else None
    when = first_read([t["at"] for t in trades], min_n, MIN_DAYS, now.date())
    pnl = sum(dollars(t["value"]) for t in closed)
    ranked = sorted(closed, key=lambda t: t["value"])

    def brief(t: dict) -> dict:
        return {"coin": t["coin"], "symbol": t["symbol"], "at": t["at"], "value": round(t["value"], 2)}
    return {
        "key": book.key, "lane": book.lane, "name": book.name, "rule": book.rule, "baseline": book.baseline,
        "hold": book.hold, "starts": book.start if book.start and now < book.start and not trades else None,
        "unit": unit, "stake_usd": stake, "since": first, "taken": len(trades), "closed": len(closed), "open": len(live),
        "skipped": dict(skipped), "stalled": sum(1 for t in closed if t.get("stalled")),
        "pnl_usd": round(pnl, 2), "staked_usd": round(stake * len(closed), 2),
        "open_pnl_usd": round(sum(dollars(t["value"]) for t in marked), 2), "open_marked": len(marked),
        "now_pnl_usd": round(pnl + sum(dollars(t["value"]) for t in marked), 2),
        "win_rate": round(sum(t["value"] > 0 for t in closed) / len(closed), 4) if closed else None,
        "median": round(float(np.median([t["value"] for t in closed])), 4) if closed else None,
        "per_trade": per_trade, "max_in_use_usd": in_use(trades, stake, now), "max_drawdown_usd": drawdown(trades, dollars),
        "curve": curve(trades, dollars, step), "best": [brief(t) for t in ranked[::-1][:3]],
        "worst": [brief(t) for t in ranked[:3]],
        "verdict": (verdict(per_trade, len(closed), len(days), when, min_n, MIN_DAYS, unit) if trades or not book.start
                    or now >= book.start else {"status": "too_early", "first_read": None,
                                               "words": f"Counts from {book.start:%b %-d}, {book.start:%H:%M} UTC."}),
    }


def counted(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t["settled"] and t["value"] is not None]


# The ledger feed's kind for each book's calls (micro_ledger): what never got a price, so is in no book.
LEDGER_KIND = {"calls": "call", "clean": "call", "score": "call", "take_profit": "call", "score_tp": "call",
               "fomo": "fomo", "bot": "trade"}
SELLS_AT_2X = {"take_profit", "score_tp"}
UNPRICED_SQL = "SELECT kind, at FROM micro_ledger WHERE status = 'unpriced' AND at > now() - interval '120 days'"


def dex_books(calls: list[dict], now: datetime, unpriced: dict[str, list[datetime]] | None = None) -> list[dict]:
    """The Deep End books, each set against what it should beat over the same period (vs_baseline, with what it is
    against): a dossier rule against every call that had a scored dossier; selling at 2x against holding the same
    calls 24 h (paired: each call's two results); FOMO and the bot against every call since their first entry.
    never_priced: calls of the book's kind since its first entry that got no price within 10 minutes (micro_ledger),
    so no book has them."""
    out, done = [], {}
    for b in (b for b in BOOKS if b.lane == "deep"):
        trades, skipped = take(calls, PICK[b.key], "take_profit" if b.key in SELLS_AT_2X else "horizon")
        if b.key in ("clean", "score", "score_tp"):   # the calls a dossier rule never saw: no scored dossier at the call
            skipped["no_dossier"] = sum(1 for c in calls if c["source_kind"] == "caller" and c["score"] is None
                                        and (b.start is None or c["logged_at"] >= b.start))
        s = summarize(b, trades, skipped, now, unit="%", stake=STAKE_USD, step=HOUR, min_n=MIN_TRADES)
        first = min((t["at"] for t in trades), default=None)
        s["never_priced"] = sum(1 for at in (unpriced or {}).get(LEDGER_KIND[b.key], []) if first and at >= first)
        out.append(s)
        done[b.key] = counted(trades)
    scored = counted(take(calls, PICK["scored"], "horizon")[0])
    scored_2x = counted(take(calls, PICK["scored"], "take_profit")[0])
    for s in out:
        a = done[s["key"]]
        if s["key"] in ("clean", "score"):
            vs, against = _vs(a, scored), "every call that had a scored dossier since this book began"
        elif s["key"] == "score_tp":
            vs, against = _vs(a, scored_2x), "every call that had a scored dossier, sold at 2x, since this book began"
        elif s["key"] == "take_profit":
            pairs = [t for t in done["calls"] if t["tp"] is not None]
            vs = mean_interval([t["tp"] - t["value"] for t in pairs], [_day(t["at"]) for t in pairs])
            against = "holding the same calls 24 hours"
        elif s["baseline"]:
            vs, against = _vs(a, done["calls"]), "every Telegram call since this book began"
        else:
            continue
        s["vs_baseline"] = None if vs is None else {**vs, "against": against}
    return out


def _vs(a: list[dict], base: list[dict]) -> dict | None:
    """a's mean per trade minus base's, base cut to the days since a's first entry (resampling days)."""
    if not a:
        return None
    start = min(t["at"] for t in a)
    b = [t for t in base if t["at"] >= start]
    return diff_interval([t["value"] for t in a], [_day(t["at"]) for t in a],
                         [t["value"] for t in b], [_day(t["at"]) for t in b])


# ─── the Strategist's ideas ──────────────────────────────────────────────────
STRATEGIST_SQL = """
SELECT c.id, c.logged_at, c.horizon_until, c.scope, o.status, o.r_multiple, o.exit_at
FROM trade_calls c LEFT JOIN trade_call_outcomes o ON o.call_id = c.id AND o.policy = 'stop_target'
WHERE c.source_kind = 'strategist' AND c.status = 'proposed'
ORDER BY c.logged_at, c.id"""


def strategist_trades(rows: list[dict]) -> tuple[list[dict], Counter]:
    """Every kept idea is its own trade (ideas on one coin at different timeframes have their own stops)."""
    trades, skipped = [], Counter()
    for r in rows:
        if r["status"] == "ungradeable":
            skipped["unpriced"] += 1
            continue
        closed = r["status"] == "graded" and r["r_multiple"] is not None
        trades.append({"call_id": r["id"], "coin": r["scope"], "symbol": r["scope"], "at": r["logged_at"],
                       "exit_at": r["exit_at"] if closed else None, "state": "closed" if closed else "open",
                       "value": r["r_multiple"], "settled": closed})
    return trades, skipped


# ─── the opposite of the quant model ─────────────────────────────────────────
FADE_SQL = """
SELECT s.id, s.scope, s.bias, s.as_of, s.valid_until, o.return_pct
FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
WHERE s.source = 'quant' AND s.tf = '1h' AND s.bias <> 0 AND s.scope NOT LIKE 'sector:%' AND s.as_of >= $1
ORDER BY s.as_of, s.scope"""
DV_SQL = """
SELECT symbol, (ts AT TIME ZONE 'UTC')::date AS day, close * volume AS dv FROM candles
WHERE venue = 'hyperliquid' AND tf = '1d' AND symbol = ANY($1::text[]) AND ts >= $2"""
# A call counts only on a coin Hyperliquid listed then: a Hyperliquid daily candle for the day before the call (the
# same candle its costs come from). The quant model also calls OKX-only coins, whose costs we don't model. Judged per call, so a later listing or delisting never rewrites the past.


def fade_trades(rows: list[dict], dv: dict[tuple[str, date], float], start: datetime) -> tuple[list[dict], Counter]:
    """The quant model's 1h calls on coins Hyperliquid listed then (a daily candle there for the day before, in dv),
    taken the other way: in at the close of the call's candle, out at the end of the grader's window, one position per
    coin. Net % = minus the call's
    signed return, minus a round trip of sim.side_cost on the coin's previous day's dollar volume there. vs_avg, before
    costs: the trade's move on its side minus the same side on the average listed coin the model called in that hour
    (every graded call then, held or not): what the pick added beyond the market. A short of a coin the model liked
    counts against shorting the average coin, a long of a coin it disliked against buying it."""
    def listed(r: dict) -> bool:
        return (r["scope"], _day(r["as_of"] + HOUR) - DAY) in dv
    skipped = Counter(not_listed=sum(1 for r in rows if not listed(r) and r["as_of"] + HOUR >= start))
    rows = [r for r in rows if listed(r)]
    hour_moves: dict[datetime, list[float]] = defaultdict(list)
    for r in rows:
        if r["return_pct"] is not None:
            hour_moves[r["as_of"]].append((1 if r["bias"] > 0 else -1) * r["return_pct"])
    avg = {h: sum(m) / len(m) for h, m in hour_moves.items()}
    trades, until = [], {}
    for r in rows:
        at = r["as_of"] + HOUR                   # the call's candle closes
        if at < start:
            continue
        if until.get(r["scope"], at) > at:
            skipped["holding"] += 1
            continue
        until[r["scope"]] = r["valid_until"]
        cost = 2 * sim.side_cost(dv.get((r["scope"], _day(at) - DAY))) * 100
        done = r["return_pct"] is not None
        side = -1 if r["bias"] > 0 else 1                                          # the fade's side
        move = -side * r["return_pct"] if done else None                           # the coin's own move
        trades.append({"call_id": r["id"], "coin": r["scope"], "symbol": r["scope"], "at": at,
                       "exit_at": r["valid_until"] if done else None, "state": "closed" if done else "open",
                       "value": -r["return_pct"] - cost if done else None, "settled": done,
                       "vs_avg": side * (move - avg[r["as_of"]]) if done else None})
    return trades, skipped


def fade_book(trades: list[dict], skipped: Counter, now: datetime) -> dict:
    s = summarize(BY_KEY["fade_quant"], trades, skipped, now, unit="%", stake=STAKE_USD, step=HOUR, min_n=MIN_TRADES)
    pairs = counted(trades)
    vs = mean_interval([t["vs_avg"] for t in pairs], [_day(t["at"]) for t in pairs])
    s["vs_baseline"] = None if vs is None else {**vs, "against": "the same side on an average coin the model called that hour, before costs"}
    return s


# ─── factor rank ─────────────────────────────────────────────────────────────
RANKS_SQL = "SELECT day, symbol, quintile, live FROM factor_ranks ORDER BY day, symbol"
CANDLES_SQL = """
SELECT symbol, (ts AT TIME ZONE 'UTC')::date AS day, close, close * volume AS dv FROM candles
WHERE venue = $1 AND tf = '1d' AND symbol = ANY($2::text[]) AND ts >= $3 AND ts < $4 ORDER BY ts"""
# The price a slice trades at: the close of the 00:00-01:00 1h candle, the first after the 00:30 ranking.
FILLS_SQL = """
SELECT symbol, (ts AT TIME ZONE 'UTC')::date AS day, close FROM candles
WHERE venue = $1 AND tf = '1h' AND symbol = ANY($2::text[]) AND ts >= $3 AND ts < $4
  AND extract(hour FROM ts AT TIME ZONE 'UTC') = 0"""
# Funding a long pays per day (a fraction of notional): the hourly payments, each the mean of that hour's snapshots
# (as the backtester's _funding does), summed by the UTC day they were paid in.
FUNDING_SQL = """
SELECT symbol, (paid_at AT TIME ZONE 'UTC')::date AS day, sum(rate) AS rate FROM (
  SELECT symbol, time_bucket('1 hour', ts) + interval '1 hour' AS paid_at, avg(funding_rate) AS rate
  FROM deriv_snapshots
  WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[]) AND ts >= $2 AND funding_rate IS NOT NULL
  GROUP BY 1, 2) h
GROUP BY 1, 2"""


def legs(day_ranks: dict[str, int], kind: str) -> list[tuple[str, float]]:
    """One day's slice, [(symbol, weight)] with weights summing to 1 in absolute value (negative = short)."""
    top = sorted(s for s, q in day_ranks.items() if q == 5)
    bottom = sorted(s for s, q in day_ranks.items() if q == 1)
    if kind == "long_short":
        return ([(s, 0.5 / len(top)) for s in top] + [(s, -0.5 / len(bottom)) for s in bottom]) if top and bottom else []
    if kind == "long":
        return [(s, 1 / len(top)) for s in top]
    return [(s, 1 / len(day_ranks)) for s in sorted(day_ranks)]          # "universe": every ranked coin


def factor_equity(slices: dict[date, list[tuple[str, float]]], closes: dict[date, dict[str, float]],
                  fills: dict[tuple[str, date], float], dv: dict[tuple[str, date], float],
                  funding: dict[tuple[str, date], float], capital: float, last_day: date) -> list[tuple[date, float]]:
    """Daily-staggered slices. slices[e] is traded on day e at 01:00 UTC (the ranking of day e - 1 lands at 00:30):
    [(symbol, weight)], weights summing to 1 in absolute value (negative = short). It gets min(cash, equity /
    HOLD_DAYS), each position's notional net of its entry cost (sim.side_cost on the ranked day's dollar volume). It
    fills at fills[(symbol, e)], the first 1h close after the ranking, else at the last daily close. It pays or earns
    each day's funding while held, is marked at each daily close (the last one seen when a coin has none that day),
    and sells HOLD_DAYS later at that day's 01:00 fill, paying the exit cost. -> [(day, equity at its close)]."""
    if not slices:
        return []
    e = min(slices)
    px: dict[str, float] = dict(closes.get(e - DAY, {}))          # the closes the first ranking was made from
    cash, held, out = capital, [], []

    def gross(p: dict, price: float | None) -> float:
        return max(0.0, p["notional"] * (1 + p["sign"] * ((price or p["entry"]) / p["entry"] - 1)))

    def worth() -> float:
        return sum(gross(p, px.get(p["sym"])) - p["paid"] for sl in held for p in sl["pos"])
    while e <= last_day:
        def fill(sym: str) -> float | None:
            return fills.get((sym, e)) or px.get(sym)
        # 01:00: the slices due today sell, then today's slice buys (px still holds yesterday's closes)
        for sl in [sl for sl in held if sl["until"] <= e]:
            cash += sum(gross(p, fill(p["sym"])) * (1 - sim.side_cost(dv.get((p["sym"], e - DAY)))) - p["paid"]
                        for p in sl["pos"])
        held = [sl for sl in held if sl["until"] > e]
        if (ws := slices.get(e)) and cash > 0:
            budget = min(cash, (cash + worth()) / HOLD_DAYS)
            pos = [{"sym": sym, "sign": 1 if w > 0 else -1, "entry": fill(sym), "paid": 0.0,
                    "notional": budget * abs(w) / (1 + sim.side_cost(dv.get((sym, e - DAY))))}
                   for sym, w in ws if fill(sym)]
            if pos:
                cash -= budget * sum(abs(w) for sym, w in ws if fill(sym))
                held.append({"until": e + timedelta(days=HOLD_DAYS), "pos": pos})
        # the day: funding while held (a long pays a positive rate, a short earns it), then its close
        for sl in held:
            for p in sl["pos"]:
                p["paid"] += p["sign"] * p["notional"] * funding.get((p["sym"], e), 0.0)
        px.update(closes.get(e, {}))
        out.append((e, round(cash + worth(), 2)))
        e += DAY
    return out


def weekly(curve_: list[tuple[date, float]], start_value: float) -> list[float]:
    """Returns over consecutive 7-day steps from the start (the capital before the first close)."""
    vals = [start_value] + [v for _, v in curve_]
    return [vals[i + 7] / vals[i] - 1 for i in range(0, len(vals) - 7, 7)]


def week_stats(rets: list[float]) -> dict:
    n = len(rets)
    if not n:
        return {"weeks": 0, "mean_pct": None, "t": None, "up_pct": None}
    m = sum(rets) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (n - 1)) if n > 1 else None
    return {"weeks": n, "mean_pct": round(m * 100, 3), "t": round(m / sd * math.sqrt(n), 2) if sd else None,
            "up_pct": round(100 * sum(r > 0 for r in rets) / n)}


def factor_verdict(stats: dict, first: date | None, today: date, what: str) -> dict:
    n = stats["weeks"]
    if n < MIN_WEEKS:
        when = first + timedelta(days=7 * MIN_WEEKS) if first else None
        return {"status": "too_early", "first_read": when,
                "words": f"{n} of the {MIN_WEEKS} weeks needed." + (f" First read around {when:%b %-d}." if when else "")}
    t = stats["t"] or 0.0
    if t >= 2:
        return {"status": "making", "words": f"{what} {stats['mean_pct']:+.2f}% a week over {n} weeks (t {t:.1f})."}
    if t <= -2:
        return {"status": "losing", "words": f"{what} {stats['mean_pct']:+.2f}% a week over {n} weeks (t {t:.1f})."}
    return {"status": "unclear", "words": f"Can't tell yet: {stats['mean_pct']:+.2f}% a week over {n} weeks (t {t:.1f})."}


def factor_books(ranks: list[dict], closes: dict[date, dict[str, float]], fills: dict, dv: dict, funding: dict,
                 today: date) -> list[dict]:
    """The two factor books, live (rankings made on their own day) and, apart, the same rules on the backfilled history.
    A ranking of day d trades on d + 1 (factor_equity)."""
    last = today - DAY                              # the last closed daily candle
    by_day: dict[tuple[bool, date], dict[str, int]] = defaultdict(dict)
    for r in ranks:
        by_day[(r["live"], r["day"])][r["symbol"]] = r["quintile"]
    out = []
    for key, kind in (("factor", "long_short"), ("factor_long", "long")):
        b = BY_KEY[key]
        runs = {}
        for live in (True, False):
            sl = {d + DAY: legs(q, kind) for (lv, d), q in by_day.items() if lv is live and d + DAY <= last}
            uni = {d + DAY: legs(q, "universe") for (lv, d), q in by_day.items() if lv is live and d + DAY <= last}
            # history runs until its last slice has closed (live rankings then take over the days after)
            end = last if live else min(last, max(sl, default=last) + timedelta(days=HOLD_DAYS))
            f = funding if live else {}
            runs[live] = (factor_equity(sl, closes, fills, dv, f, FACTOR_USD, end),
                          factor_equity(uni, closes, fills, dv, f, FACTOR_USD, end) if kind == "long" else [])
        (eq, bench), (hist, hist_bench) = runs[True], runs[False]
        first = eq[0][0] if eq else None
        rets = weekly(eq, FACTOR_USD)
        if kind == "long":                          # judged on what it beat the same slices in every coin by
            excess = [a - b2 for a, b2 in zip(rets, weekly(bench, FACTOR_USD))]
            stats, what = week_stats(excess), "Beating every coin by"
            hist_stats = week_stats([a - b2 for a, b2 in zip(weekly(hist, FACTOR_USD), weekly(hist_bench, FACTOR_USD))])
        else:
            stats, what = week_stats(rets), "Making"
            hist_stats = week_stats(weekly(hist, FACTOR_USD))
        out.append({
            "key": key, "lane": b.lane, "name": b.name, "rule": b.rule, "baseline": None, "unit": "equity",
            "capital_usd": FACTOR_USD, "since": first, "days": len(eq),
            "equity_usd": eq[-1][1] if eq else FACTOR_USD,
            "pnl_usd": round((eq[-1][1] if eq else FACTOR_USD) - FACTOR_USD, 2),
            "benchmark_usd": bench[-1][1] if bench else None,
            "curve": [[d, v] for d, v in eq], "benchmark_curve": [[d, v] for d, v in bench],
            "weekly": stats, "verdict": factor_verdict(stats, first, today, what),
            "history": {"from": hist[0][0] if hist else None, "to": hist[-1][0] if hist else None,
                        "return_pct": round((hist[-1][1] / FACTOR_USD - 1) * 100, 1) if hist else None,
                        "benchmark_return_pct": round((hist_bench[-1][1] / FACTOR_USD - 1) * 100, 1) if hist_bench else None,
                        **hist_stats},
        })
    return out


# ─── the dossier report ──────────────────────────────────────────────────────
RED_FLAG_LINES = ("mint", "lp", "dev", "insiders", "wallets", "top10", "tax", "liquidity", "organic", "reddit", "github")
# The lines' names as the dossier cards show them (the history keeps only keys and statuses).
LABELS_SQL = """
SELECT DISTINCT ON (x->>'key') x->>'key' AS key, x->>'label' AS label
FROM (SELECT snapshot FROM de_dossier ORDER BY updated_at DESC LIMIT 100) d, jsonb_array_elements(d.snapshot->'checklist') x
WHERE x->>'label' IS NOT NULL
ORDER BY x->>'key'"""


def _group(rows: list[dict]) -> dict:
    v = [r["net"] for r in rows]
    return {"n": len(rows), "coins": len({r["coin"] for r in rows}),
            "mean_pct": round(sum(v) / len(v), 2) if v else None,
            "median_pct": round(float(np.median(v)), 2) if v else None,
            "up": round(sum(x > 0 for x in v) / len(v), 4) if v else None,
            "sold_2x": round(sum(r["tp"] for r in rows) / len(rows), 4) if rows else None}


def _split(a: list[dict], b: list[dict]) -> dict | None:
    return diff_interval([r["net"] for r in a], [r["coin"] for r in a], [r["net"] for r in b], [r["coin"] for r in b])


def dossier_report(calls: list[dict], labels: dict[str, str], now: datetime) -> dict:
    tg = [c for c in calls if c["source_kind"] == "caller"]
    graded = [c for c in tg if c["status"] == "graded" and c["net_return_pct"] is not None]
    rows = [{"net": c["net_return_pct"], "coin": c["scope"], "at": c["logged_at"], "score": c["score"],
             "red": bool(c["red_flags"]), "statuses": c["statuses"] or {}, "tp": c["tp_reason"] == "target"}
            for c in graded if c["score"] is not None]
    clean, flagged = [r for r in rows if not r["red"]], [r for r in rows if r["red"]]
    red_diff, rho = _split(clean, flagged), rank_corr([r["score"] for r in rows], [r["net"] for r in rows],
                                                      [r["coin"] for r in rows])
    bands, lo = [], -math.inf
    for hi, label in SCORE_BANDS:
        sel = [r for r in rows if r["score"] >= lo and (hi is None or r["score"] < hi)]
        bands.append({"label": label, **_group(sel)})
        lo = hi if hi is not None else lo
    flags = []
    for key in RED_FLAG_LINES:
        bad = [r for r in rows if r["statuses"].get(key) == "bad"]
        if bad:
            rest = [r for r in rows if r["statuses"].get(key) != "bad"]
            flags.append({"key": key, "label": labels.get(key) or key, **_group(bad), "others_mean_pct": _group(rest)["mean_pct"],
                          "diff": _split(bad, rest) if len(bad) >= MIN_GROUP else None})
    lines = []
    for key in sorted({k for r in rows for k in r["statuses"]}):
        by = {st: _group([r for r in rows if r["statuses"].get(key, "unknown") == st])
              for st in ("good", "warn", "bad", "unknown")}
        lines.append({"key": key, "label": labels.get(key) or key, "by": by})
    first = _day(min(r["at"] for r in rows)) if rows else None
    enough = len(rows) >= MIN_CALLS and min(len(clean), len(flagged)) >= MIN_SIDE
    if not enough:
        # paced by every Telegram call with a scored dossier, graded or not: each is graded a day after it came in
        when = first_read([c["logged_at"] for c in tg if c["score"] is not None], MIN_CALLS, 1, now.date())
        verdict_ = {"status": "too_early", "first_read": when,
                    "words": (f"{len(rows):,} graded calls had a scored dossier when they came in; the first read needs "
                              f"{MIN_CALLS:,}, with {MIN_SIDE} or more on each side of the red-flag split."
                              + (f" First read around {when:%b %-d}." if when else ""))}
    else:
        parts = []
        if clear_above(red_diff):
            parts.append(f"calls without a red flag did {red_diff['value']:+.1f} points better a trade")
        elif clear_below(red_diff):
            parts.append(f"calls without a red flag did {red_diff['value']:+.1f} points worse a trade")
        else:
            parts.append("no clear difference between calls with and without a red flag")
        if clear_above(rho):
            parts.append(f"higher scores went with better returns (rank correlation {rho['value']:+.2f})")
        elif clear_below(rho):
            parts.append(f"higher scores went with worse returns (rank correlation {rho['value']:+.2f})")
        else:
            parts.append("no clear link between the score and the return")
        helps = clear_above(red_diff) or clear_above(rho)
        hurts = clear_below(red_diff) or clear_below(rho)
        verdict_ = {"status": "helps" if helps and not hurts else "hurts" if hurts and not helps else "unclear",
                    "words": "; ".join(parts).capitalize() + "."}
    return {
        "population": {"telegram_calls": len(tg), "graded": len(graded), "scored": len(rows),
                       "coins": len({r["coin"] for r in rows}), "since": first},
        "base": _group(rows), "red": {"none": _group(clean), "any": _group(flagged), "diff": red_diff},
        "score": {"bands": bands, "rho": rho}, "flags": flags, "lines": lines,
        "comparisons": 2 + len(bands) + len(flags) + 4 * len(lines), "verdict": verdict_, "enough": enough,
        "min": {"calls": MIN_CALLS, "side": MIN_SIDE, "group": MIN_GROUP},
    }


# ─── the job ─────────────────────────────────────────────────────────────────
# What the agents cost over the last day, as the Output page counts it: API spend, and subscription allowance used.
COST_SQL = """
SELECT round(coalesce(sum(cost_usd), 0)::numeric, 2)::float AS api_usd,
       round((coalesce(sum((output->>'subscription_equiv_cost_usd')::float), 0)
              + coalesce(sum(credits), 0) * 0.04)::numeric, 2)::float AS pool_usd
FROM agent_runs WHERE started_at > now() - interval '24 hours'"""        # credits at OpenAI's $0.04 (llm/codex.py)


async def _factor_inputs(today: date) -> tuple[list[dict], dict, dict, dict, dict]:
    pool = await db.pool()
    ranks = [dict(r) for r in await pool.fetch(RANKS_SQL)]
    if not ranks:
        return [], {}, {}, {}, {}
    syms = sorted({r["symbol"] for r in ranks})
    start = min(r["day"] for r in ranks)
    closes: dict[date, dict[str, float]] = defaultdict(dict)
    dv: dict[tuple[str, date], float] = {}
    for r in await pool.fetch(CANDLES_SQL, FACTOR_VENUE, syms, datetime.combine(start, datetime.min.time(), UTC),
                              datetime.combine(today, datetime.min.time(), UTC)):
        closes[r["day"]][r["symbol"]] = r["close"]
        dv[(r["symbol"], r["day"])] = r["dv"]
    fills = {(r["symbol"], r["day"]): r["close"] for r in await pool.fetch(
        FILLS_SQL, FACTOR_VENUE, syms, datetime.combine(start, datetime.min.time(), UTC),
        datetime.combine(today, datetime.min.time(), UTC))}
    live_from = min((r["day"] for r in ranks if r["live"]), default=today)
    funding = {(r["symbol"], r["day"]): r["rate"] for r in await pool.fetch(
        FUNDING_SQL, syms, datetime.combine(live_from, datetime.min.time(), UTC))}
    return ranks, closes, fills, dv, funding


def _jsonable(x):
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if isinstance(x, np.generic):
        return x.item()
    raise TypeError(type(x).__name__)


async def run() -> dict:
    """paper_books: every book and the dossier report, rebuilt from scratch and stored in paper_reports."""
    now = datetime.now(UTC)
    pool = await db.pool()
    calls = [dict(r) for r in await pool.fetch(DEX_SQL)]
    for c in calls:
        c["red_flags"] = list(c["red_flags"] or []) if c["dossier_at"] else None
        c["statuses"] = orjson.loads(c["statuses"]) if c["statuses"] else None
    unpriced: dict[str, list[datetime]] = defaultdict(list)
    for r in await pool.fetch(UNPRICED_SQL):
        unpriced[r["kind"]].append(r["at"])
    books = dex_books(calls, now, unpriced)
    books += factor_books(*await _factor_inputs(now.date()), now.date())
    trades, skipped = strategist_trades([dict(r) for r in await pool.fetch(STRATEGIST_SQL)])
    books.append(summarize(BY_KEY["strategist"], trades, skipped, now, unit="R", stake=RISK_USD, step=HOUR,
                           min_n=MIN_IDEAS))
    fade_rows = [dict(r) for r in await pool.fetch(FADE_SQL, START_V2 - HOUR)]
    syms = sorted({r["scope"] for r in fade_rows})
    dv = {(r["symbol"], r["day"]): r["dv"] for r in await pool.fetch(DV_SQL, syms, START_V2 - 2 * DAY)} if syms else {}
    books.append(fade_book(*fade_trades(fade_rows, dv, START_V2), now))
    order = {b.key: i for i, b in enumerate(BOOKS)}
    books.sort(key=lambda b: order[b["key"]])
    labels = {r["key"]: r["label"] for r in await pool.fetch(LABELS_SQL)}
    dossier = dossier_report(calls, labels, now)
    cost = dict(await pool.fetchrow(COST_SQL))
    dump = lambda body: orjson.dumps(body, default=_jsonable).decode()      # noqa: E731
    await db.upsert("paper_reports", ["key", "computed_at", "report"],
                    [("books", now, dump({"books": books, "cost_24h": cost,
                                          "rules": {"stake_usd": STAKE_USD, "risk_usd": RISK_USD, "factor_usd": FACTOR_USD,
                                                    "score_min": SCORE_MIN, "take_profit_pct": sim.TAKE_PROFIT_PCT}})),
                     ("dossier", now, dump(dossier))], ["key"])
    log.info("%s: %d books, %d calls, %d scored graded calls in the dossier report", JOB, len(books), len(calls),
             dossier["population"]["scored"])
    return {"rows": len(books) + 1, "calls": len(calls), "scored": dossier["population"]["scored"]}
