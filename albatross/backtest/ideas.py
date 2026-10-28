"""grade_ideas: simulates every ledger call into trade_call_outcomes, one row per exit policy (rules
in sim.py). Vetoed ideas are graded exactly like proposed ones: that is how we learn whether the
devil's-advocate critic helps. Open outcomes are recomputed each run; resolved ones are left alone
unless regrade=True (e.g. after a cost-model change). The ledger itself is only read.

Prices: 5m candles when a venue has them for the whole elapsed window, else 1h, each through the
grader's venue preference. Costs: Hyperliquid taker fee + slippage from its 24h volume at logged_at,
and Hyperliquid funding over the hours held.

DEX calls (market 'dex', from micro/ledger_feed.py) are graded on our micro_candles_1m with the DEX cost
model (sim.simulate_dex) and recomputed only when news arrives: when the candles cover another checkpoint
(5m/1h/6h/24h) or the horizon. ~600 a day stay open for 24 h, and their numbers only move then. They have a
third policy, take_profit (sim.TAKE_PROFIT_PCT), written for older calls the first time they are seen without
it; like every policy, a row stays as it was once it is no longer open. Such a row is priced from the candles as
they stand when it is written, so a candle revised since the horizon row froze can make the two differ slightly
(1 of 439 on 2026-10-02). When a call is given up on (GIVE_UP: its candles stopped before its horizon), its rows
are 'ungradeable' but keep the last mark and its time, and a take_profit that already sold stands. Open calls are
re-marked at least every MARK_EVERY for the paper books' "if sold now".
"""

import logging
from datetime import UTC, datetime, timedelta

import orjson
import pandas as pd

from albatross import db
from albatross.backtest import sim
from albatross.features.engine import TFS, VENUE_PREFERENCE
from albatross.micro import rules as micro_rules

log = logging.getLogger(__name__)

# Candle syncs store the forming candle and re-fetch it on their next pass, and the all-tier 1h sync
# takes minutes, so a candle is trusted only this long after it closes.
SETTLE = timedelta(minutes=10)
# Like the grader: price data still missing this long after the horizon -> ungradeable.
GIVE_UP = timedelta(days=2)
# An open DEX call is re-marked at least this often, not only at its checkpoints: the paper books show what the open
# positions would fetch if sold now (backtest/books.py).
MARK_EVERY = timedelta(hours=1)
COLS = ["call_id", "policy", "status", "exit_reason", "exit_price", "net_return_pct", "r_multiple",
        "mfe_pct", "mae_pct", "graded_at", "exit_at", "cost_pct", "funding_pct", "risk_pct",
        "long_net_pct", "short_net_pct", "candles", *sim.CHECKPOINTS, "size_usd", "fee_pct", "gas_usd",
        "liquidity_usd", "quote_reserve_usd", "exits"]
FOREVER = datetime.max.replace(tzinfo=UTC)


async def _candles(symbol: str, tf: str, start, end) -> tuple[str, pd.DataFrame] | None:
    pool = await db.pool()
    for venue in VENUE_PREFERENCE:
        rows = await pool.fetch(
            """SELECT ts, open, high, low, close FROM candles
               WHERE venue = $1 AND symbol = $2 AND tf = $3 AND ts >= $4 AND ts <= $5 ORDER BY ts""",
            venue, symbol, tf, start, end,
        )
        if rows:
            return venue, pd.DataFrame([dict(r) for r in rows]).set_index("ts")
    return None


async def _funding(symbol: str, start, end) -> pd.Series | None:
    """Hourly Hyperliquid funding by payment time: the mean of the minute snapshots in each hour."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT time_bucket('1 hour', ts) + interval '1 hour' AS paid_at, avg(funding_rate) AS rate
           FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND symbol = $1 AND ts >= $2 AND ts < $3 AND funding_rate IS NOT NULL
           GROUP BY 1 ORDER BY 1""",
        symbol, start - sim.HOUR, end,
    )
    return pd.Series({r["paid_at"]: r["rate"] for r in rows}, dtype=float) if rows else None


async def _day_volume(symbol: str, at: datetime) -> float | None:
    """Hyperliquid 24h volume as of the call (the venue the costs assume); else CoinGecko's aggregate
    volume, which is larger than any single venue's, so that fallback understates slippage."""
    pool = await db.pool()
    v = await pool.fetchval(
        """SELECT day_volume_usd FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND symbol = $1 AND ts <= $2 AND ts > $2 - interval '1 hour'
           ORDER BY ts DESC LIMIT 1""", symbol, at)
    return v or await pool.fetchval("SELECT volume_24h_usd FROM assets WHERE symbol = $1", symbol)


