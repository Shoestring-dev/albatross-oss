"""Pure trade simulation for the call ledger (docs/SPEC-LEDGER.md §4): fills, exits and costs over one
candle frame. No I/O, so every rule here is unit-tested on hand-built candles.

- Entry: the call's ref_price at logged_at, worsened by one side of costs. Only candles that open
  strictly after logged_at are walked; the candle in progress at logged_at already holds prices from
  before the call, and one opening exactly at logged_at is excluded too (no look-ahead, no ties).
- stop_target: first touch of the stop or the first target, else the horizon close. When one candle
  touches both, the stop is assumed to have come first: candles don't say which did, and the
  pessimistic guess can only understate a call. A candle opening beyond the stop fills at its open (a
  gap costs extra); one opening beyond the target fills at the target (no windfall).
- horizon: hold to the close of the last candle ending at or before horizon_until.
- Returns are signed to the call and net of taker fee + slippage on both sides and of funding.
"""

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

POLICIES = ["stop_target", "horizon"]

# Taker fee per side, base (no-volume) tier, checked 2026-09-29:
#   hyperliquid 0.045%  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees (perps, tier 0)
#   binance     0.050%  https://www.binance.com/en/support/faq/binance-futures-fee-structure-fee-calculations-360033544231
#                       (USDⓈ-M futures, Regular user)
#   bybit       0.055%  https://www.bybit.com/en/help-center/article/Trading-Fee-Structure (perps, non-VIP)
# Targets could rest as maker orders; charging taker on every fill keeps the estimate pessimistic.
TAKER_FEE = {"hyperliquid": 0.00045, "binance": 0.0005, "bybit": 0.00055}
# Calls carry no venue yet. Of the three, only Hyperliquid is open to a US person, so it is the default.
EXEC_VENUE = "hyperliquid"

# Slippage per side in bps: a half-spread floor plus square-root market impact for a reference size,
#     slip_bps = SPREAD_BPS + IMPACT_BPS * sqrt(REF_NOTIONAL_USD / day_volume_usd), capped at MAX_SLIP_BPS
# The square-root law puts impact near daily volatility * sqrt(size / daily volume); IMPACT_BPS = 400
# stands in for a ~4% daily move. At $10k: BTC on HL ($2B/day) ~1.9 bps, a $20M/day perp ~10 bps,
# $1M/day ~41 bps. Calls carry no size yet, hence the reference notional; unknown volume pays the cap.
SPREAD_BPS = 1.0
IMPACT_BPS = 400.0
REF_NOTIONAL_USD = 10_000
MAX_SLIP_BPS = 100.0

# A source missing more candles than this in the elapsed window is not trusted: a stop can hide in a hole.
COVERAGE = 0.95
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def side_cost(day_volume_usd: float | None, venue: str = EXEC_VENUE) -> float:
    """Fraction of notional lost per side: taker fee + slippage."""
    slip = MAX_SLIP_BPS
    if day_volume_usd and day_volume_usd > 0:
        slip = min(MAX_SLIP_BPS, SPREAD_BPS + IMPACT_BPS * math.sqrt(REF_NOTIONAL_USD / day_volume_usd))
    return TAKER_FEE[venue] + slip / 10_000


def net_return_pct(d: int, entry: float, exit_: float, cost: float, funding: float = 0.0) -> float:
    """% return for direction d (+1 long, -1 short) after `cost` per side and `funding` (% paid)."""
    fill_in, fill_out = entry * (1 + d * cost), exit_ * (1 - d * cost)
    return d * (fill_out / fill_in - 1) * 100 - funding


