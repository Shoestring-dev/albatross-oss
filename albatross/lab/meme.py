"""Memecoin call strategies: which calls to take (filters on the call and on the dossier as it stood at the call) and
how to exit (hold 24 hours, or sell at 2x). A strategy is a config, so a code mutation or the researcher can write
one and nothing else; it is judged on the same trades the paper books count (backtest/books.py `take`: $100 a trade
after every cost, one position per coin), over any window of time.

Features per call, all known when the call came in (no look-ahead): the dossier's score, red flags and check
statuses at its last version before the call; its market cap, age, how many groups and calls it had; whether its
Dex listing was paid; the hour; and the caller's own record from its earlier calls whose 24 h were already graded."""

import bisect
from collections import defaultdict
from datetime import datetime
from typing import Literal

import orjson
from pydantic import BaseModel, Field, model_validator

from albatross import db
from albatross.backtest import books
from albatross.deepend.dossier import RULES

NUMERIC = {"score": (0, 100), "n_red_flags": (0, 11), "mcap": (0, 1e9), "age_h": (0, 24 * 90), "n_groups": (0, 50),
           "n_calls": (0, 500), "calls_24h": (0, 500), "smart_holders": (0, 100), "hour_utc": (0, 23),
           "caller_prior_n": (0, 1000), "caller_prior_mean": (-100, 1000), "caller_prior_win": (0, 1)}
BOOLEAN = {"dex_paid"}
CHECKS = tuple(f"check:{k}" for k in RULES)                  # check:<key> == good | warn | bad | unknown
STATUSES = ("good", "warn", "bad", "unknown")
EXITS = {"hold_24h": "horizon", "take_profit_2x": "take_profit"}
EXIT_WORD = {"hold_24h": "held 24 h", "take_profit_2x": "sold at 2x"}
WORD = {"score": "score", "n_red_flags": "red flags", "mcap": "market cap", "age_h": "age h", "n_groups": "groups",
        "n_calls": "calls", "calls_24h": "calls in 24 h", "smart_holders": "smart holders", "hour_utc": "hour UTC",
        "caller_prior_n": "caller's graded calls", "caller_prior_mean": "caller's average %",
        "caller_prior_win": "caller's win share", "dex_paid": "Dex paid"}


class Filter(BaseModel):
    field: str
    op: Literal[">=", "<=", "==", "!="]
    value: float | bool | str

    @model_validator(mode="after")
    def _known(self):
        if self.field in NUMERIC:
            lo, hi = NUMERIC[self.field]
            if isinstance(self.value, bool) or not isinstance(self.value, int | float) or not lo <= self.value <= hi:
                raise ValueError(f"{self.field} takes a number from {lo} to {hi}")
        elif self.field in BOOLEAN:
            if not isinstance(self.value, bool) or self.op not in ("==", "!="):
                raise ValueError(f"{self.field} is true or false, compared with == or !=")
        elif self.field in CHECKS:
            if self.value not in STATUSES or self.op not in ("==", "!="):
                raise ValueError(f"{self.field} is one of {', '.join(STATUSES)}, compared with == or !=")
        else:
            raise ValueError(f"unknown field {self.field}")
        return self


class MemeConfig(BaseModel):
    kinds: list[Literal["caller", "fomo", "bot"]] = Field(default_factory=lambda: ["caller"], min_length=1)
    filters: list[Filter] = Field(default_factory=list, max_length=6)
    exit: Literal["hold_24h", "take_profit_2x"] = "hold_24h"


def name(cfg: MemeConfig) -> str:
    who = {"caller": "group calls", "fomo": "FOMO buys", "bot": "the bot's buys"}
    parts = [" + ".join(who[k] for k in cfg.kinds)]
    for f in cfg.filters:
        w = WORD.get(f.field, f.field.replace("check:", "") + " check")
        v = (f.value if isinstance(f.value, bool | str) else f"{f.value:,.0f}" if abs(f.value) >= 1000
             else f"{f.value:g}")
        v = f"${v}" if f.field == "mcap" else v
        parts.append(f"{w} {f.op} {v}")
    return " · ".join(parts + [EXIT_WORD[cfg.exit]])