async def run(regrade: bool = False) -> int:
    pool = await db.pool()
    calls = [dict(r) for r in await pool.fetch(
        """SELECT c.id, c.logged_at, c.scope, c.market, c.direction, c.ref_price, c.stop, c.targets,
                  c.horizon_until
           FROM trade_calls c
           WHERE c.market <> 'dex' AND ($1 OR (SELECT count(*) FROM trade_call_outcomes o
                                               WHERE o.call_id = c.id AND o.status <> 'open') < $2)
           ORDER BY c.id""", regrade, len(sim.POLICIES))]
    final = set() if regrade else {(r["call_id"], r["policy"]) for r in await pool.fetch(
        "SELECT call_id, policy FROM trade_call_outcomes WHERE status <> 'open' AND call_id = ANY($1::bigint[])",
        [c["id"] for c in calls])}
    now = datetime.now(UTC)
    asof = now - SETTLE
    rows = []

    def add(c: dict, policy: str, o: dict, candles: str | None = None) -> None:
        if (c["id"], policy) not in final:
            o = {**o, "call_id": c["id"], "policy": policy, "graded_at": now, "candles": candles}
            rows.append(tuple(o.get(k) for k in COLS))

    groups: dict[str, list[dict]] = {}
    for c in calls:
        why = sim.ungradeable_reason(c)
        if why:
            for p in sim.POLICIES:
                add(c, p, {"status": "ungradeable", "exit_reason": why})
        else:
            groups.setdefault(c["scope"], []).append(c)

    for symbol, items in groups.items():
        start = min(c["logged_at"] for c in items)
        end = max(c["horizon_until"] for c in items)
        # A day before the first call, so the venue is picked for carrying the symbol, not for having
        # the freshest candle (a venue that stores its forming candle would win on a just-logged call).
        sources = [(f"{s[0]}:{tf}", TFS[tf][2], s[1]) for tf in ("5m", "1h")
                   if (s := await _candles(symbol, tf, start - timedelta(days=1), end))]
        funding = await _funding(symbol, start, end)
        for c in items:
            cost = sim.side_cost(await _day_volume(symbol, c["logged_at"]))
            runs = [(label, r) for label, bar, df in sources
                    if (r := sim.simulate(c, df, bar, asof, cost, funding))]
            for p in sim.POLICIES:
                # The finest source that resolves the call, else the finest that can mark it.
                resolved = [(label, r[p]) for label, r in runs if r[p]["status"] == "graded"]
                label, o = (resolved or [(label, r[p]) for label, r in runs] or [(None, None)])[0]
                if (o is None or o["status"] == "open") and c["horizon_until"] < now - GIVE_UP:
                    add(c, p, {"status": "ungradeable", "exit_reason": "no_candles"})
                elif o is None:
                    add(c, p, {"status": "open", "cost_pct": round(2 * cost * 100, 4)})
                else:
                    add(c, p, o, label)

    rows += await _dex(regrade, now)
    await db.upsert("trade_call_outcomes", COLS, rows, ["call_id", "policy"])
    log.info("grade_ideas: %d calls, %d outcomes written (%s)", len(calls), len(rows),
             ", ".join(f"{s}={sum(r[2] == s for r in rows)}" for s in ("graded", "open", "ungradeable")))
    return len(rows)


