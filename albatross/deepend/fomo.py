"""FOMO (fomo.family) for The Deep End: who among the social trading app's traders holds, buys and sells a token, read
through fomoapi.io, an unofficial relay of the app's data ("not affiliated with fomo.family"). Using it is the operator's
choice; read NOTICE.md first. The key is FOMOAPI_KEY; it never leaves this module and is never logged.

Jobs
  deep_fomo_leaderboard  hourly at :07  each board (24h, 7d, 30d, all; 250 credits each) once per UTC day, a failed one
                                        again the next hour -> de_fomo_traders (wallets, PnL, clan), de_fomo_leaderboard
  deep_fomo_feed         every minute   keeps one reader of the app's live feed (wss://api.fomoapi.io/ws/alerts, free)
                                        alive in the ingest process; -> events landed since the last run
  deep_fomo_holders      every 5 min    the top FOMO holders of the hot tokens (watch.py; 250 credits a lookup), each
                                        token at most every 30 minutes, those FOMO traders were active in first

The feed carries FOMO's own LARGE events only (about $3,000 of position value and up), from every FOMO user. Measured
2026-10-01 19:45 UTC: 128 alerts in 16 minutes (a replay of the last 100 on connect, then 28 live in 3 minutes, ~11k a
day): buy 40%, sell 36%, thesis 16%, perp 9% (Hyperliquid, not stored); Solana 66%, Robinhood 11%, BSC 7%, Base 5%,
Ethereum 2%. A live alert arrives ~3 s after its ts (FOMO's own time for the event: the execution, floored to 5 s); a
heartbeat comes every 20 s. usdValue is not a trade size: on a buy it is the position's value after it (the app's
"($3K size)"), on a sell the position's realized PnL so far; tradeUsd, the on-chain fill, came with half of the trades.
We store what the Deep End reads: buys and sells by anyone on a current board, whale trades (DEEP_FOMO_WHALE_USD), every
thesis, and any event on a token on our watch list; every message is counted by type in the run's job_runs detail. The
socket costs no credits; a free key is realtime for its first 7 days, then 15 s late.

Calls. A buy by a top trader (top 50 on the 7d or 30d board) is a call, one per position (FOMO's tradeId: adding to a
position is not a new call): the token enters micro_tokens (first_seen_via 'fomo') so the micro pipeline prices it, the
call goes into de_fomo_calls, and albatross/micro/ledger_feed.py logs it into trade_calls (source_kind 'fomo', source_id
the handle, market 'dex') under the window rules of a group call. FOMO buys never enter micro_calls: they are not group
calls and do not move a token's n_calls or n_groups. In the 16 minutes measured, none of 51 buys was a top-50 (7d)
trader's.

Holders. fomoapi lists a token's top FOMO holders (up to 100) with each one's position, PnL, dev flag, thesis and
wallets; when FOMO does not answer it serves its last good answer, dated by its age (2 of 3 lookups on 2026-10-01 were
1.8 h old, and took 11 s), and we date the snapshot by that age. The holders' wallets join de_fomo_traders, which then
covers traders who never made a board.

Credits (free key: 250,000 a month). Every REST call is metered in de_api_usage (provider 'fomoapi', the x-credits-cost
header when sent) and paced: at most DEEP_FOMO_CREDITS_PER_DAY (8,000), less when the month's remainder spread over its
remaining days is smaller, so DEEP_FOMO_CREDITS_PER_MONTH (240,000, a UTC calendar month) is never passed. The boards
take 1,000 a day up front; holder lookups get the rest spread evenly over the day (~27 lookups). A 402 (credits
exhausted) stops REST calls until the next UTC day; a 429 backs off a minute, doubling to 30. Wallet resolution
(/v2/users/{handle}, 2,500 credits) is never called: the boards and the holder lists carry each trader's wallets.
"""

import asyncio
import calendar
import hashlib
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import asyncpg
import httpx
import orjson
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from albatross import controls, db
from albatross.deepend import watch
from albatross.deepend.cfg import env_float, env_int, env_str
from albatross.http import client
from albatross.micro.rules import norm

log = logging.getLogger(__name__)
logging.getLogger("websockets").setLevel(logging.WARNING)      # its debug log prints the URL, which carries the key

BASE = "https://api.fomoapi.io"
WS_URL = "wss://api.fomoapi.io/ws/alerts"
PROVIDER = "fomoapi"
BOARD_JOB, FEED_JOB, HOLDERS_JOB = "deep_fomo_leaderboard", "deep_fomo_feed", "deep_fomo_holders"

# ─── chains ──────────────────────────────────────────────────────────────────
CHAINS = ("solana", "ethereum", "base", "bsc", "robinhood")
CHAIN_IDS = {1399811149: "solana", 1: "ethereum", 8453: "base", 56: "bsc", 4663: "robinhood"}
CHAIN_NAMES = {"solana": "solana", "sol": "solana", "ethereum": "ethereum", "eth": "ethereum", "base": "base",
               "bsc": "bsc", "bnb": "bsc", "robinhood": "robinhood", "hood": "robinhood", "rh": "robinhood"}

# ─── credits ─────────────────────────────────────────────────────────────────
CALL_CREDITS = 250                      # a board or a holder lookup
WINDOWS = ("24h", "7d", "30d", "all")
BOARD_CREDITS = CALL_CREDITS * len(WINDOWS)
COOL_MIN, COOL_MAX = 60.0, 1800.0       # seconds without REST calls after a 429

# ─── boards ──────────────────────────────────────────────────────────────────
TOP_N, TOP_WINDOWS = 50, ("7d", "30d")  # a top trader's buy is a call
CURRENT_DAYS = 3                        # an older board no longer counts as current
PNL = {"24h": "pnl_24h", "7d": "pnl_7d", "30d": "pnl_30d", "all": "pnl_all"}

# ─── the feed ────────────────────────────────────────────────────────────────
TRADES = ("buy", "sell")
WHALE_USD = 100_000.0                   # a fill, or a buy's position, this big (8% of buys on 2026-10-01)
FLUSH_S, BATCH, MAX_BUFFER = 5.0, 200, 5_000
IDLE_S = 120.0                          # the server sends a heartbeat every 20 s: silence this long is a dead socket
STABLE_S = 120.0                        # a connection that lasted this long resets the backoff
BACKOFF_MIN, BACKOFF_MAX = 2.0, 300.0
REFUSED_S = 900.0                       # the key refused (401/403)
SEEN_IDS = 5_000                        # event ids remembered, so a reconnect's replay is not counted twice

# ─── holders ─────────────────────────────────────────────────────────────────
HOLDERS_EVERY = timedelta(minutes=30)
EMPTY_EVERY = timedelta(hours=2)        # nobody held it, and no FOMO event on it since
HOLDERS_PER_RUN = 2
HOLDERS_LIMIT = 100                     # fomoapi's ceiling

# ─── the dossier ─────────────────────────────────────────────────────────────
RECENT = timedelta(days=7)
MAX_TRADERS, MAX_THESES = 15, 5


