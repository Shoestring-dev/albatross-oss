"""micro_ledger job: every Telegram group call (micro_calls), every real-money bot entry
(micro_trades) and every buy by a trader on FOMO's 7d or 30d board (de_fomo_calls, albatross/deepend/fomo.py: one
per position, source_kind 'fomo') goes into the call ledger (docs/SPEC-LEDGER.md §2), priced by OUR micro pipeline, never by the caller, the bot
or FOMO.

A call is logged once its token has a trusted price (rules.live_price, the rule the Micro tab uses) within
WINDOW of the call; ref_price is that price at logged_at. The bot's own fill price is kept in the evidence
for cost calibration, never used as the entry. Calls that never get a trusted price are counted, not logged:
  late      the feed delivered the call more than WINDOW after it was made
  missed    this job first saw it more than WINDOW after it was made (job down, or history from before it)
  no_price  seen in time, but no trusted price within WINDOW; detail = the last check (no_token, no_pool,
            no_price, stale)
Sell calls are skipped. Ledger rows: market 'dex', direction long, no stop or targets, 24 h horizon (tf 1d).
"""

import logging
from datetime import UTC, datetime, timedelta

from albatross import db, ledger
from albatross.micro import rules

log = logging.getLogger(__name__)

WINDOW = timedelta(minutes=10)
HORIZON = timedelta(hours=24)
COLS = ["kind", "src_id", "at", "status", "reason", "detail", "call_id", "first_checked", "decided_at"]
SOURCE_KIND = {"call": "caller", "trade": "bot", "fomo": "fomo"}     # micro_ledger kind -> trade_calls source_kind

PENDING_SQL = """
WITH src AS (
  SELECT 'call' AS kind, src_id, called_at AS at, received_at, chain, address, intent, caller AS who, group_id,
         group_name, NULL::text AS strategy, NULL::float8 AS size_usd, NULL::float8 AS bot_price
  FROM micro_calls
  UNION ALL
  SELECT 'trade', src_id, entry_at, NULL, chain, address, NULL, trader, group_id, NULL, strategy, size_usd,
         entry_price
  FROM micro_trades WHERE NOT coalesce(dry, false) AND entry_at IS NOT NULL
  UNION ALL
  SELECT 'fomo', src_id, at, received_at, chain, address, 'buy', handle, NULL, 'FOMO', NULL, size_usd, NULL
  FROM de_fomo_calls)
SELECT src.*, l.first_checked, tok.first_seen, coalesce(tok.resolved_chain, tok.chain) AS trade_chain,
       s.symbol, s.created_at, s.pool, s.dex, p.labels, s.price_usd, s.price_ts, s.sources, s.flags,
       s.liquidity_usd, s.liquidity_src, s.liquidity_ts
FROM src
LEFT JOIN micro_ledger l ON l.kind = src.kind AND l.src_id = src.src_id
LEFT JOIN micro_tokens tok ON tok.chain = src.chain AND tok.address = src.address
LEFT JOIN micro_state s ON s.chain = coalesce(tok.resolved_chain, tok.chain) AND s.token = src.address
LEFT JOIN micro_pools p ON p.chain = s.chain AND p.pool = s.pool AND p.token = s.token
WHERE l.src_id IS NULL OR l.status = 'pending'
"""


def price_check(r: dict, now: datetime) -> tuple[float | None, str]:
    """Our trusted price for the call's token now, and what the check found."""
    if r["first_seen"] is None:
        return None, "no_token"
    if r["pool"] is None:
        return None, "no_pool"                  # includes a chain-less call not yet found on any chain
    if r["price_usd"] is None:
        return None, "no_price"
    price = rules.live_price(r["price_usd"], r["price_ts"], rules.born(r["created_at"], r["first_seen"]), now)
    return (price, "ok") if price is not None else (None, "stale")


def verdict(r: dict, price: float | None, now: datetime) -> tuple[str, str | None]:
    """-> (status, reason): logged | pending | unpriced | skipped."""
    if r["intent"] == "sell":
        return "skipped", "sell"
    if r["received_at"] is not None and r["received_at"] - r["at"] > WINDOW:
        return "unpriced", "late"
    if (r["first_checked"] or now) - r["at"] > WINDOW:
        return "unpriced", "missed"
    if now - r["at"] > WINDOW:
        return "unpriced", "no_price"
    return ("logged", None) if price is not None else ("pending", None)