async def _dex(regrade: bool, now: datetime) -> list[tuple]:
    pool = await db.pool()
    calls = [dict(r) for r in await pool.fetch(
        f"""SELECT c.id, c.logged_at, c.scope, c.ref_price, c.horizon_until, c.evidence, o.status, o.graded_at,
                   {", ".join(f"o.{k}" for k in sim.CHECKPOINTS)},
                   array_remove(ARRAY(SELECT f.policy FROM trade_call_outcomes f
                                      WHERE f.call_id = c.id AND f.status <> 'open'), NULL) AS final,
                   EXISTS (SELECT 1 FROM trade_call_outcomes f
                           WHERE f.call_id = c.id AND f.policy = 'take_profit') AS has_tp
            FROM trade_calls c
            LEFT JOIN trade_call_outcomes o ON o.call_id = c.id AND o.policy = 'horizon'
            WHERE c.market = 'dex' AND ($1 OR o.call_id IS NULL OR o.status = 'open'
                                        OR (SELECT count(*) FROM trade_call_outcomes f
                                            WHERE f.call_id = c.id AND f.status <> 'open') < $2)""",
        regrade, len(sim.DEX_POLICIES))]
    if not calls:
        return []
    for c in calls:
        c["chain"], c["token"] = c["scope"].split(":", 1)
        c["evidence"] = orjson.loads(c["evidence"])
        c["final"] = set() if regrade else set(c["final"])          # resolved rows stay as they are
    keys = sorted({(c["chain"], c["token"]) for c in calls})
    # How far each token's candles are complete: micro_state.candles_at while the last fetch worked (Birdeye or
    # GeckoTerminal, each net of its own lag), else the last candle we hold. A token retired with its liquidity
    # measured under $500 has stopped
    # trading in any real sense: its last close carries forward (else a rug would turn ungradeable).
    covered = {}
    for r in await pool.fetch(
        """SELECT k.chain, k.token, s.candles_at, s.candles_error, s.liquidity_usd,
                  (SELECT max(ts) FROM micro_candles_1m m WHERE m.chain = k.chain AND m.token = k.token) AS last_ts,
                  EXISTS (SELECT 1 FROM micro_tokens t WHERE coalesce(t.resolved_chain, t.chain) = k.chain
                          AND t.address = k.token AND t.status = 'retired')
                  AND NOT EXISTS (SELECT 1 FROM micro_tokens t WHERE coalesce(t.resolved_chain, t.chain) = k.chain
                                  AND t.address = k.token AND t.status <> 'retired') AS retired
           FROM unnest($1::text[], $2::text[]) AS k(chain, token)
           LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.token""",
            [k[0] for k in keys], [k[1] for k in keys]):
        if r["retired"] and r["liquidity_usd"] is not None and r["liquidity_usd"] < micro_rules.DUST_USD:
            until = FOREVER
        elif r["candles_at"] and r["candles_error"] is None:
            until = r["candles_at"]
        else:
            until = r["last_ts"] + sim.MINUTE if r["last_ts"] else datetime.min.replace(tzinfo=UTC)
        covered[(r["chain"], r["token"])] = until

    def due(c: dict) -> bool:
        until = covered[(c["chain"], c["token"])]
        return (regrade or c["status"] is None or c["horizon_until"] <= until or c["horizon_until"] < now - GIVE_UP
                or c["status"] != "open" or not c["has_tp"]   # a policy is missing (take_profit, added 2026-10-02)
                or c["graded_at"] is None or c["graded_at"] < now - MARK_EVERY
                or any(c[k] is None and c["logged_at"] + dt <= until for k, dt in sim.CHECKPOINTS.items()))

    by_token: dict[tuple, list[dict]] = {}
    for c in filter(due, calls):
        by_token.setdefault((c["chain"], c["token"]), []).append(c)
    rows = []

    def add(c: dict, outs: dict[str, dict], candles: str | None = None) -> None:
        for policy, o in outs.items():
            if policy in c["final"]:
                continue
            exits = orjson.dumps(o["exits"]).decode() if o.get("exits") is not None else None
            o2 = {**o, "call_id": c["id"], "policy": policy, "graded_at": now, "candles": candles, "exits": exits}
            rows.append(tuple(o2.get(k) for k in COLS))

    def every(o: dict) -> dict[str, dict]:
        return {policy: o for policy in sim.DEX_POLICIES}

    for (chain, token), items in by_token.items():
        start, end = min(c["logged_at"] for c in items), max(c["horizon_until"] for c in items)
        bars = pd.DataFrame([dict(r) for r in await pool.fetch(
            """SELECT ts, high, low, close, source FROM micro_candles_1m
               WHERE chain = $1 AND token = $2 AND ts > $3 AND ts <= $4 AND NOT 'rejected_print' = ANY(flags)
               ORDER BY ts""", chain, token, start, end)] or None, columns=["ts", "high", "low", "close", "source"])
        bars = bars.set_index("ts")
        liq = pd.DataFrame([dict(r) for r in await pool.fetch(
            """SELECT ingest_ts, source, liquidity_usd FROM micro_snapshots
               WHERE chain = $1 AND token = $2 AND ingest_ts > $3 AND ingest_ts <= $4 AND liquidity_usd IS NOT NULL
                 AND source IN ('dexscreener', 'jupiter') ORDER BY ingest_ts""", chain, token, start, end)] or None,
            columns=["ingest_ts", "source", "liquidity_usd"])
        label = f"{bars['source'].iloc[-1]}:1m" if len(bars) else None
        for c in items:
            ev = c["evidence"]
            liq_in = ev.get("liquidity_usd")
            if not liq_in or liq_in <= 0:
                add(c, every({"status": "ungradeable", "exit_reason": "no_liquidity"}))
                continue
            src = liq[liq["source"] == ev.get("liquidity_src")]
            outs = sim.simulate_dex(
                c, bars, covered[(chain, token)], liq_in, src.set_index("ingest_ts")["liquidity_usd"],
                sim.dex_fee(ev["price"].get("dex"), ev["price"].get("labels")),
                sim.GAS_USD.get(chain, sim.GAS_DEFAULT_USD))
            if outs["horizon"]["status"] == "open" and c["horizon_until"] < now - GIVE_UP:
                # The 24 h hold can't be priced: its candles stopped first (the coin stopped trading, or our price
                # source failed). The row keeps its last mark (sold at
                # the last covered minute) and says when that was; scorecards skip 'ungradeable', the paper books
                # count it there. A take-profit that already sold inside the covered minutes stands.
                h = outs["horizon"]
                last = covered[(chain, token)] if h.get("net_return_pct") is not None else None
                gone = {**h, "status": "ungradeable", "exit_reason": "no_candles", "exit_at": last and min(last, c["horizon_until"]),
                        "exit_price": (h.get("exits") or {}).get("exit", {}).get("price") if last else None}
                add(c, {p: outs[p] if p == "take_profit" and outs[p]["status"] == "graded" else gone
                        for p in sim.DEX_POLICIES}, label)
            else:
                add(c, outs, label)
    return rows
