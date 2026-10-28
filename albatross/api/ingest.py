"""Push ingest for the micro-cap lane: an external call feed POSTs new Telegram group calls and a trading bot's trade
states here (deploy/call-feed/ documents the payload).

Auth is a dedicated bearer token (CALL_FEED_TOKEN), not the dashboard login;
the Caddy gate lets /api/ingest/* through without the site login for that reason. The exporter only
advances its cursor on a 2xx, so a rejected or failed batch is simply re-sent, forever. Hence one odd row
must never reject a batch: display text is cut to length, and a row that is still unusable (no address to
key it by, a missing id) is skipped, logged and counted in the answer. On 2026-09-30 a token whose symbol was
~200 characters ("INUINUINU...") got every batch rejected for 90 minutes.
"""

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, ValidationError

from albatross import db
from albatross.config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ingest")

CHAINS = {"solana", "robinhood", "ethereum", "base", "bsc", "arbitrum", "evm"}
LISTENER = "telegram"           # micro_calls.source of the built-in Telegram listener's calls
FEED_ID_MAX = 2 ** 51           # a feed's own src_id stays under this (deploy/call-feed/README.md)
EPOCH_MIN = 1_420_070_400       # 2015-01-01: an earlier "Unix time" is a mistake, not a call


def row_id(source: str, src_id: int) -> int:
    """The key a call or trade is stored under. The default feed ("feed") and the Telegram listener (its ids sit in
    [2^52, 2^53)) keep their own ids; any other source's are hashed with its name into [2^51, 2^52), so two programs
    that both count from 1 never drop or overwrite each other's rows. Every key stays under 2^53: exact in JavaScript."""
    if source in ("feed", LISTENER):
        return src_id
    h = int.from_bytes(hashlib.blake2b(f"{source}\0{src_id}".encode(), digest_size=8).digest(), "big")
    return FEED_ID_MAX + h % FEED_ID_MAX


def _seconds(v: float | None) -> float | None:
    if v is None:
        return v
    if v > 1e11:
        raise ValueError("looks like milliseconds; send Unix seconds")
    if not EPOCH_MIN <= v <= time.time() + 86_400:
        raise ValueError("isn't a Unix time in seconds between 2015 and tomorrow")
    return v


Seconds = Annotated[float, AfterValidator(_seconds)]


def _chain(c: str | None, address: str = "") -> str:
    c = (c or "").lower()
    c = {"eth": "ethereum", "sol": "solana", "binance": "bsc"}.get(c, c) if c else "evm"
    # A call's chain is a word or link found anywhere in the message: an 0x address is never a Solana mint.
    return "evm" if c == "solana" and address.startswith("0x") else c


def _addr(chain: str, a: str) -> str:
    # A feed may key paper positions as "<address>#paper": the token is the plain address.
    a = a.split("#", 1)[0]
    return a.lower() if a.startswith("0x") else a


def _paper(a: str) -> bool:
    return a.partition("#")[2].lower() == "paper"


def _ts(t: float | None) -> datetime | None:
    return datetime.fromtimestamp(t, UTC) if t else None


def _clip(n: int) -> BeforeValidator:
    """Cut text (or a list) to n instead of rejecting it."""
    return BeforeValidator(lambda v: v[:n] if isinstance(v, str | list) else v)


class CallIn(BaseModel):
    src_id: int
    chain: str | None = None
    address: str = Field(min_length=20, max_length=64)
    group_id: int | None = None
    group_name: Annotated[str | None, _clip(200)] = None
    caller: Annotated[str | None, _clip(200)] = None
    intent: Annotated[str | None, _clip(16)] = None
    called_at: Seconds


class TradeIn(BaseModel):
    src_id: int
    trader: Annotated[str, _clip(32), Field(min_length=1)]
    chain: str
    address: str = Field(min_length=20, max_length=64)
    symbol: Annotated[str | None, _clip(64)] = None
    strategy: Annotated[str | None, _clip(32)] = None
    dry: bool | None = None
    group_id: int | None = None
    caller: Annotated[str | None, _clip(200)] = None
    entry_at: Seconds | None = None
    entry_price: float | None = None
    size_usd: float | None = None
    remaining: float | None = None
    peak: float | None = None
    realized_x: float | None = None
    real_pnl_usd: float | None = None
    status: Annotated[str | None, _clip(16)] = None
    closed_at: Seconds | None = None
    close_reason: Annotated[str | None, _clip(300)] = None
    txs: Annotated[list[str], _clip(50)] = Field(default_factory=list)


class Batch(BaseModel):
    """Rows arrive raw and are checked one by one in the endpoint, so a bad row is skipped, not the batch."""
    source: Annotated[str, _clip(32)] = "feed"
    calls: list[dict] = Field(default_factory=list, max_length=5000)
    trades: list[dict] = Field(default_factory=list, max_length=5000)


def _valid(model: type[BaseModel], rows: list[dict], kind: str, skipped: list[dict]) -> list:
    out = []
    for r in rows:
        try:
            row = model.model_validate(r)
        except ValidationError as e:
            err = e.errors()[0]
            skipped.append({"kind": kind, "src_id": r.get("src_id"),
                            "error": f"{'.'.join(map(str, err['loc']))}: {err['msg']}"})
            continue
        chain = _chain(row.chain, row.address.lower()) if kind == "call" else _chain(row.chain)
        if not 0 < row.src_id < FEED_ID_MAX:
            skipped.append({"kind": kind, "src_id": row.src_id, "error": "src_id: use a positive integer under 2^51"})
        elif chain not in CHAINS:
            skipped.append({"kind": kind, "src_id": row.src_id,
                            "error": f"chain: {row.chain!r} isn't one of {', '.join(sorted(CHAINS))}"})
        else:
            out.append(row)
    return out