def _f(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _ts(v) -> datetime | None:
    """Unix milliseconds or seconds, or ISO text (naive = UTC) -> aware datetime."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, int | float):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
        t = datetime.fromisoformat(str(v))
    except (ValueError, OverflowError, OSError):         # garbage, or a time no calendar holds (1e20)
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


_UNSTORABLE = dict.fromkeys([0, *range(0xD800, 0xE000)])     # NUL, and lone UTF-16 surrogates


def clean(v):
    """NULs and lone surrogates stripped at any depth: Postgres stores neither a NUL in text nor \\u0000 in jsonb,
    and orjson refuses a lone surrogate (stdlib json, which httpx uses, lets "\\ud83d" through). One such message
    would otherwise fail every batch it rides in."""
    if isinstance(v, str):
        return v.translate(_UNSTORABLE)
    if isinstance(v, dict):
        return {clean(k) if isinstance(k, str) else k: clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def _s(v) -> str | None:
    """A text field as text: strings and numbers kept (NULs stripped), objects, lists and booleans dropped."""
    if v is None or isinstance(v, bool) or not isinstance(v, str | int | float):
        return None
    return clean(str(v)).strip() or None


def _json(v) -> str | None:
    return None if v is None else orjson.dumps(clean(v)).decode()


def _load(v):
    return orjson.loads(v) if isinstance(v, str | bytes) else v


def our_chain(chain, chain_id=None) -> str | None:
    """FOMO's chain (chain id, else friendly name) -> our chain key, or None for a chain we don't track (Hyperliquid
    perps, Monad, Arc...)."""
    try:
        if (c := CHAIN_IDS.get(int(chain_id))) is not None:
            return c
    except (TypeError, ValueError):
        pass
    return CHAIN_NAMES.get(str(chain or "").strip().lower())


def wallets(w) -> dict:
    """FOMO's wallets object -> our columns. Evidence: onchain-holdings (proven) or provider-claimed (a lead only)."""
    w = w if isinstance(w, dict) else {}
    ev = w.get("evidence") if isinstance(w.get("evidence"), dict) else {}
    sol, evm = _s(w.get("solana")), _s(w.get("evm"))
    return {"solana_wallet": sol, "solana_evidence": _s(ev.get("solana")) if sol else None,
            "evm_wallet": norm(evm) if evm else None, "evm_evidence": _s(ev.get("evm")) if evm else None}


# ─── REST, metered and paced ─────────────────────────────────────────────────
# What storing a row can fail with when the data itself is at fault: the server's DataError, asyncpg's client-side one
# (a ValueError subclass) and orjson's TypeError. The live feed drops such a row and counts it (bad_rows). A database
# that is down raises OSError or asyncpg's connection errors: those are retried.
DATA_ERRORS = (asyncpg.exceptions.DataError, TypeError, ValueError)
# After a paid call, also an answer of a shape we didn't expect: never re-bought, the check marked failed. (The feed
# doesn't use these: a KeyError there is our bug, and it must fail the job loudly, not drop rows quietly.)
UNSTORABLE = (*DATA_ERRORS, AttributeError, KeyError, IndexError)


class Exhausted(RuntimeError):
    """fomoapi answered 402 (credits_exhausted): no REST call until the next UTC day."""


class BudgetExceeded(RuntimeError):
    """Our own pace for the day, or the month's ceiling."""


_lock = asyncio.Lock()
_api: dict = {"stopped": None, "cool_until": 0.0, "cool_s": COOL_MIN, "remaining": None}


def cooling() -> bool:
    """After a 429, no REST call goes out until the cool-off ends."""
    return time.monotonic() < _api["cool_until"]


def daily_cap() -> int:
    return env_int("DEEP_FOMO_CREDITS_PER_DAY", 8_000)


def monthly_cap() -> int:
    return env_int("DEEP_FOMO_CREDITS_PER_MONTH", 240_000)


def days_left(d: date) -> int:
    """Days left in d's month, d included."""
    return calendar.monthrange(d.year, d.month)[1] - d.day + 1


def daily_budget(cap: float, monthly: float, used_before_today: float, today: date) -> float:
    """The day's credits: the daily cap, or the month's remainder spread over its remaining days when that is less."""
    return max(0.0, min(cap, (monthly - used_before_today) / days_left(today)))


def pace(daily: float, now: datetime) -> float:
    """Credits the day may have spent by `now`: the boards' share from midnight, the rest spread evenly over the UTC
    day, plus an hour's share of it."""
    board = min(daily, BOARD_CREDITS)
    rest = daily - board
    frac = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)) / timedelta(days=1)
    return min(daily, board + rest * frac + rest / 24)


def room(kind: str, cost: int, used: dict, daily: float, board_due: int, monthly: float, now: datetime) -> bool:
    """May a call of this kind ('board' or 'holders') spend `cost` now? Never past the month's ceiling; a board within
    the day's budget; anything else within the day's pace, leaving what today's boards still need."""
    if used["month"] + cost > monthly:
        return False
    if kind == "board":
        return used["today"] + cost <= daily
    return used["today"] + cost + board_due <= pace(daily, now)


USAGE_SQL = """
SELECT coalesce(sum(units) FILTER (WHERE day = $1), 0) AS today,
       coalesce(sum(units), 0) AS month,
       coalesce(sum(units) FILTER (WHERE day < $1), 0) AS before
FROM de_api_usage WHERE provider = 'fomoapi' AND day >= $2"""


async def usage(today: date) -> dict:
    pool = await db.pool()
    return {k: int(v) for k, v in dict(await pool.fetchrow(USAGE_SQL, today, today.replace(day=1))).items()}


async def board_due(today: date) -> int:
    """Credits today's boards still need."""
    pool = await db.pool()
    try:
        done = await pool.fetchval('SELECT count(DISTINCT "window") FROM de_fomo_leaderboard WHERE day = $1', today)
    except asyncpg.UndefinedTableError:
        done = 0
    return CALL_CREDITS * max(0, len(WINDOWS) - int(done or 0))


async def _record(units: int) -> None:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_api_usage (provider, day, calls, units)
           VALUES ('fomoapi', (now() AT TIME ZONE 'UTC')::date, 1, $1)
           ON CONFLICT (provider, day) DO UPDATE SET calls = de_api_usage.calls + 1, units = de_api_usage.units + $1""",
        units)


async def get(path: str, params: dict | None = None, *, kind: str) -> dict | list | None:
    """GET a fomoapi data endpoint -> the JSON body, or None (no key, cooling off after a 429, not found, or an error,
    logged). Raises Exhausted after a 402, until the next UTC day, and BudgetExceeded past our pace or ceiling."""
    key = env_str("FOMOAPI_KEY")
    if not key:
        return None
    now = datetime.now(UTC)
    if _api["stopped"] == now.date():
        raise Exhausted("fomoapi: out of credits (402) today")
    if cooling():
        return None
    async with _lock:                                # one call at a time: the budget check holds for the call
        used = await usage(now.date())
        daily = daily_budget(daily_cap() * await controls.scale(PROVIDER), monthly_cap(), used["before"], now.date())
        due = await board_due(now.date()) if kind != "board" else 0
        if not room(kind, CALL_CREDITS, used, daily, due, monthly_cap(), now):
            raise BudgetExceeded(f"fomoapi: {used['today']:,} credits used today of {int(daily):,} "
                                 f"({used['month']:,} this month of {monthly_cap():,})")
        try:
            r = await client().get(BASE + path, params=params or {}, timeout=30,
                                   headers={"authorization": f"Bearer {key}", "accept": "application/json"})
        except httpx.HTTPError as e:
            log.warning("fomoapi %s: %s", path, type(e).__name__)
            return None
        billed = _i(r.headers.get("x-credits-cost"))
        await _record(billed if billed is not None else CALL_CREDITS if r.status_code == 200 else 0)
        if (left := _i(r.headers.get("x-credits-remaining"))) is not None:
            _api["remaining"] = left
    if r.status_code == 402:
        _api["stopped"] = now.date()
        log.warning("fomoapi: credits exhausted (402); no REST calls until tomorrow (UTC)")
        raise Exhausted("fomoapi: out of credits (402)")
    if r.status_code == 429:
        _api["cool_until"] = time.monotonic() + _api["cool_s"]
        log.warning("fomoapi %s -> 429; no REST calls for %.0f s", path, _api["cool_s"])
        _api["cool_s"] = min(COOL_MAX, _api["cool_s"] * 2)
        return None
    _api["cool_s"] = COOL_MIN
    if r.status_code in (401, 403):
        raise RuntimeError(f"fomoapi: HTTP {r.status_code}, the key was refused")
    if r.status_code != 200:
        log.warning("fomoapi %s -> HTTP %s", path, r.status_code)
        return None
    try:
        return clean(r.json())
    except ValueError:
        log.warning("fomoapi %s: the answer was not JSON", path)
        return None


# ─── boards ──────────────────────────────────────────────────────────────────
def parse_board(body) -> list[dict]:
    """A /v2/leaderboard/{window} body -> trader rows in board order. Ranks are FOMO's; the position stands in if a rank
    is missing or repeated."""
    rows = []
    for t in (body.get("traders") or []) if isinstance(body, dict) else []:
        handle = _s(t.get("handle")) if isinstance(t, dict) else None
        if not handle:
            continue
        rows.append({
            "rank": _i(t.get("rank")), "handle": handle, "user_id": _s(t.get("userId")),
            "display_name": _s(t.get("displayName")), "pnl_usd": _f(t.get("pnlUsd")), "volume_usd": _f(t.get("volumeUsd")),
            "trades": _i(t.get("trades")), "followers": _i(t.get("followers")), **wallets(t.get("wallets")),
            "created_at": _ts(t.get("createdAt")), "clan": t.get("clan") if isinstance(t.get("clan"), dict) else None})
    ranks = [r["rank"] for r in rows]
    if None in ranks or len(set(ranks)) != len(ranks):
        for i, r in enumerate(rows, 1):
            r["rank"] = i
    return rows


# Wallets: a new one replaces the old with its own evidence; a missing one keeps what we had.
WALLET_SET = """
  solana_wallet = coalesce(EXCLUDED.solana_wallet, de_fomo_traders.solana_wallet),
  solana_evidence = CASE WHEN EXCLUDED.solana_wallet IS NULL THEN de_fomo_traders.solana_evidence
                         ELSE EXCLUDED.solana_evidence END,
  evm_wallet = coalesce(EXCLUDED.evm_wallet, de_fomo_traders.evm_wallet),
  evm_evidence = CASE WHEN EXCLUDED.evm_wallet IS NULL THEN de_fomo_traders.evm_evidence ELSE EXCLUDED.evm_evidence END,
  display_name = coalesce(EXCLUDED.display_name, de_fomo_traders.display_name),
  followers = coalesce(EXCLUDED.followers, de_fomo_traders.followers),
  updated_at = EXCLUDED.updated_at"""

TRADER_SQL = """
INSERT INTO de_fomo_traders (handle, user_id, display_name, solana_wallet, solana_evidence, evm_wallet, evm_evidence,
                             {pnl}, volume_usd, trades, followers, account_created_at, clan, first_seen, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $14)