# ─── the data ────────────────────────────────────────────────────────────────
SQL = books.DEX_SQL.replace("h.at AS dossier_at, h.score, h.red_flags, h.statuses, s.symbol",
                            "h.at AS dossier_at, h.score, h.red_flags, h.statuses, h.evidence, s.symbol, c.source_id, "
                            "c.ref_price") \
    .replace("SELECT d.at, d.score, d.red_flags,", "SELECT d.at, d.score, d.red_flags, d.evidence,")


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def features(calls: list[dict]) -> None:
    """Each call's features (`f`), in place. The caller's record counts only its calls graded before this one."""
    graded: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for c in calls:
        state, net, at = books.outcome(c, "horizon")
        if state in ("closed", "stalled") and net is not None and at is not None and c["source_kind"] == "caller":
            graded[c["source_id"]].append((at, net))
    prior = {}
    for src, xs in graded.items():
        xs.sort()
        times, sums, wins = [t for t, _ in xs], [0.0], [0]
        for _, net in xs:
            sums.append(sums[-1] + net)
            wins.append(wins[-1] + (net > 0))
        prior[src] = (times, sums, wins)
    for c in calls:
        ev = c.get("evidence") or {}
        ev = ev if isinstance(ev, dict) else orjson.loads(ev)
        st = c.get("statuses") or {}
        st = st if isinstance(st, dict) else orjson.loads(st)
        f = {"kind": c["source_kind"], "score": _num(c.get("score")), "hour_utc": float(c["logged_at"].hour),
             "n_red_flags": float(len(c["red_flags"] or [])) if c.get("dossier_at") else None,
             "mcap": _num(ev.get("mcap")), "age_h": age / 3600 if (age := _num(ev.get("age_s"))) is not None else None,
             "n_groups": _num(ev.get("n_groups")), "n_calls": _num(ev.get("n_calls")),
             "calls_24h": _num(ev.get("calls_24h")), "smart_holders": _num(ev.get("smart_holders")),
             "dex_paid": ev.get("dex_paid") if isinstance(ev.get("dex_paid"), bool) else None}
        f.update({f"check:{k}": v for k, v in st.items()})
        if c["source_kind"] == "caller" and c.get("source_id") in prior:
            times, sums, wins = prior[c["source_id"]]
            k = bisect.bisect_right(times, c["logged_at"])
            f.update(caller_prior_n=float(k), caller_prior_mean=sums[k] / k if k else None,
                     caller_prior_win=wins[k] / k if k else None)
        else:
            f["caller_prior_n"] = 0.0 if c["source_kind"] == "caller" else None
        c["f"] = f


async def load() -> list[dict]:
    """Every DEX call with its outcomes and features, oldest first."""
    pool = await db.pool()
    calls = [dict(r) for r in await pool.fetch(SQL)]
    features(calls)
    return calls


# ─── a strategy over a window ────────────────────────────────────────────────
def _passes(f: dict, flt: Filter) -> bool:
    v = f.get(flt.field)
    if v is None:
        return False
    if flt.op == "==":
        return v == flt.value
    if flt.op == "!=":
        return v != flt.value
    return v >= flt.value if flt.op == ">=" else v <= flt.value


def picker(cfg: MemeConfig):
    kinds = set(cfg.kinds)
    return lambda c: c["source_kind"] in kinds and all(_passes(c["f"], flt) for flt in cfg.filters)


def trades(cfg: MemeConfig, calls: list[dict], start: datetime, end: datetime) -> list[tuple[float, str, object]]:
    """(net % a trade, its coin, its UTC day) for the settled trades whose call came in [start, end). The lab clusters
    on the day: the whole memecoin market moves together day to day, which made coin clusters about eight times too
    confident for a broad rule (review of 2026-10-09)."""
    window = [c for c in calls if start <= c["logged_at"] < end]
    took, _ = books.take(window, picker(cfg), EXITS[cfg.exit])
    return [(t["value"], t["coin"], t["at"].date()) for t in took if t["settled"] and t["value"] is not None]