def funding_pct(rates: pd.Series | None, start: datetime, end: datetime) -> float | None:
    """Funding a LONG pays (% of notional; a short pays the negative) for each hourly payment strictly
    inside (start, end). `rates` = hourly rate by payment time. Hours without a stored rate pay the mean
    of the stored ones in the window; with none in the window it is unknown (None), never borrowed from
    other dates."""
    if rates is None:
        return None
    first = _floor(start, HOUR) + HOUR
    n = max(0, -((first - end) // HOUR))
    if n == 0:
        return 0.0
    held = rates[(rates.index > start) & (rates.index < end)]
    if held.empty:
        return None
    return float((held.sum() + (n - len(held)) * held.mean()) * 100)


def ungradeable_reason(c: dict) -> str | None:
    if c["market"] != "perp":
        return f"no_{c['market']}_cost_model"
    d, ref = _sign(c), c["ref_price"]
    if not ref or ref <= 0:
        return "bad_levels"
    if c["stop"] is not None and (c["stop"] - ref) * d >= 0:
        return "bad_levels"
    if c["targets"] and (c["targets"][0] - ref) * d <= 0:
        return "bad_levels"
    return None


def simulate(c: dict, bars: pd.DataFrame, bar: timedelta, asof: datetime, cost: float,
             funding: pd.Series | None = None) -> dict[str, dict] | None:
    """-> {policy: outcome} for one call over one candle source, or None when the source has holes.

    c: a trade_calls row (direction, ref_price, stop, targets, logged_at, horizon_until).
    bars: open/high/low/close indexed by candle open time. asof: candles closing after it are ignored
    (not final yet). cost: side_cost(). Outcome keys are trade_call_outcomes column names."""
    d, ref, stop = _sign(c), c["ref_price"], c["stop"]
    target = c["targets"][0] if c["targets"] else None
    first = _floor(c["logged_at"], bar) + bar
    end = _floor(min(c["horizon_until"], asof), bar)
    w = bars[(bars.index > c["logged_at"]) & (bars.index + bar <= end)]
    expected = max(0, (end - first) // bar)
    if len(w) < COVERAGE * expected:
        return None
    horizon_done = (c["horizon_until"] <= asof and len(w) > 0
                    and w.index[-1] + bar == _floor(c["horizon_until"], bar))
    fill_in = ref * (1 + d * cost)
    risk = abs(fill_in - stop) / fill_in * 100 if stop is not None else None
    base = {"cost_pct": round(2 * cost * 100, 4), "risk_pct": _r(risk)}

    out = {}
    for policy in POLICIES:
        if w.empty:
            out[policy] = {"status": "open", **base}
            continue
        held, px, reason = w, float(w["close"].iloc[-1]), "horizon" if horizon_done else None
        if policy == "stop_target":
            lo, hi, never = w["low"].to_numpy(), w["high"].to_numpy(), np.zeros(len(w), bool)
            adverse = (lo <= stop if d > 0 else hi >= stop) if stop is not None else never
            favour = (hi >= target if d > 0 else lo <= target) if target is not None else never
            hits = adverse | favour
            if hits.any():
                i = int(hits.argmax())
                held = w.iloc[: i + 1]
                if adverse[i]:                       # also when the same candle touched the target
                    o = float(w["open"].iloc[i])
                    px, reason = (stop if (o - stop) * d > 0 else o), "stop"
                else:
                    px, reason = target, "target"
        best = held["high"].max() if d > 0 else held["low"].min()
        worst = held["low"].min() if d > 0 else held["high"].max()
        if policy == "stop_target":
            # Beyond the stop or target the position no longer exists.
            if target is not None and (best - target) * d > 0:
                best = target
            if reason == "stop":
                worst = px
        exit_at = held.index[-1] + bar
        f_long = funding_pct(funding, c["logged_at"], exit_at)
        f = (f_long or 0.0) * d
        net = net_return_pct(d, ref, px, cost, f)
        out[policy] = {
            **base,
            "status": "graded" if reason else "open",
            "exit_reason": reason,
            "exit_price": _px(px) if reason else None,
            "exit_at": exit_at.to_pydatetime() if reason else None,
            "net_return_pct": _r(net),
            "r_multiple": _r(net / risk) if risk else None,
            "mfe_pct": _r(d * (best / fill_in - 1) * 100),
            "mae_pct": _r(d * (worst / fill_in - 1) * 100),
            "funding_pct": _r(f) if f_long is not None else None,
            "long_net_pct": _r(net_return_pct(1, ref, px, cost, f_long or 0.0)),
            "short_net_pct": _r(net_return_pct(-1, ref, px, cost, -(f_long or 0.0))),
        }
    return out


# ─── DEX calls (micro-caps) ──────────────────────────────────────────────────
# Graded on our own 1m candles (micro_candles_1m, rejected prints dropped). A minute without a candle had no
# swap, so the last close carries forward. No stop or targets: each call is sold at fixed checkpoints
# (SPEC-LEDGER §4) and at its 24 h horizon. Both legs pay the pool fee, constant-product price impact for
# DEX_REF_NOTIONAL_USD and a network allowance.
#
# Impact: a pool with USD liquidity L holds ~L/2 on the quote side (x). Swapping q in (after the fee) returns
# the other side worth q * x / (x + q) at the pre-trade price, so a buy loses q / (x + q) to impact, and so does
# a sale of value v into the exit pool. Both legs pay: the exit is priced at the observed market price, which
# never held our buy's footprint. That is the pessimistic end; had the footprint stayed in the price (nobody
# arbitrages a memecoin pool), a round trip at unchanged depth would cost only the fees.
# Exit liquidity is the reading closest before the exit (same source as
# at entry); without one, x * sqrt(exit / entry): with the same LPs, a constant-product quote reserve scales
# with the square root of the price. A bonding curve's virtual reserves are not modelled (it is deeper than
# its reported liquidity, so impact there is overstated).
DEX_REF_NOTIONAL_USD = 100.0
# Pool fee per swap by DexScreener dexId, checked 2026-09-29:
#   pumpfun   1.25%  bonding curve, 0.95% protocol + 0.30% creator
#                    (https://www.soltokencreator.io/blog/pump-fun-fees-explained, https://froglabs.io/blog/pump-fun-fees-explained)
#   pumpswap  1.25%  graduated pools: a market-cap sliding scale from 1.25% down to 0.30%, and these tokens sit at
#                    the small end (https://x.com/pumpdotfun/status/1962916237148926104, Dynamic Fees V1)
#   raydium   0.25%  AMM v4 fixed, CPMM default tier (https://docs.raydium.io/ray/protocol-fees)
#   uniswap v2 0.30%, pancakeswap v2 0.25%
# Tiered pools (Raydium CLMM, Uniswap/Pancake v3/v4, Meteora DLMM/DAMM, Orca) don't tell us their tier: 1%.
# Meteora's anti-sniper fee scheduler can start a DAMM v2 pool at 50% for its first 2 h; not modelled.
DEX_FEE = {"pumpfun": 0.0125, "pumpswap": 0.0125, "raydium": 0.0025}
DEX_FEE_V2 = {"uniswap": 0.003, "pancakeswap": 0.0025}
DEX_FEE_DEFAULT = 0.01
# Network cost per swap in USD. Allowances, not measurements: Solana = base fee + a memecoin-bot priority fee or
# Jito tip (~0.002 SOL); L2s cents; Ethereum mainnet a swap's gas at moderate prices.
GAS_USD = {"solana": 0.25, "robinhood": 0.05, "base": 0.05, "bsc": 0.10, "ethereum": 3.0}
GAS_DEFAULT_USD = 1.0
CHECKPOINTS = {"ret_5m_pct": timedelta(minutes=5), "ret_1h_pct": HOUR, "ret_6h_pct": 6 * HOUR,
               "ret_24h_pct": 24 * HOUR}
LIQ_MAX_AGE = HOUR          # an older liquidity reading at the exit gives way to the sqrt estimate
MINUTE = timedelta(minutes=1)
# Take profit, DEX calls only (added 2026-10-02 for the paper books): sell everything at the close of the first minute
# where that sale nets TAKE_PROFIT_PCT or more, all costs in; else hold to the horizon like "horizon". Closes, not
# highs: a bot checks the price and sells at market, and a wick can be gone before its swap lands.
TAKE_PROFIT_PCT = 100.0     # 2x the stake
DEX_POLICIES = [*POLICIES, "take_profit"]


def dex_fee(dex: str | None, labels: list[str] | None) -> float:
    if dex in DEX_FEE_V2 and "v2" in (labels or ()):
        return DEX_FEE_V2[dex]
    return DEX_FEE.get(dex, DEX_FEE_DEFAULT)


def dex_round_trip_pct(p_in: float, p_out: float, liq_in: float, liq_out: float, fee: float, gas_usd: float,
                       size: float = DEX_REF_NOTIONAL_USD) -> float:
    """Net % return of buying `size` USD at spot p_in and selling everything at spot p_out."""
    x_in, x_out = liq_in / 2, liq_out / 2
    q = size * (1 - fee)
    held = q * x_in / (x_in + q) * p_out / p_in            # what was bought, valued at the exit spot
    v = held * (1 - fee)
    got = v * x_out / (x_out + v) if x_out > 0 else 0.0
    return ((got - gas_usd) / (size + gas_usd) - 1) * 100


def exit_liquidity(measured: float | None, liq_in: float, p_in: float, p_out: float) -> float:
    return measured if measured is not None else liq_in * math.sqrt(max(p_out, 0.0) / p_in)


def simulate_dex(c: dict, bars: pd.DataFrame, covered_until: datetime, liq_in: float,
                 liquidity: pd.Series | None, fee: float, gas_usd: float) -> dict[str, dict]:
    """-> {policy: outcome} for a DEX call (DEX_POLICIES): stop_target and horizon both exit at the horizon (no stop or
    targets); take_profit sells at the first minute's close that nets TAKE_PROFIT_PCT, else at the horizon too.

    bars: the token's 1m candles (open time index). covered_until: every swap up to here is in `bars`, later
    minutes may still arrive. liquidity: measured USD liquidity by time, same source as liq_in.
    The outcome keeps every cost input (size, fee, gas, entry pool, and price + pool at each exit), so a
    different size can be regraded from the row alone with dex_round_trip_pct."""
    logged, ref, horizon = c["logged_at"], c["ref_price"], c["horizon_until"]
    w = bars[bars.index > logged] if len(bars) else bars

    def closed_by(t: datetime) -> pd.DataFrame:
        return w[w.index + MINUTE <= t] if len(w) else w

    def exit_at(t: datetime) -> dict:
        done = closed_by(t)
        p = float(done["close"].iloc[-1]) if len(done) else ref     # no swap since the call: unchanged
        m = None
        if liquidity is not None:
            seen = liquidity[(liquidity.index <= t) & (liquidity.index > t - LIQ_MAX_AGE)]
            m = float(seen.iloc[-1]) if len(seen) else None
        liq = exit_liquidity(m, liq_in, ref, p)
        return {"at": t.isoformat(), "price": _px(p), "liquidity_usd": _r(liq), "quote_reserve_usd": _r(liq / 2),
                "measured": m is not None, "net_pct": _r(dex_round_trip_pct(ref, p, liq_in, liq, fee, gas_usd))}

    done = horizon <= covered_until
    end = horizon if done else covered_until
    exits = {k.removeprefix("ret_").removesuffix("_pct"): exit_at(logged + dt)
             for k, dt in CHECKPOINTS.items() if logged + dt <= covered_until}
    row = {k: exits.get(k.removeprefix("ret_").removesuffix("_pct"), {}).get("net_pct") for k in CHECKPOINTS}
    # No stop: no R. No short side on a DEX. No funding on spot tokens.
    row |= {"exit_reason": None, "exit_price": None, "exit_at": None, "r_multiple": None, "risk_pct": None,
            "short_net_pct": None, "funding_pct": None, "size_usd": DEX_REF_NOTIONAL_USD, "fee_pct": _r(fee * 100),
            "gas_usd": gas_usd, "liquidity_usd": _r(liq_in), "quote_reserve_usd": _r(liq_in / 2), "exits": exits}
    def sold_at(t: datetime, x: dict) -> dict:
        """The outcome fields for everything sold at t (x = exit_at(t)): its net, and the path held until then."""
        q = DEX_REF_NOTIONAL_USD * (1 - fee)
        # The price actually paid per token, all entry costs in.
        fill_in = ref * (DEX_REF_NOTIONAL_USD + gas_usd) * (liq_in / 2 + q) / (q * liq_in / 2)
        held = closed_by(t)
        best = held["high"].max() if len(held) else ref
        worst = held["low"].min() if len(held) else ref
        return {"net_return_pct": x["net_pct"], "long_net_pct": x["net_pct"],
                "cost_pct": _r((x["price"] / ref - 1) * 100 - x["net_pct"]),
                "mfe_pct": _r((max(best, ref) / fill_in - 1) * 100), "mae_pct": _r((min(worst, ref) / fill_in - 1) * 100)}

    if end > logged:
        x = exits["exit"] = exit_at(end)
        row |= sold_at(end, x)
        if done:
            row |= {"exit_reason": "horizon", "exit_price": x["price"], "exit_at": horizon}
    out = {policy: {"status": "graded" if done else "open", **row} for policy in POLICIES}
    out["take_profit"] = dict(out["horizon"])
    held = closed_by(end) if end > logged else w.iloc[:0]           # nothing covered yet: nothing to sell into
    if len(held):
        # Below 2x the call's price no sale nets +100% (fees, impact and gas only take away), so only those minutes
        # are priced.
        for ts in held.index[held["close"].to_numpy() >= 2 * ref]:
            t = pd.Timestamp(ts).to_pydatetime() + MINUTE        # the minute closes; a bot sells then
            x = exit_at(t)
            if x["net_pct"] >= TAKE_PROFIT_PCT:
                out["take_profit"] = {**row, **sold_at(t, x), "status": "graded", "exit_reason": "target",
                                      "exit_price": x["price"], "exit_at": t, "exits": {**exits, "exit": x}}
                break
    return out


def _sign(c: dict) -> int:
    return 1 if c["direction"] == "long" else -1


def _floor(t: datetime, bar: timedelta) -> datetime:
    return EPOCH + ((t - EPOCH) // bar) * bar


def _r(x) -> float | None:
    # Plain Python floats: asyncpg rejects numpy scalars.
    return None if x is None else round(float(x), 6)


def _px(x) -> float:
    """A price to 10 significant digits: decimal places would erase a $0.000004 token (or PEPE)."""
    return float(f"{float(x):.10g}")