ON CONFLICT (handle) DO UPDATE SET {wallets},
  user_id = coalesce(EXCLUDED.user_id, de_fomo_traders.user_id),
  {pnl} = EXCLUDED.{pnl}, volume_usd = EXCLUDED.volume_usd, trades = EXCLUDED.trades,
  account_created_at = coalesce(EXCLUDED.account_created_at, de_fomo_traders.account_created_at),
  clan = EXCLUDED.clan"""

BEST_RANK_SQL = """
WITH latest AS (SELECT "window", max(day) AS day FROM de_fomo_leaderboard WHERE day > $1::date - $2::int GROUP BY 1),
ranked AS (SELECT b.handle, min(b.rank) AS r FROM de_fomo_leaderboard b
           JOIN latest l ON l."window" = b."window" AND l.day = b.day GROUP BY 1)
UPDATE de_fomo_traders t SET best_rank = x.r
FROM (SELECT t2.handle, ranked.r FROM de_fomo_traders t2 LEFT JOIN ranked ON ranked.handle = t2.handle) x
WHERE x.handle = t.handle AND t.best_rank IS DISTINCT FROM x.r"""


async def store_board(window: str, rows: list[dict], day: date, now: datetime) -> int:
    """One window's board: traders upserted (that window's PnL; volume and trades from the longest window, as the
    windows are stored shortest first), whoever left the board loses that window's PnL, the day's ranks replaced."""
    pnl = PNL[window]
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(TRADER_SQL.format(pnl=pnl, wallets=WALLET_SET), [
            (r["handle"], r["user_id"], r["display_name"], r["solana_wallet"], r["solana_evidence"], r["evm_wallet"],
             r["evm_evidence"], r["pnl_usd"], r["volume_usd"], r["trades"], r["followers"], r["created_at"],
             _json(r["clan"]), now) for r in rows])
        await conn.execute(
            f"UPDATE de_fomo_traders SET {pnl} = NULL WHERE {pnl} IS NOT NULL AND NOT handle = ANY($1::text[])",
            [r["handle"] for r in rows])
        await conn.execute('DELETE FROM de_fomo_leaderboard WHERE day = $1 AND "window" = $2', day, window)
        await conn.executemany(
            """INSERT INTO de_fomo_leaderboard (day, "window", rank, handle, pnl_usd, volume_usd)
               VALUES ($1, $2, $3, $4, $5, $6)""",
            [(day, window, r["rank"], r["handle"], r["pnl_usd"], r["volume_usd"]) for r in rows])
    return len(rows)


_boards_failed: set[tuple[date, str]] = set()         # boards bought but unstorable: (UTC day, window)


async def leaderboard(windows: tuple[str, ...] = WINDOWS) -> int:
    """deep_fomo_leaderboard: each board not yet stored today (UTC), shortest window first. -> traders written."""
    if not env_str("FOMOAPI_KEY"):
        return 0
    now = datetime.now(UTC)
    pool = await db.pool()
    done = {r["window"] for r in await pool.fetch(
        'SELECT DISTINCT "window" FROM de_fomo_leaderboard WHERE day = $1', now.date())}
    n = 0
    for w in [w for w in windows if w not in done and (now.date(), w) not in _boards_failed]:
        try:
            body = await get(f"/v2/leaderboard/{w}", {"limit": 150}, kind="board")
        except (Exhausted, BudgetExceeded) as e:
            log.warning("%s: %s", BOARD_JOB, e)
            break
        if body is None:                             # get() logged why; the next hour tries again
            continue
        try:
            rows = parse_board(body)
            if not rows:
                log.warning("%s: the %s board came back empty", BOARD_JOB, w)
                continue
            n += await store_board(w, rows, now.date(), now)
        except UNSTORABLE as e:                      # its 250 credits are spent: not bought again today
            _boards_failed.add((now.date(), w))
            log.warning("%s: the %s board could not be stored (%s); not bought again today", BOARD_JOB, w,
                        type(e).__name__)
    if n:
        await pool.execute(BEST_RANK_SQL, now.date(), CURRENT_DAYS)
    await prune(now)
    return n


# FOMO's alerts and holder snapshots are kept RETAIN (the dossier reads the last 7 days; a call lives on in the ledger
# and de_fomo_calls). The first evening kept ~10.8k alerts a day at ~0.9 KB each.
RETAIN = timedelta(days=30)
PRUNE_BATCH = 20_000


async def prune(now: datetime) -> dict:
    """Drop alerts and holder snapshots older than RETAIN, a batch at a time (hourly, so no run holds a long lock)."""
    pool = await db.pool()
    cut = now - RETAIN
    events = await pool.execute(
        """DELETE FROM de_fomo_events WHERE event_id IN (
             SELECT event_id FROM de_fomo_events WHERE received_at < $1 LIMIT $2)""", cut, PRUNE_BATCH)
    held = await pool.execute(
        """DELETE FROM de_fomo_holders WHERE ctid IN (
             SELECT ctid FROM de_fomo_holders WHERE snapshot_at < $1 LIMIT $2)""", cut, PRUNE_BATCH)
    try:
        await pool.execute("DELETE FROM de_fomo_holder_demand WHERE day < $1", cut.date())
    except asyncpg.UndefinedTableError:
        pass
    return {"events": _deleted(events), "holders": _deleted(held)}


def _deleted(status) -> int:
    """asyncpg's command tag ("DELETE 12") -> 12."""
    tail = status.split()[-1] if isinstance(status, str) and status.split() else ""
    return int(tail) if tail.isdigit() else 0


