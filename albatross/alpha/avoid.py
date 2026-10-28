"""Alpha › Loss avoidance: what tells memecoin losers from the rest, measured on the same trades the paper books count
($100 a group call after every cost, one position per coin; albatross/backtest/books.py `take`), from Sep 30.

  ages        each age band at the call, held 24 h and sold at 2x
  checklists  every call, today's checklist with no red flag, and the rebuilt one (review of 2026-10-09) with none
  lines       each checklist line's calls by status: does "bad" really do worse?
  rules       two rules fixed on 2026-10-09 before any of their forward trades existed, recorded from FORWARD_FROM
  bot         the bot's own buys by age, and what skipping the young ones would have done. Paper only: nothing is
              sent to the bot.

Every comparison with every call is made day by day (lab/run.py `paired`): the market's mood swings the whole day.
"""

from datetime import UTC, datetime
from statistics import median

from albatross.backtest import books
from albatross.lab import run as lab
from albatross.lab import stats

FORWARD_FROM = datetime(2026, 10, 10, tzinfo=UTC)
READ_AT = {"trades": 1000, "date": "2026-11-01"}
KEPT_FLAGS = ("dev", "liquidity", "mint", "tax", "top10", "organic")     # today's red flags the review kept
AGES = [(0, 1, "under 1 hour"), (1, 24, "1 to 24 hours"), (24, 168, "1 to 7 days"), (168, None, "over 7 days")]
LINES = ("age", "dev", "liquidity", "organic", "top10", "mint", "tax", "lp", "insiders", "wallets", "dexpaid", "flow",
         "holders", "calls", "callers", "x", "fit", "smart", "fomo", "reddit", "github")
RULES = {
    "seasoned": {"name": "Group calls on coins 24 hours or older, no dev or liquidity red flag, sold at 2x",
                 "exit": "take_profit"},
    "rebuilt": {"name": "Group calls the rebuilt checklist marks PASS, held 24 hours", "exit": "horizon"},
}


def rebuilt_flags(f: dict) -> list[str] | None:
    """The rebuilt checklist's red flags for one call; None when the call had no dossier to judge. Today's flags it
    kept, plus: younger than 24 hours, and a Dex listing still unpaid after 24 hours. LP lock, insiders and wallets
    become information only."""
    if f.get("n_red_flags") is None:
        return None
    out = [k for k in KEPT_FLAGS if f.get(f"check:{k}") == "bad"]
    age = f.get("age_h")
    if age is not None and age < 24:
        out.append("young")
    if age is not None and age >= 24 and f.get("check:dexpaid") == "warn":
        out.append("unpaid")
    return out


def caller(c: dict) -> bool:
    return c["source_kind"] == "caller"


def aged(lo: float, hi: float | None):
    return lambda c: (c["f"].get("age_h") is not None and c["f"]["age_h"] >= lo
                      and (hi is None or c["f"]["age_h"] < hi))


def seasoned(c: dict) -> bool:
    f = c["f"]
    return (caller(c) and f.get("age_h") is not None and f["age_h"] >= 24
            and f.get("check:dev") != "bad" and f.get("check:liquidity") != "bad")


def rebuilt_clean(c: dict) -> bool:
    return caller(c) and rebuilt_flags(c["f"]) == []


def trades(calls: list[dict], pick, policy: str) -> list[tuple[float, str, object]]:
    took, _ = books.take(calls, pick, policy)
    return [(t["value"], t["coin"], t["at"].date()) for t in took if t["settled"] and t["value"] is not None]


def summary(t: list[tuple], base: list[tuple] | None = None) -> dict:
    """n, mean, median, the share that lost 90% or more, the share that made money, days, the day-clustered t, and
    the day-by-day difference from the baseline."""
    if not t:
        return {"n": 0}
    v = [x for x, _, _ in t]
    s = stats.clustered(v, [d for _, _, d in t]) or {}
    out = {"n": len(v), "mean": round(sum(v) / len(v), 2), "median": round(median(v), 2),
           "lost90": round(sum(x <= -90 for x in v) / len(v), 3), "won": round(sum(x > 0 for x in v) / len(v), 3),
           "days": len({d for _, _, d in t}), "t": s.get("t"), "usd": round(sum(v), 0)}
    if base is not None:
        out["vs_every_call"] = lab.paired(t, base)
    return out


