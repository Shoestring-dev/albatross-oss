"""Alert rules: pure functions from observed state to the conditions that should be open right now.
The monitor feeds them DB rows every 5 minutes; the tests feed them hand-built rows."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

CRITICAL, WARNING = "critical", "warning"
M, H, D = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)


@dataclass(frozen=True)
class Condition:
    key: str            # identity: at most one open alert per key
    kind: str
    severity: str
    title: str          # the condition, without a duration: messages and the dashboard add "for 36m"
    detail: str
    since: datetime | None = None   # when the problem began, if known (else: when the alert opened)


# Expected gap between runs of every scheduled job (__main__.run() and the host worker). A test keeps
# this in step with the scheduler lines, so a new job can't silently go unwatched.
CADENCE = {
    "derivs_hl": M, "news": 3 * M,
    "micro_snapshots": M, "micro_pools": M, "micro_candles": M, "micro_reconcile": H, "micro_retire": H,
    "micro_ledger": M, "micro_bot_txs": M,
    "events_listings": 10 * M, "events_polymarket": H, "events_macro": D,          # events calendar feeds
    "candles_hl_5m": 5 * M, "candles_cb_5m": 5 * M, "features_ltf": 5 * M, "news_scout": 5 * M,
    "grader": 5 * M, "grade_ideas": 5 * M, "alerts": 5 * M, "paper_books": 15 * M, "codex_limits": 5 * M,
    "ltf_analyst": 30 * M, "ensemble": 30 * M, "events_news": 30 * M, "oracle_watch": 10 * M,
    "candles_hl_1h": H, "candles_okx_1h": H, "candles_cb_1h": H, "fear_greed": H, "features_htf": H,
    "derivs_cex": H, "flow_analyst": H, "strategist": H, "jev_analyst": H, "jev_scout": 5 * M, "jev_grade": 10 * M,
    "htf_analyst": 4 * H, "reports_brief": 4 * H, "social_pulse": H,
    "macro_data": 13 * H, "macro_analyst": 13 * H,           # 01:40 / 14:40: the longer gap is 13 h
    "ceo": 12 * H, "strategy_lab": H, "strategy_researcher": D, "alpha_report": H, "alpha_live": 5 * M,
    "candles_hl_1d": D, "candles_okx_1d": D, "candles_cb_1d": D, "features_daily": D, "onchain_mvrv": D, "factor_rank": D,
    "ledger_verify": D, "reports_daily": D, "oracle_daily": D, "events_oracle": D,
    "universe": 7 * D, "oracle_weekly": 7 * D, "agent_review": 7 * D,
}
# The Deep End's jobs register their own cadences (albatross/deepend/registry.py).
from albatross.deepend.registry import CADENCE as _DEEP_CADENCE  # noqa: E402

CADENCE.update({k: timedelta(seconds=v) for k, v in _DEEP_CADENCE.items()})     # modules give seconds
FAIL_RUNS = 3                   # more than this many consecutive failures
FAIL_FOR = 30 * M               # or failing this long
STALE_RUNS = 3                  # no success within this many cadences
STALE_FLOOR = 15 * M            # never stricter than this (a 1-minute job isn't stale after 3 minutes)
CANDLE_STALE = 16 * M           # newest 5m candle opened this long ago = no new candle for 15+ min
LLM_WINDOW_MIN_RUNS = 3
LLM_FAIL_RATE = 0.5
BUSY_PER_2H = 5                 # call-feed rows in a typical 2 h window for "normally busy"
SILENT = 2 * H


def dur(td: timedelta) -> str:
    m = max(0, int(td.total_seconds() // 60))
    return f"{m // 1440}d {m % 1440 // 60}h" if m >= 1440 else f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def snippet(text: str | None, n: int = 200) -> str:
    """An error message on one line (pydantic's run to several), short enough for a phone."""
    return " ".join((text or "?").split())[:n]


def hhmm(ts: datetime | None, now: datetime | None = None) -> str:
    if ts is None:
        return "never"
    return ts.strftime("%H:%M UTC" if now and ts.date() == now.date() else "%m-%d %H:%M UTC")


def jobs(health: list[dict], now: datetime, paused: frozenset | set = frozenset()) -> list[Condition]:
    """health: ingest_health rows (job, last_run, last_ok, consecutive_errors, last_error). Manual and
    one-off jobs (not in CADENCE) are ignored; a failing job gets one alert, not also a stale one. A paused job
    (the owner's pause on the Output page, or an agent the culling system retired) skips its runs on purpose: no alert."""
    out = []
    for h in health:
        job, cad = h["job"], CADENCE.get(h["job"])
        if cad is None or job in paused:
            continue
        errs, last_ok = h["consecutive_errors"] or 0, h["last_ok"]
        if errs:
            # The first failure was about the first scheduled run after the last success.
            since = last_ok + cad if last_ok else h["last_run"]
            failing_for = now - since
            if errs > FAIL_RUNS or failing_for >= FAIL_FOR:
                out.append(Condition(
                    f"job_failing:{job}", "job_failing", CRITICAL if errs > FAIL_RUNS else WARNING,
                    f"{job} failing ×{errs}",
                    f"Failing since about {hhmm(since, now)}. Last error: {snippet(h['last_error'])}", since))
                continue
        if last_ok is None or now - last_ok > max(STALE_RUNS * cad, STALE_FLOOR):
            out.append(Condition(
                f"job_stale:{job}", "job_stale", CRITICAL if cad <= 5 * M else WARNING,
                f"{job} hasn't succeeded" if last_ok else f"{job} has never succeeded",
                f"Runs every {dur(cad)}; last success {hhmm(last_ok, now)}. "
                "The scheduler may be down or the job hanging.", last_ok))
    return out


def llm(hour: list[dict], now: datetime) -> list[Condition]:
    """hour: per agent over the last hour: runs, refused (HTTP 402) with the first and last one's time,
    and the other failures with their most common error. Any 402 is one loud alert of its own: OpenRouter
    refuses calls once the balance can't cover them, often intermittently at first (whichever calls
    are in flight together), and an empty balance silently stops every analyst."""
    out = []
    # Jev's own API (TypeSafe) refuses with a 402 too: its own alert, with where to top it up
    for key, name, after in (
            ("openrouter_402", "OpenRouter", "Every OpenRouter agent stops when the balance runs out: top up at "
                                            "openrouter.ai/settings/credits."),
            ("typesafe_402", "TypeSafe (Jev)", "The Jev scout stops until TypeSafe has credits again: add them at "
                                               "console.typesafe.ai/settings/billing.")):
        refused = sorted((r for r in hour if r["refused"] and bool(r.get("typesafe_402")) == (key == "typesafe_402")),
                         key=lambda r: -r["refused"])
        if refused:
            out.append(Condition(
                key, key, CRITICAL, f"{name} refusing calls: out of credit (HTTP 402)",
                f"{plural(sum(r['refused'] for r in refused), 'call')} refused in the last hour: "
                + ", ".join(f"{r['agent']} ×{r['refused']}" for r in refused)
                + f"; last at {hhmm(max(r['last_402'] for r in refused), now)}. {after}", min(r["first_402"] for r in refused)))
    for r in hour:
        if r["runs"] < LLM_WINDOW_MIN_RUNS or r["failures"] / r["runs"] < LLM_FAIL_RATE:
            continue
        out.append(Condition(
            f"llm_failures:{r['agent']}", "llm_failures", WARNING,
            f"{r['agent']}: {r['failures']} of {r['runs']} LLM calls failed in the last hour",
            f"Most common error: {snippet(r['top_error'])}"))
    return out


def candles(latest: dict[str, datetime | None], now: datetime) -> list[Condition]:
    """latest: Tier-1 symbol -> open time of its newest 5m candle (any venue), None if none today."""
    stale = {s: t for s, t in latest.items() if t is None or now - t > CANDLE_STALE}
    if not stale:
        return []
    every = len(stale) == len(latest)
    known = [t for t in stale.values() if t is not None]
    return [Condition(
        "candles_stale", "candles_stale", CRITICAL if every else WARNING,
        "No new Tier-1 candles" if every else f"{plural(len(stale), 'Tier-1 asset')} without new candles",
        "Newest 5m candle: " + ", ".join(f"{s} {hhmm(t, now)}" for s, t in sorted(stale.items())),
        min(known) + 5 * M if known else None)]         # the next candle was due when this one closed


def call_feed(last: datetime | None, hourly: dict[datetime, int], now: datetime) -> list[Condition]:
    """last: when the call feed last delivered anything (calls or trade updates). hourly: rows
    received per UTC hour over the last 8 days. Silence only counts when the feed is normally busy at
    this time of day: the median of the same 2 h window on the previous days it existed, or, before it
    has a day of history, its overall rate."""
    if last is None or not hourly or now - last < SILENT:
        return []
    first = min(hourly)

    def window(end: datetime) -> int:
        return sum(n for h, n in hourly.items() if end - SILENT <= h < end)

    days = [window(now - d * D) for d in range(1, 8) if now - d * D - SILENT >= first]
    if days:
        typical = median(days)
    else:
        span = (now - SILENT - first) / H
        typical = sum(n for h, n in hourly.items() if h < now - SILENT) / span * 2 if span >= 1 else 0
    if typical < BUSY_PER_2H:
        return []
    return [Condition(
        "call_feed_silent", "call_feed_silent", WARNING, "Call feed silent",
        f"Nothing received since about {hhmm(last, now)}; this time of day normally brings ~{typical:.0f} rows "
        "per 2 h. Check the exporter that pushes to /api/ingest/calls.", last)]


def ledger(n: int, broken: int | None) -> list[Condition]:
    if broken is None:
        return []
    return [Condition(
        "ledger_broken", "ledger_broken", CRITICAL, f"Ledger hash chain broken at call #{broken}",
        f"{n} calls verify before it: a logged call was changed after the fact. Do not trust the ledger "
        "until this is explained.")]


def loops(agents: list[dict], now: datetime) -> list[Condition]:
    """Model agents showing a loop sign over the last two hours (albatross/tracker.py): the same input sent again
    and again, or money spent with nothing stored. Failures already have their own alert (llm)."""
    out = []
    for a in agents:
        for f in a.get("flags", []):
            if f["kind"] in ("repeating", "spinning"):
                out.append(Condition(
                    f"loop:{f['kind']}:{a['agent']}", "loop", WARNING, f"{a.get('label', a['agent'])}: {f['label'].lower()}",
                    f"{f['detail']} Pause it or scale its budget on the Output page if this isn't expected.", now))
    return out


REDDIT_MIN_READS = 10
REDDIT_EMPTY_SHARE = 0.8


def reddit(reads: int, failed: int) -> list[Condition]:
    """The Reddit scout failing to search: most of a day's reads either never reached Reddit ('blind') or were a
    major's unverified read (a major always has threads). A memecoin's 'none' is not counted: the control proved
    Reddit was in the results, and most memecoins genuinely have no thread."""
    if reads < REDDIT_MIN_READS or failed < REDDIT_EMPTY_SHARE * reads:
        return []
    return [Condition(
        "reddit_empty", "reddit_empty", WARNING, f"Reddit scout: {failed} of its last {reads} reads failed to search",
        "They never reached Reddit, or a major came back with no proven thread. Look at de_reddit_reads' audit column; "
        "until it recovers, the coin pages show no Reddit panel rather than a wrong one.")]


def spend(spent: float, threshold: float) -> list[Condition]:
    if spent < threshold:
        return []
    return [Condition(
        "spend_high", "spend_high", WARNING, f"OpenRouter spend today ${spent:.2f} (alert at ${threshold:.2f})",
        "Agents stop one by one as they reach their own daily caps. Check the Health tab's spend table.")]