# ─── the live feed ───────────────────────────────────────────────────────────
@dataclass
class Filters:
    """What the feed keeps: the traders on the current boards (by FOMO's user id, else by handle) with their ranks,
    the Deep End's watch list, and the whale floor."""
    by_user: dict[str, dict] = field(default_factory=dict)
    by_handle: dict[str, dict] = field(default_factory=dict)        # lowercase handle -> {handle, user_id, ranks}
    watch: set[tuple[str, str]] = field(default_factory=set)
    whale_usd: float = WHALE_USD

    def trader(self, user_id, handle) -> dict | None:
        return self.by_user.get(str(user_id or "")) or self.by_handle.get(str(handle or "").lower())


def top(ranks: dict[str, int]) -> bool:
    return any(ranks.get(w, TOP_N + 1) <= TOP_N for w in TOP_WINDOWS)


def parse_event(m: dict, received_at: datetime) -> dict | None:
    """A {type: "alert"} message -> an event row (chain None when it is not one of ours), or None."""
    if not isinstance(m, dict) or m.get("type") != "alert":
        return None
    m = clean(m)
    kind = (_s(m.get("alertType")) or "").lower()
    if not kind:
        return None
    eid = m.get("eventId") or m.get("id")
    if not eid:                                      # push-sourced alerts carry no id: hash what identifies them
        eid = "h:" + hashlib.sha1(orjson.dumps([kind, m.get("trader"), m.get("tokenAddress"), m.get("ts"),
                                                m.get("text")])).hexdigest()
    addr = _s(m.get("tokenAddress"))
    return {"event_id": str(eid), "ts": _ts(m.get("ts")) or received_at, "type": kind, "handle": _s(m.get("trader")),
            "user_id": _s(m.get("userId")), "chain": our_chain(m.get("chain"), m.get("chainId")),
            "token_address": norm(addr) if addr else None, "symbol": _s(m.get("token")),
            "usd_value": _f(m.get("usdValue")), "text": _s(m.get("text")), "raw": m, "received_at": received_at}


def position_usd(raw: dict) -> float | None:
    """A buy's position value after it (FOMO's usdValue on a buy: the app's "($3K size)")."""
    if str(raw.get("alertType") or "").lower() != "buy":
        return None
    return _f(raw["positionValueUsd"]) if raw.get("positionValueUsd") is not None else _f(raw.get("usdValue"))


def size(raw: dict) -> float | None:
    """A trade's USD size: the on-chain fill when fomoapi matched one (tradeUsd), else a buy's position after it. A
    sell without a matched fill has none: its usdValue is the position's realized PnL so far, not a size."""
    fill = _f(raw.get("tradeUsd"))
    return fill if fill is not None else position_usd(raw)


def whale(raw: dict, floor: float) -> bool:
    """A whale's trade: alertType whale, or a fill, or the position a buy leaves, of at least `floor`."""
    if str(raw.get("alertType") or "").lower() == "whale":
        return True
    return max(_f(raw.get("tradeUsd")) or 0.0, position_usd(raw) or 0.0) >= floor


def why_kept(e: dict, f: Filters) -> str | None:
    """Why an event is stored (the first reason that holds): tracked (a buy or sell by anyone on a current board),
    thesis, watch (a token on our watch list), whale; None: not stored, only counted."""
    if e["type"] in TRADES and f.trader(e["user_id"], e["handle"]):
        return "tracked"
    if e["type"] == "thesis":
        return "thesis"
    if e["token_address"] and (e["chain"], e["token_address"]) in f.watch:
        return "watch"
    if e["type"] in (*TRADES, "whale") and whale(e["raw"], f.whale_usd):
        return "whale"
    return None


def call_ranks(e: dict, f: Filters) -> dict | None:
    """The trader's ranks when the event is a call: a buy of a token by anyone on a current 7d or 30d board (ranks
    1-150), so the grading can compare rank bands. Only a top trader's call brings its token into the price pipeline
    (store_events); the others are graded when we price the token anyway."""
    if e["type"] != "buy" or not e["token_address"] or not e["chain"]:
        return None
    t = f.trader(e["user_id"], e["handle"])
    return t["ranks"] if t and any(t["ranks"].get(w) is not None for w in TOP_WINDOWS) else None


def position(e: dict) -> str:
    """The position a buy opens or adds to: FOMO's tradeId, else the trader and token that UTC day."""
    tid = e["raw"].get("tradeId")
    return str(tid) if tid else f"{e['user_id'] or e['handle']}:{e['chain']}:{e['token_address']}:{e['ts'].date()}"


INSERT_EVENTS_SQL = """
INSERT INTO de_fomo_events (event_id, ts, type, handle, user_id, chain, token_address, symbol, usd_value, text, raw,
                            received_at)
SELECT e, t, ty, h, u, c, a, s, v, tx, r::jsonb, rc
FROM unnest($1::text[], $2::timestamptz[], $3::text[], $4::text[], $5::text[], $6::text[], $7::text[], $8::text[],
            $9::float8[], $10::text[], $11::text[], $12::timestamptz[]) AS x(e, t, ty, h, u, c, a, s, v, tx, r, rc)
ON CONFLICT (event_id) DO NOTHING
RETURNING event_id"""

INSERT_TOKENS_SQL = """
INSERT INTO micro_tokens (chain, address, symbol, first_seen, first_seen_via, resolved_chain)
SELECT c, a, s, t, 'fomo', c FROM unnest($1::text[], $2::text[], $3::text[], $4::timestamptz[]) AS x(c, a, s, t)
ON CONFLICT (chain, address) DO NOTHING
RETURNING address"""

INSERT_CALLS_SQL = """
INSERT INTO de_fomo_calls (event_id, position, at, received_at, chain, address, symbol, handle, user_id, rank_7d,
                           rank_30d, size_usd)
SELECT * FROM unnest($1::text[], $2::text[], $3::timestamptz[], $4::timestamptz[], $5::text[], $6::text[], $7::text[],
                     $8::text[], $9::text[], $10::int[], $11::int[], $12::float8[])
ON CONFLICT DO NOTHING
RETURNING src_id"""


def _columns(rows: list[tuple]) -> list[list]:
    """Row tuples -> one list per column, for unnest()."""
    return [list(c) for c in zip(*rows)]


async def store_events(rows: list[dict]) -> dict:
    """Write kept events, and the calls among those new to us, in one transaction. A call's token enters micro_tokens
    (under the fold's lock, as the ingest endpoint does) unless it is there. -> {events, calls, tokens} written."""
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        new = {r["event_id"] for r in await conn.fetch(INSERT_EVENTS_SQL, *_columns([
            (e["event_id"], e["ts"], e["type"], e["handle"], e["user_id"], e["chain"], e["token_address"], e["symbol"],
             e["usd_value"], e["text"], _json(e["raw"]), e["received_at"]) for e in rows]))}
        calls: dict[str, dict] = {}                  # each position's first buy (de_fomo_calls keeps it unique too)
        for e in sorted((e for e in rows if e.get("call") is not None and e["event_id"] in new), key=lambda e: e["ts"]):
            calls.setdefault(position(e), e)
        tokens = written = 0
        if calls:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('micro_tokens'))")
            first: dict[tuple, dict] = {}
            for e in calls.values():
                if top(e["call"]):                   # the price pipeline's capacity goes to top traders' tokens
                    first.setdefault((e["chain"], e["token_address"]), e)
            tokens = len(await conn.fetch(INSERT_TOKENS_SQL, *_columns([
                (c, a, e["symbol"], e["ts"]) for (c, a), e in first.items()]))) if first else 0
            written = len(await conn.fetch(INSERT_CALLS_SQL, *_columns([
                (e["event_id"], pos, e["ts"], e["received_at"], e["chain"], e["token_address"], e["symbol"],
                 e["handle"] or "unknown", e["user_id"], e["call"].get("7d"), e["call"].get("30d"), size(e["raw"]))
                for pos, e in calls.items()])))
    return {"events": len(new), "calls": written, "tokens": tokens}