def report(calls: list[dict], now: datetime) -> dict:
    """The whole page, from the calls meme.load() returns (oldest first)."""
    settled = [c for c in calls if lab.MEME_START <= c["logged_at"] < now - lab.SETTLE]
    every = {p: trades(settled, caller, p) for p in ("horizon", "take_profit")}

    ages = []
    for lo, hi, label in AGES:
        pick = lambda c, a=aged(lo, hi): caller(c) and a(c)          # noqa: E731
        ages.append({"band": label, "held": summary(trades(settled, pick, "horizon"), every["horizon"]),
                     "sold_2x": summary(trades(settled, pick, "take_profit"), every["take_profit"])})

    has_dossier = lambda c: caller(c) and c["f"].get("n_red_flags") is not None        # noqa: E731
    checklists = []
    for key, name, pick in (
            ("every", "Every group call", caller),
            ("dossier", "Every group call with a dossier", has_dossier),
            ("today_clean", "Today's checklist: no red flag", lambda c: has_dossier(c) and c["f"]["n_red_flags"] == 0),
            ("today_flagged", "Today's checklist: red-flagged", lambda c: has_dossier(c) and c["f"]["n_red_flags"] > 0),
            ("rebuilt_clean", "Rebuilt checklist: PASS", rebuilt_clean),
            ("rebuilt_flagged", "Rebuilt checklist: SKIP",
             lambda c: caller(c) and bool(rebuilt_flags(c["f"])))):
        checklists.append({"key": key, "name": name,
                           "held": summary(trades(settled, pick, "horizon"), every["horizon"]),
                           "sold_2x": summary(trades(settled, pick, "take_profit"), every["take_profit"])})

    lines = []
    for k in LINES:
        row = {"key": k, "red_flag_today": k in lab_flags(), "red_flag_rebuilt": k in KEPT_FLAGS or k in ("age", "dexpaid")}
        for status in ("good", "warn", "bad"):
            pick = lambda c, k=k, st=status: has_dossier(c) and c["f"].get(f"check:{k}") == st     # noqa: E731
            row[status] = summary(trades(settled, pick, "horizon"))
        if any(row[s]["n"] for s in ("good", "warn", "bad")):
            lines.append(row)

    forward = [c for c in calls if FORWARD_FROM <= c["logged_at"] < now - lab.SETTLE]
    fwd_every = {p: trades(forward, caller, p) for p in ("horizon", "take_profit")}
    rules = []
    for key, r in RULES.items():
        pick = seasoned if key == "seasoned" else rebuilt_clean
        rules.append({"key": key, "name": r["name"], "since": FORWARD_FROM, "read_at": READ_AT,
                      "forward": summary(trades(forward, pick, r["exit"]), fwd_every[r["exit"]]),
                      "history": summary(trades(settled, pick, r["exit"]), every[r["exit"]])})

    bot = lambda c: c["source_kind"] == "bot"                                           # noqa: E731
    bot_rows = [{"band": label, "held": summary(trades(settled, lambda c, a=aged(lo, hi): bot(c) and a(c), "horizon"))}
                for lo, hi, label in AGES]
    all_bot = summary(trades(settled, bot, "horizon"))
    gated = summary(trades(settled, lambda c: bot(c) and (c["f"].get("age_h") or 0) >= 24, "horizon"))
    return {"since": lab.MEME_START, "settled_to": now - lab.SETTLE, "ages": ages, "checklists": checklists,
            "lines": lines, "rules": rules,
            "bot": {"bands": bot_rows, "all": all_bot, "gated": gated,
                    "note": "Paper only. Computed from the bot's buys we already record; nothing is sent to the bot."}}


def lab_flags() -> set[str]:
    from albatross.deepend.dossier import RED_FLAG_KEYS
    return set(RED_FLAG_KEYS)