@router.post("/calls")
async def calls(batch: Batch, authorization: str = Header(default="")) -> dict:
    token = settings.call_feed_token
    if not token:
        raise HTTPException(503, "ingest not configured")
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "bad token")

    skipped: list[dict] = []
    calls_in = _valid(CallIn, batch.calls, "call", skipped)
    trades_in = _valid(TradeIn, batch.trades, "trade", skipped)
    if skipped:
        log.warning("call feed: skipped %d unusable rows: %s", len(skipped), skipped[:5])
    # "telegram" names the built-in listener's calls (albatross/telegram): a feed that calls itself that stays apart.
    source = f"feed:{batch.source}" if batch.source == LISTENER else batch.source
    return {"ok": True, **await store(source, calls_in, trades_in), "skipped": skipped}


def _earliest(tokens: dict, ch: str, a: str, seen: datetime, via: str, symbol: str | None) -> None:
    k = (ch, a)
    if k not in tokens or seen < tokens[k][2]:
        tokens[k] = (ch, a, seen, via, symbol)


async def store(source: str, calls_in: list[CallIn], trades_in: list[TradeIn] = (),
                repeats: timedelta | None = None) -> dict:
    """Store checked calls and trades: the feed's batches above, and the built-in Telegram listener's calls in-process.
    repeats: leave out a call when its group already has a call of the same token within that long before it (the
    listener's rule, albatross/telegram/listener.py); the feed decides itself what it sends.
    -> {calls, trades, tokens, folded}, calls counting the ones stored."""
    calls, trades, sightings, tokens = [], [], [], {}
    for c in calls_in:
        ch = _chain(c.chain, c.address.lower())
        if ch not in CHAINS:
            continue
        calls.append((row_id(source, c.src_id), ch, _addr(ch, c.address), c.group_id, c.group_name, c.caller, c.intent,
                      _ts(c.called_at), source))
    for t in trades_in:
        ch = _chain(t.chain)
        if ch not in CHAINS:
            continue
        a = _addr(ch, t.address)
        trades.append((row_id(source, t.src_id), t.trader, ch, a, t.symbol, t.strategy, True if _paper(t.address) else t.dry, t.group_id, t.caller,
                       _ts(t.entry_at), t.entry_price, t.size_usd, t.remaining, t.peak, t.realized_x,
                       t.real_pnl_usd, t.status, _ts(t.closed_at), t.close_reason,
                       [x[:120] for x in t.txs], datetime.now(UTC)))
        sightings.append((ch, a, _ts(t.entry_at) or datetime.now(UTC), "trade", t.symbol))

    # Tokens: keep the earliest sighting; fill the symbol once a trade supplies it. Then fold any row keyed by a
    # chain hint into the one where the token trades (migration 013): one token, one row. All in one transaction,
    # under the fold's lock, so a token row and its calls are never seen apart, and two repeats can't both pass.
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('micro_tokens'))")
        if repeats and calls:
            calls = await _new_calls(conn, calls, repeats)
        for seen in [(c[1], c[2], c[7], "call", None) for c in calls] + sightings:
            _earliest(tokens, *seen)
        await conn.executemany(
            """INSERT INTO micro_tokens (chain, address, first_seen, first_seen_via, symbol)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (chain, address) DO UPDATE SET
                 first_seen = least(micro_tokens.first_seen, EXCLUDED.first_seen),
                 first_seen_via = CASE WHEN EXCLUDED.first_seen < micro_tokens.first_seen
                                       THEN EXCLUDED.first_seen_via ELSE micro_tokens.first_seen_via END,
                 symbol = coalesce(micro_tokens.symbol, EXCLUDED.symbol)""",
            list(tokens.values()),
        )
        await conn.executemany(
            """INSERT INTO micro_calls (src_id, chain, address, group_id, group_name, caller, intent, called_at, source)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT (src_id) DO NOTHING""", calls)
        cols = ["src_id", "trader", "chain", "address", "symbol", "strategy", "dry", "group_id", "caller", "entry_at",
                "entry_price", "size_usd", "remaining", "peak", "realized_x", "real_pnl_usd", "status", "closed_at",
                "close_reason", "txs", "updated_at"]
        await conn.executemany(
            f"""INSERT INTO micro_trades ({", ".join(cols)}) VALUES ({", ".join(f"${i + 1}" for i in range(len(cols)))})
                ON CONFLICT (src_id) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])}""", trades)
        folded = await conn.fetchval("SELECT micro_fold_tokens()")
    return {"calls": len(calls), "trades": len(trades), "tokens": len(tokens), "folded": folded}


async def _new_calls(conn, calls: list[tuple], window: timedelta) -> list[tuple]:
    """The calls whose group has no call of the same token in the window before them, stored or earlier in this batch.
    By address, not chain: a call's chain follows its token when the token's row is folded (migration 013)."""
    calls = sorted(calls, key=lambda c: c[7])
    keyed = [c for c in calls if c[3] is not None]
    held: dict[tuple, list[datetime]] = {}
    if keyed:
        for r in await conn.fetch(
                """SELECT group_id, address, called_at FROM micro_calls
                   WHERE called_at > $3::timestamptz - $4::interval AND called_at <= $5::timestamptz
                     AND (group_id, address) IN (SELECT * FROM unnest($1::bigint[], $2::text[]))""",
                [c[3] for c in keyed], [c[2] for c in keyed], keyed[0][7], window, keyed[-1][7]):
            held.setdefault((r["group_id"], r["address"]), []).append(r["called_at"])
    out = []
    for c in calls:
        k = (c[3], c[2])
        if c[3] is not None and any(timedelta(0) <= c[7] - t < window for t in held.get(k, ())):
            continue
        out.append(c)
        held.setdefault(k, []).append(c[7])
    return out