TRACKED_SQL = """
WITH latest AS (SELECT "window", max(day) AS day FROM de_fomo_leaderboard
                WHERE day > (now() AT TIME ZONE 'UTC')::date - $1::int GROUP BY 1)
SELECT b."window", b.rank, b.handle, t.user_id
FROM de_fomo_leaderboard b JOIN latest l ON l."window" = b."window" AND l.day = b.day
LEFT JOIN de_fomo_traders t ON t.handle = b.handle"""


def filters_from(rows: list[dict], watch_keys: set, whale_usd: float) -> Filters:
    by_user, by_handle = {}, {}
    for r in rows:
        t = by_handle.setdefault(r["handle"].lower(), {"handle": r["handle"], "user_id": r["user_id"], "ranks": {}})
        t["ranks"][r["window"]] = r["rank"]
        if r["user_id"]:
            by_user[r["user_id"]] = t
    return Filters(by_user, by_handle, watch_keys, whale_usd)


async def load_filters(prev: Filters | None = None) -> Filters:
    """The current boards' traders and the watch list (on failure, the previous list)."""
    pool = await db.pool()
    try:
        rows = [dict(r) for r in await pool.fetch(TRACKED_SQL, CURRENT_DAYS)]
    except asyncpg.UndefinedTableError:              # before migration 029
        rows = []
    try:
        keys = {(t["chain"], t["address"]) for t in await watch.tokens()}
    except Exception as e:                           # the feed keeps its last list
        log.warning("%s: watch list unavailable (%s); keeping the last one", FEED_JOB, type(e).__name__)
        keys = prev.watch if prev else set()
    return filters_from(rows, keys, env_float("DEEP_FOMO_WHALE_USD", WHALE_USD))


def backoff(attempt: int, status: int | None = None) -> float:
    """Seconds before reconnecting: 2 s doubling to 5 minutes; a refused key 15 minutes; a 429 at least a minute."""
    if status in (401, 403):
        return REFUSED_S
    wait = min(BACKOFF_MAX, BACKOFF_MIN * 2 ** attempt)
    return max(wait, 60.0) if status == 429 else wait


class Idle(Exception):
    """The socket stayed open but silent for IDLE_S."""


class Paused(Exception):
    """deep_fomo_feed was paused on the Output page."""


class Reader:
    """The live feed in this process: one socket, reconnected with backoff, kept events written in batches.
    `connect` and `sleep` are swappable for tests."""

    def __init__(self, connect=ws_connect, sleep=asyncio.sleep):
        self.connect, self.sleep = connect, sleep
        self.filters = Filters()
        self.tally: Counter = Counter()              # since the last report()
        self.buffer: list[dict] = []
        self.buffered_at = 0.0
        self.seen: deque = deque(maxlen=SEEN_IDS)
        self.seen_ids: set[str] = set()
        self.task: asyncio.Task | None = None
        self.since: datetime | None = None           # the open connection's start
        self.welcome: dict | None = None
        self.error: str | None = None

    def ensure(self) -> bool:
        """Start the reader task unless it is running. -> True when it had to start one."""
        if self.task is not None and not self.task.done():
            return False
        if self.task is not None and not self.task.cancelled() and self.task.exception() is not None:
            log.warning("%s: the reader stopped on %s; restarting", FEED_JOB, self._safe(self.task.exception()))
        self.task = asyncio.get_running_loop().create_task(self.run(), name=f"{FEED_JOB} reader")
        return True

    @staticmethod
    def _safe(e: BaseException) -> str:
        s = f"{type(e).__name__}: {e}"[:300]
        key = env_str("FOMOAPI_KEY")
        return s.replace(quote(key, safe=""), "***").replace(key, "***") if key else s

    async def run(self) -> None:
        """Connect, read, reconnect, until cancelled, paused on the Output page, or the key is gone. A buffer left by a
        cancellation is not written: the socket replays its last 100 alerts on the next connect."""
        attempt = 0
        while True:
            if await controls.paused(FEED_JOB):
                log.info("%s paused on the Output page; the reader stops", FEED_JOB)
                return
            key = env_str("FOMOAPI_KEY")
            if not key:
                return
            opened, status = None, None
            try:
                # The socket takes the key on the query string (fomoapi's docs); websockets' logger is held at
                # WARNING above, so the URL is never logged.
                async with self.connect(f"{WS_URL}?key={quote(key, safe='')}", open_timeout=20, ping_interval=20,
                                        ping_timeout=20, close_timeout=5, max_size=2 ** 21) as ws:
                    opened, self.since, self.welcome = time.monotonic(), datetime.now(UTC), None
                    await self.pump(ws)
            except Paused:
                self.since = None
                await self.flush(force=True)
                log.info("%s paused on the Output page; the reader stops", FEED_JOB)
                return
            except Exception as e:                   # anything else: the reader outlives it
                status = e.response.status_code if isinstance(e, InvalidStatus) else None
                self.error = self._safe(e)
                self.tally["disconnects"] += 1
                level = logging.INFO if isinstance(e, ConnectionClosed | Idle) else logging.WARNING
                log.log(level, "%s: socket %s", FEED_JOB, self.error)
            finally:
                self.since = None
            await self.flush(force=True)
            if opened is not None and time.monotonic() - opened >= STABLE_S:
                attempt = 0
            await self.sleep(backoff(attempt, status))
            attempt += 1

    async def pump(self, ws) -> None:
        """Read until the socket fails, goes silent (Idle) or the job is paused; flush as batches fill or age."""
        last = checked = time.monotonic()
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=FLUSH_S)
            except TimeoutError:
                msg = None
            now = time.monotonic()
            if msg is not None:
                last = now
                self.on_message(msg, datetime.now(UTC))
            elif now - last > IDLE_S:
                raise Idle(f"no message for {IDLE_S:.0f} s")
            await self.flush()
            if now - checked >= controls.CACHE_S:
                checked = now
                if await controls.paused(FEED_JOB):
                    raise Paused()

    def on_message(self, msg: str | bytes, now: datetime) -> None:
        """Count every message by type; buffer the alerts we keep. A message we can't make sense of is counted
        (bad_messages) and skipped: it never takes the reader down, which would replay it and fail again."""
        try:
            self._on_message(msg, now)
        except Exception as e:  # noqa: BLE001
            self.tally["bad_messages"] += 1
            log.warning("%s: skipped a message we couldn't read (%s)", FEED_JOB, type(e).__name__)

    def _on_message(self, msg: str | bytes, now: datetime) -> None:
        try:
            m = orjson.loads(msg)
        except orjson.JSONDecodeError:
            self.tally["bad_json"] += 1
            return
        if isinstance(m, list):                      # a batch, if one ever comes
            for x in m:
                self.on_message(orjson.dumps(x), now)
            return
        kind = m.get("type") if isinstance(m, dict) else None
        if kind == "welcome":
            self.welcome = {"realtime": m.get("realtime"), "delay_s": m.get("delaySeconds")}
            log.info("%s: connected (realtime %s, delay %s s)", FEED_JOB, m.get("realtime"), m.get("delaySeconds"))
            return
        e = parse_event(m, now)
        if e is None:
            self.tally[f"message:{kind}"] += 1
            return
        if e["event_id"] in self.seen_ids:
            self.tally["replayed"] += 1
            return
        if len(self.seen) == self.seen.maxlen:
            self.seen_ids.discard(self.seen[0])
        self.seen.append(e["event_id"])
        self.seen_ids.add(e["event_id"])
        self.tally["seen"] += 1
        self.tally[f"type:{e['type']}"] += 1
        if e["chain"] is None:
            self.tally["other_chain"] += 1
            return
        why = why_kept(e, self.filters)
        if why is None:
            return
        self.tally[f"kept:{why}"] += 1
        e["call"] = call_ranks(e, self.filters)
        if not self.buffer:
            self.buffered_at = time.monotonic()
        self.buffer.append(e)

    async def flush(self, force: bool = False) -> None:
        young = len(self.buffer) < BATCH and time.monotonic() - self.buffered_at < FLUSH_S
        if not self.buffer or (young and not force):
            return
        rows = self.buffer[:BATCH * 5]
        try:
            try:
                out = await store_events(rows)
            except DATA_ERRORS:                      # a row that can't be stored: the rest one by one, it dropped
                out = await self.one_by_one(rows)
        except Exception as e:                       # the database is down: keep the rows, try again
            self.tally["write_errors"] += 1
            log.warning("%s: writing %d events failed: %s", FEED_JOB, len(rows), type(e).__name__)
            if len(self.buffer) > MAX_BUFFER:
                self.tally["dropped"] += len(self.buffer) - MAX_BUFFER
                del self.buffer[:len(self.buffer) - MAX_BUFFER]
            return
        del self.buffer[:len(rows)]
        self.buffered_at = time.monotonic()
        self.tally["landed"] += out["events"]
        self.tally["calls"] += out["calls"]
        self.tally["new_tokens"] += out["tokens"]

    async def one_by_one(self, rows: list[dict]) -> dict:
        """Write a refused batch a row at a time, in time order (a position's first buy stays its call); a row the
        database still refuses is dropped and counted (bad_rows), never retried."""
        out = Counter()
        for e in sorted(rows, key=lambda e: e["ts"]):
            try:
                out.update(await store_events([e]))
            except DATA_ERRORS as err:
                self.tally["bad_rows"] += 1
                log.warning("%s: dropped event %s the database refused (%s)", FEED_JOB, e["event_id"],
                            type(err).__name__)
        return {"events": out["events"], "calls": out["calls"], "tokens": out["tokens"]}

    def report(self) -> dict:
        """What happened since the last report (the counters restart): rows = events landed."""
        t, self.tally = self.tally, Counter()
        return {"rows": t["landed"], "seen": t["seen"], "calls": t["calls"], "new_tokens": t["new_tokens"],
                "types": {k[5:]: v for k, v in sorted(t.items()) if k.startswith("type:")},
                "kept": {k[5:]: v for k, v in sorted(t.items()) if k.startswith("kept:")},
                "other_chain": t["other_chain"], "replayed": t["replayed"], "heartbeats": t["message:heartbeat"],
                "disconnects": t["disconnects"], "write_errors": t["write_errors"], "dropped": t["dropped"],
                "bad_rows": t["bad_rows"], "bad_messages": t["bad_messages"],
                "buffered": len(self.buffer), "connected": self.since is not None,
                "realtime": (self.welcome or {}).get("realtime"), "delay_s": (self.welcome or {}).get("delay_s"),
                "tracked": len(self.filters.by_handle), "watch": len(self.filters.watch),
                **({"error": self.error} if self.since is None and self.error else {})}