def ledger_row(r: dict, price: float, now: datetime) -> dict:
    call = r["kind"] == "call"
    return {
        "source_kind": SOURCE_KIND[r["kind"]], "source_id": r["who"] or r["group_name"] or "unknown",
        "scope": f"{r['trade_chain']}:{r['address']}", "market": "dex", "direction": "long", "tf": "1d",
        "entry_type": "market", "ref_price": price, "stop": None, "targets": [], "horizon_until": now + HORIZON,
        "reward_risk": None, "confidence": None, "status": "proposed",
        "thesis": (f"Called in {r['group_name'] or r['group_id']}" if call
                   else f"Bought on FOMO by ranked trader @{r['who']}" if r["kind"] == "fomo"
                   else f"Bot entry ({r['strategy'] or 'unknown strategy'})"),
        # Plain JSON values only (ISO strings for times) so the hash chain re-verifies from the stored JSONB.
        "evidence": {
            "kind": r["kind"], "src_id": r["src_id"], "at": r["at"].isoformat(),
            "received_at": r["received_at"].isoformat() if r["received_at"] else None,
            "delay_s": round((now - r["at"]).total_seconds(), 1), "symbol": r["symbol"],
            # buy | mention for group calls, bot_entry for the bot, fomo_buy for FOMO; the idea scorecard splits on
            # it (a few buy calls would otherwise vanish among hundreds of mentions).
            "call_type": r["intent"] if call else "fomo_buy" if r["kind"] == "fomo" else "bot_entry",
            "group_id": r["group_id"], "group_name": r["group_name"],
            "strategy": r["strategy"], "size_usd": r["size_usd"], "bot_entry_price": r["bot_price"],
            "price": {"ts": r["price_ts"].isoformat(), "sources": list(r["sources"] or []),
                      "flags": list(r["flags"] or []), "pool": r["pool"], "dex": r["dex"],
                      "labels": list(r["labels"] or [])},
            "liquidity_usd": r["liquidity_usd"], "liquidity_src": r["liquidity_src"],
            "liquidity_ts": r["liquidity_ts"].isoformat() if r["liquidity_ts"] else None,
        },
        "critique": None, "parent_id": None, "agent_run_id": None,
    }


async def run() -> int:
    pool = await db.pool()
    rows = [dict(r) for r in await pool.fetch(PENDING_SQL)]
    if not rows:
        return 0
    now = datetime.now(UTC)
    # A crash between the ledger append and the bookkeeping below would leave logged calls 'pending':
    # find them in the ledger rather than logging them twice.
    already = {(r["kind"], int(r["src_id"])): r["id"] for r in await pool.fetch(
        """SELECT id, evidence->>'kind' AS kind, evidence->>'src_id' AS src_id FROM trade_calls
           WHERE source_kind = ANY($1::text[]) AND logged_at > now() - interval '1 day'""", list(SOURCE_KIND.values()))}
    out, to_log = [], []
    for r in rows:
        price, detail = price_check(r, now)
        status, reason = verdict(r, price, now)
        call_id = already.get((r["kind"], r["src_id"]))
        if call_id:
            status, reason = "logged", None
        elif status == "logged":
            to_log.append((len(out), ledger_row(r, price, now)))
        # The check only explains a call that was in time; a late or missed one was never priced in time.
        detail = None if reason in ("late", "missed", "sell") else detail
        out.append([r["kind"], r["src_id"], r["at"], status, reason, detail, call_id, r["first_checked"] or now,
                    None if status == "pending" else now])
    if to_log:
        for (i, _), call_id in zip(to_log, await ledger.append([row for _, row in to_log])):
            out[i][6] = call_id
    await db.upsert("micro_ledger", COLS, [tuple(o) for o in out], ["kind", "src_id"],
                    update=["status", "reason", "detail", "call_id", "decided_at"])
    counts = {s: sum(o[3] == s for o in out) for s in ("logged", "pending", "unpriced", "skipped")}
    log.info("micro_ledger: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return counts["logged"]