_reader = Reader()


async def feed() -> dict:
    """deep_fomo_feed, every minute: refresh what the reader keeps, make sure it runs, and report what landed."""
    if not env_str("FOMOAPI_KEY"):
        return {"rows": 0, "reader": "no FOMOAPI_KEY"}
    _reader.filters = await load_filters(_reader.filters)
    started = _reader.ensure()
    out = _reader.report() | {"started": started}
    log.info("%s: %d landed, %d calls; seen %d (%s); %s", FEED_JOB, out["rows"], out["calls"], out["seen"],
             ", ".join(f"{k} {v}" for k, v in out["types"].items()) or "nothing",
             "connected" if out["connected"] else "reconnecting")
    if out["bad_rows"]:          # rows were dropped: fail the run so it shows on the System page (and alerts if it lasts)
        raise RuntimeError(f"{FEED_JOB}: dropped {out['bad_rows']} event(s) the database refused (see the warnings)")
    return out


# ─── holders ─────────────────────────────────────────────────────────────────
def parse_holders(body, now: datetime) -> dict | None:
    """A /token/{address}/holders body -> {holders: [...], snapshot_at, stale}, or None when fomoapi has nothing for
    the token (available: false). A stale answer (FOMO did not answer; fomoapi's last good one) is dated by its age."""
    if not isinstance(body, dict) or body.get("available") is False:
        return None
    body = clean(body)
    out, seen = [], set()
    for h in body.get("holders") or []:
        handle = _s(h.get("handle")) if isinstance(h, dict) else None
        if not handle or handle.lower() in seen:
            continue
        seen.add(handle.lower())
        h = h | {"handle": handle}
        out.append({"handle": handle, "display_name": _s(h.get("displayName")), "amount": _f(h.get("amount")),
                    "value_usd": _f(h.get("valueUsd")), "pnl_usd": _f(h.get("pnlUsd")),
                    "cost_basis_usd": _f(h.get("costBasisUsd")), "is_dev": h.get("isDev"),
                    "thesis": _s(h.get("thesis")), "followers": _i(h.get("followers")),
                    "clan": h.get("clan") if isinstance(h.get("clan"), dict) else None, **wallets(h.get("wallet"))})
    stale = bool(body.get("stale"))
    age = _f(body.get("ageSeconds")) if stale else None
    return {"holders": out, "stale": stale, "snapshot_at": now - timedelta(seconds=age) if age else now}


def holders_due(t: dict, now: datetime) -> bool:
    """Never checked, or checked HOLDERS_EVERY ago; a token nobody held waits EMPTY_EVERY unless a FOMO event on it
    came in since."""
    last = t.get("checked_at")
    if last is None:
        return True
    if t.get("holders") or (t.get("last_event") and t["last_event"] > last):
        return now - last >= HOLDERS_EVERY
    return now - last >= EMPTY_EVERY


def holders_order(t: dict, now: datetime) -> tuple:
    """Higher first: FOMO traders active in it in the last 24 h, a dossier just opened, never checked, the longest
    unchecked."""
    last = t.get("checked_at")
    return ((t.get("events_24h") or 0) > 0, "opened" in (t.get("why") or []), last is None,
            (now - last) if last else timedelta(0))


CANDIDATES_SQL = """
WITH k AS (SELECT * FROM unnest($1::text[], $2::text[]) AS k(chain, address))
SELECT k.chain, k.address, c.checked_at, c.holders, e.events_24h, e.last_event
FROM k
LEFT JOIN de_fomo_holder_checks c ON c.chain = k.chain AND c.token_address = k.address
LEFT JOIN LATERAL (SELECT count(*) FILTER (WHERE ts > now() - interval '24 hours') AS events_24h, max(ts) AS last_event
                   FROM de_fomo_events WHERE chain = k.chain AND token_address = k.address) e ON TRUE"""

HOLDER_TRADER_SQL = f"""
INSERT INTO de_fomo_traders (handle, display_name, solana_wallet, solana_evidence, evm_wallet, evm_evidence, followers,
                             clan, first_seen, updated_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $9)
ON CONFLICT (handle) DO UPDATE SET {WALLET_SET},
  clan = coalesce(EXCLUDED.clan, de_fomo_traders.clan)"""


async def store_holders(chain: str, address: str, got: dict | None, at: datetime) -> int:
    """One lookup: the holders' snapshot, the token's check row, and the holders' wallets into de_fomo_traders
    (matched to a known trader case-insensitively). -> holders stored."""
    hs = (got or {}).get("holders") or []
    snap = (got or {}).get("snapshot_at") or at
    status = "unavailable" if got is None else "none" if not hs else "stale" if got["stale"] else "ok"
    details = {h["handle"].lower(): {k: h[k] for k in ("is_dev", "pnl_usd", "cost_basis_usd", "thesis")} for h in hs}
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        known = {r["handle"].lower(): r["handle"] for r in await conn.fetch(
            "SELECT handle FROM de_fomo_traders WHERE lower(handle) = ANY($1::text[])", list(details))}
        name = {h["handle"].lower(): known.get(h["handle"].lower(), h["handle"]) for h in hs}
        await conn.executemany(
            """INSERT INTO de_fomo_holders (snapshot_at, chain, token_address, handle, amount, value_usd)
               VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING""",
            [(snap, chain, address, name[h["handle"].lower()], h["amount"], h["value_usd"]) for h in hs])
        await conn.executemany(HOLDER_TRADER_SQL, [
            (name[h["handle"].lower()], h["display_name"], h["solana_wallet"], h["solana_evidence"], h["evm_wallet"],
             h["evm_evidence"], h["followers"], _json(h["clan"]), at) for h in hs])
        await conn.execute(
            """INSERT INTO de_fomo_holder_checks (chain, token_address, checked_at, snapshot_at, holders, status,
                                                  details)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
               ON CONFLICT (chain, token_address) DO UPDATE SET checked_at = EXCLUDED.checked_at,
                 snapshot_at = EXCLUDED.snapshot_at, holders = EXCLUDED.holders, status = EXCLUDED.status,
                 details = EXCLUDED.details""",
            chain, address, at, snap, len(hs), status, _json(details))
    return len(hs)


async def lookup_holders(chain: str, address: str) -> int | None:
    """One token's FOMO holders, stored. -> holders found, or None when the call failed (tried again next run)."""
    body = await get(f"/token/{address}/holders", {"limit": HOLDERS_LIMIT}, kind="holders")
    if body is None:
        return None
    now = datetime.now(UTC)
    try:
        return await store_holders(chain, address, parse_holders(body, now), now)
    except UNSTORABLE as e:                          # its credits are spent: wait like any check, don't re-buy it
        log.warning("%s: %s %s: an answer the database refused (%s); marked failed", HOLDERS_JOB, chain, address,
                    type(e).__name__)
        await mark_failed(chain, address, now)
        return 0


async def mark_failed(chain: str, address: str, at: datetime) -> None:
    """A lookup whose answer could not be stored: the check row says so (keeping any earlier snapshot), so
    holders_due() waits before buying it again."""
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_fomo_holder_checks (chain, token_address, checked_at, snapshot_at, holders, status)
           VALUES ($1, $2, $3, $3, 0, 'failed')
           ON CONFLICT (chain, token_address) DO UPDATE SET checked_at = EXCLUDED.checked_at, status = 'failed'""",
        chain, address, at)


async def holders(limit: int = HOLDERS_PER_RUN) -> int:
    """deep_fomo_holders: the most deserving due hot tokens, as far as the credits' pace allows. -> holders stored."""
    if not env_str("FOMOAPI_KEY"):
        return 0
    now = datetime.now(UTC)
    hot = [t for t in await watch.tokens("hot") if t["chain"] in CHAINS]
    if not hot:
        return 0
    pool = await db.pool()
    extra = {(r["chain"], r["address"]): dict(r) for r in await pool.fetch(
        CANDIDATES_SQL, [t["chain"] for t in hot], [t["address"] for t in hot])}
    cands = [{**t, **extra.get((t["chain"], t["address"]), {})} for t in hot]
    due = sorted((t for t in cands if holders_due(t, now)), key=lambda t: holders_order(t, now), reverse=True)
    await demand(due, [], now)
    n, looked = 0, []
    for t in due[:limit]:
        if cooling():                                # a 429 stopped the calls (get() checks the same with no await
            break                                    # between): the rest wait for a later run, not served
        try:
            got = await lookup_holders(t["chain"], t["address"])
        except (Exhausted, BudgetExceeded) as e:      # the credits said no: this and the rest go unserved
            log.info("%s: %s", HOLDERS_JOB, e)
            break
        looked.append(t)                             # a call went out on our budget; an outage is not a credit shortage
        n += got or 0
    await demand([], looked, now)
    return n


DEMAND_SQL = """
INSERT INTO de_fomo_holder_demand (day, chain, token_address, first_due_at, looked_at)
SELECT $1::date, c, a, $2::timestamptz, CASE WHEN l THEN $2::timestamptz END
FROM unnest($3::text[], $4::text[], $5::bool[]) AS x(c, a, l)
ON CONFLICT (day, chain, token_address) DO UPDATE
  SET looked_at = coalesce(de_fomo_holder_demand.looked_at, EXCLUDED.looked_at)"""


async def demand(due: list[dict], looked: list[dict], now: datetime) -> None:
    """Record today's demand for holder lookups: the hot tokens due one, and those that got it. The subscription test
    (fomo_verdict.py) reads how much of the day's demand the free credits served."""
    rows = [(t["chain"], t["address"], False) for t in due] + [(t["chain"], t["address"], True) for t in looked]
    if not rows:
        return
    try:                                             # bookkeeping: it must never stop the paid lookups
        pool = await db.pool()
        await pool.execute(DEMAND_SQL, now.date(), now, *_columns(rows))
    except asyncpg.UndefinedTableError:              # before migration 035
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("%s: recording demand failed: %s", HOLDERS_JOB, type(e).__name__)


# ─── the dossier's section ───────────────────────────────────────────────────
NUMBER = "CASE WHEN jsonb_typeof(raw->'{k}') = 'number' THEN (raw->>'{k}')::float8 END"
EVENTS_SQL = f"""
SELECT ts, type, handle, user_id, text, usd_value, {NUMBER.format(k='tradeUsd')} AS fill_usd,
       {NUMBER.format(k='positionValueUsd')} AS position_usd, {NUMBER.format(k='realizedPnlUsd')} AS realized_usd
FROM de_fomo_events WHERE chain = $1 AND token_address = $2 AND ts > now() - $3::interval
ORDER BY ts DESC LIMIT 500"""

CHECK_SQL = """SELECT checked_at, snapshot_at, holders, status, details FROM de_fomo_holder_checks
WHERE chain = $1 AND token_address = $2"""

HOLDERS_SQL = """
SELECT h.handle, h.amount, h.value_usd FROM de_fomo_holders h
JOIN de_fomo_holder_checks c
  ON c.chain = h.chain AND c.token_address = h.token_address AND c.snapshot_at = h.snapshot_at
WHERE h.chain = $1 AND h.token_address = $2 ORDER BY h.value_usd DESC NULLS LAST"""

TRADERS_SQL = """
WITH latest AS (SELECT "window", max(day) AS day FROM de_fomo_leaderboard
                WHERE day > (now() AT TIME ZONE 'UTC')::date - $3::int GROUP BY 1),
ranks AS (SELECT b.handle, jsonb_object_agg(b."window", b.rank) AS ranks FROM de_fomo_leaderboard b
          JOIN latest l ON l."window" = b."window" AND l.day = b.day GROUP BY 1)
SELECT t.handle, t.user_id, t.display_name, t.pnl_30d, t.clan, r.ranks,
       s.graded, s.hit_2x, s.hit_rate_2x, s.median_24h_pct
FROM de_fomo_traders t LEFT JOIN ranks r ON r.handle = t.handle
LEFT JOIN de_caller_stats s ON s.group_name = 'app:fomo' AND lower(s.caller) = lower(t.handle)
WHERE lower(t.handle) = ANY($1::text[]) OR t.user_id = ANY($2::text[])"""
MAX_CLANS = 8
TRIM_SHARE = 0.5                         # a sell under half of what the trader still held is a trim, not an exit


def best_rank(ranks: dict | None) -> tuple[str, int] | None:
    """The trader's best rank on the boards that make a top trader (7d, 30d): the one the card shows."""
    on = {w: r for w, r in (ranks or {}).items() if w in TOP_WINDOWS and r is not None}
    return min(on.items(), key=lambda kv: kv[1]) if on else None


def exited(r: dict, snapshot_at: datetime | None) -> bool:
    """Did a trader whose last trade was a sell leave the position? Not when a holder list taken after the sell still
    shows them holding, nor when the sale was under TRIM_SHARE of what they held."""
    held = r.get("holding_usd")
    if not held:
        return True
    if snapshot_at is not None and r.get("at") is not None and snapshot_at >= r["at"]:
        return False
    return not (r.get("usd") is not None and r["usd"] < TRIM_SHARE * held)


def shape(events: list[dict], check: dict | None, holders: list[dict], traders: list[dict],
          now: datetime) -> dict | None:
    """The section from the token's FOMO events (newest first), its latest holder lookup and the traders involved.
    A trader's action is their latest trade in the window (buy; sell when they left, trim when they still hold), else
    "holds" (from the lookup). usd is a buy's fill (or the position after it), a sell's fill, or a holding's value,
    and usd_kind says which. Ranks are the 7d and 30d boards' only, the boards a top trader is judged on."""
    if not events and not holders:
        return None
    by_user = {t["user_id"]: t for t in traders if t.get("user_id")}
    by_handle = {t["handle"].lower(): t for t in traders}
    details = _load((check or {}).get("details")) or {}
    rows: dict[str, dict] = {}

    def row(handle: str | None, user_id: str | None = None) -> dict:
        t = by_user.get(user_id or "") or by_handle.get((handle or "").lower()) or {}
        name = t.get("handle") or handle or "unknown"
        if name.lower() not in rows:
            ranks = _load(t.get("ranks")) or {}
            best = best_rank(ranks)
            d = details.get(name.lower()) or details.get((handle or "").lower()) or {}
            clan = _load(t.get("clan")) or {}
            rows[name.lower()] = {
                "handle": name, "display_name": t.get("display_name"), "rank": best[1] if best else None,
                "board": best[0] if best else None, "pnl_30d": t.get("pnl_30d"), "top": top(ranks),
                "action": None, "at": None, "usd": None, "usd_kind": None, "realized_usd": None, "holding_usd": None,
                "position_pnl_usd": d.get("pnl_usd"), "is_dev": bool(d.get("is_dev")),
                "clan": _s(clan.get("name")) if isinstance(clan, dict) else None,
                "record": {k: t.get(k) for k in ("graded", "hit_2x", "hit_rate_2x", "median_24h_pct")}
                if t.get("graded") else None}
        return rows[name.lower()]

    flow = {"buys": 0, "sells": 0}
    buyers, sellers, theses = set(), set(), []
    for e in events:
        if e["type"] in TRADES:
            r = row(e["handle"], e.get("user_id"))
            if r["action"] is None:
                buy = e["type"] == "buy"
                fill = e.get("fill_usd")
                position_ = e.get("position_usd") if e.get("position_usd") is not None else e.get("usd_value")
                r.update(action=e["type"], at=e["ts"],
                         usd=fill if fill is not None else (position_ if buy else None),
                         usd_kind="fill" if fill is not None else ("position" if buy and position_ is not None else None),
                         realized_usd=None if buy else (e["realized_usd"] if e.get("realized_usd") is not None
                                                        else e.get("usd_value")))
            flow[e["type"] + "s"] += 1
            (buyers if e["type"] == "buy" else sellers).add(r["handle"].lower())
        elif e["type"] == "thesis" and len(theses) < MAX_THESES:
            theses.append({"handle": e["handle"], "rank": row(e["handle"], e.get("user_id"))["rank"],
                           "text": e["text"], "at": e["ts"], "source": "feed"})
    snapshot_at = check["snapshot_at"] if check and holders else None
    for h in holders:
        r = row(h["handle"])
        r["holding_usd"] = h["value_usd"]
        if r["action"] is None:
            r.update(action="holds", at=snapshot_at, usd=h["value_usd"], usd_kind="holding")
    for r in rows.values():
        if r["action"] == "sell" and not exited(r, snapshot_at):
            r["action"] = "trim"
    authors = {(t["handle"] or "").lower() for t in theses}
    for h in holders:                                # the holders' own theses on their positions, undated
        d = details.get(h["handle"].lower()) or {}
        if len(theses) < MAX_THESES and d.get("thesis") and h["handle"].lower() not in authors:
            theses.append({"handle": h["handle"], "rank": row(h["handle"])["rank"], "text": d["thesis"], "at": None,
                           "source": "holding"})
    active = [r for r in rows.values() if r["action"] is not None]
    active.sort(key=lambda r: (not r["top"], r["rank"] is None, r["rank"] or 0,
                               -(r["holding_usd"] or r["usd"] or 0), -(r["at"] or now).timestamp()))
    clans: dict[str, dict] = {}
    for r in active:
        if r["clan"]:
            c = clans.setdefault(r["clan"].lower(), {"name": r["clan"], "traders": 0, "top": 0})
            c["traders"] += 1
            c["top"] += r["top"]
    stale = bool(check and (check["status"] in ("stale", "failed")
                            or (snapshot_at is not None and now - snapshot_at > HOLDERS_EVERY * 4)))
    return {
        "as_of": max([e["ts"] for e in events] + ([snapshot_at] if snapshot_at else [])),
        "window_h": int(RECENT / timedelta(hours=1)),
        "traders": active[:MAX_TRADERS],
        "n_traders": len(active), "n_ranked": sum(r["rank"] is not None for r in active),
        "n_top": sum(r["top"] for r in active), "n_holders": len(holders),
        **flow, "buyers": len(buyers), "sellers": len(sellers),
        "theses": theses,
        "clans": sorted(clans.values(), key=lambda c: (-c["traders"], -c["top"], c["name"].lower()))[:MAX_CLANS],
        "holders_as_of": snapshot_at,
        "holders_stale": stale,
    }


async def section(chain: str, address: str) -> dict | None:
    """The dossier's FOMO section: which FOMO traders hold the token (its top FOMO holders at the last lookup) or bought
    or sold it in the last 7 days (handle, best current rank and its board, 30-day PnL, last action, its time and USD
    size), how many are ranked and how many are top traders (top 50 on 7d or 30d), the flow, and the latest theses.
    None without any FOMO evidence."""
    pool = await db.pool()
    try:
        events = [dict(r) for r in await pool.fetch(EVENTS_SQL, chain, address, RECENT)]
        check = await pool.fetchrow(CHECK_SQL, chain, address)
        held = [dict(r) for r in await pool.fetch(HOLDERS_SQL, chain, address)] if check and check["holders"] else []
        handles = sorted({(x["handle"] or "").lower() for x in events + held if x.get("handle")})
        users = sorted({e["user_id"] for e in events if e.get("user_id")})
        traders = [dict(r) for r in await pool.fetch(TRADERS_SQL, handles, users, CURRENT_DAYS)] \
            if handles or users else []
    except asyncpg.UndefinedTableError:              # before migration 029
        return None
    return shape(events, dict(check) if check else None, held, traders, datetime.now(UTC))


# ─── registry (albatross/deepend/registry.py) ────────────────────────────────
JOBS: dict = {BOARD_JOB: leaderboard, FEED_JOB: feed, HOLDERS_JOB: holders}
SCHEDULE: list = [(BOARD_JOB, {"minute": 7, "second": 10}), (FEED_JOB, {"second": 20}),
                  (HOLDERS_JOB, {"minute": "*/5", "second": 40})]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE: dict = {BOARD_JOB: 3_600, FEED_JOB: 60, HOLDERS_JOB: 300}
