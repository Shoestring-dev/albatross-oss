"""Where the lab's strategies come from, besides the strategy researcher: the seeds (what we already run), random ones
from the same grammar, and mutations and crossovers of the best tested so far. Each is a config; a config is
canonicalised and hashed so the same strategy is never tested twice."""

import hashlib
import random

import orjson

from albatross.lab import factor, meme

STEP = {"score": 5, "n_red_flags": 1, "mcap": 25_000, "age_h": 6, "n_groups": 1, "n_calls": 2, "calls_24h": 2,
        "smart_holders": 1, "hour_utc": 2, "caller_prior_n": 5, "caller_prior_mean": 5, "caller_prior_win": 0.05}
START = {"score": (50, 95), "n_red_flags": (0, 2), "mcap": (10_000, 2_000_000), "age_h": (1, 72), "n_groups": (1, 5),
         "n_calls": (1, 30), "calls_24h": (1, 20), "smart_holders": (0, 5), "hour_utc": (0, 23),
         "caller_prior_n": (5, 50), "caller_prior_mean": (-40, 20), "caller_prior_win": (0.2, 0.6)}


def canonical(family: str, cfg) -> tuple[dict, str]:
    """(the config as stored, its hash): filters in a fixed order, weights rounded, defaults filled in."""
    d = cfg.model_dump(mode="json")
    if family == "meme":
        d["kinds"] = sorted(set(d["kinds"]))
        d["filters"] = sorted(d["filters"], key=lambda f: (f["field"], f["op"], str(f["value"])))
    return d, hashlib.sha256(orjson.dumps({"family": family, **d}, option=orjson.OPT_SORT_KEYS)).hexdigest()[:24]


def parse(family: str, d: dict):
    return (meme.MemeConfig if family == "meme" else factor.FactorConfig).model_validate(d)


# ─── seeds: what the paper books and factor rank already run ─────────────────
def seeds() -> list[tuple[str, object]]:
    m = meme.MemeConfig
    return [("meme", m()), ("meme", m(exit="take_profit_2x")),
            ("meme", m(filters=[{"field": "n_red_flags", "op": "<=", "value": 0}])),
            ("meme", m(filters=[{"field": "score", "op": ">=", "value": 85}])),
            ("meme", m(filters=[{"field": "score", "op": ">=", "value": 85}], exit="take_profit_2x")),
            ("meme", m(kinds=["fomo"])), ("meme", m(kinds=["bot"])),
            ("factor", factor.FactorConfig()), ("factor", factor.FactorConfig(side="long_only")),
            ("factor", factor.FactorConfig(side="long_hedged"))]


# ─── random strategies from the grammar ──────────────────────────────────────
def _random_filter(rng: random.Random) -> dict:
    roll = rng.random()
    if roll < 0.15:
        return {"field": f"check:{rng.choice(list(meme.RULES))}", "op": rng.choice(["==", "!="]), "value": rng.choice(meme.STATUSES)}
    if roll < 0.2:
        return {"field": "dex_paid", "op": "==", "value": rng.random() < 0.5}
    f = rng.choice(list(START))
    lo, hi = START[f]
    v = round(round(rng.uniform(lo, hi) / STEP[f]) * STEP[f], 4)
    return {"field": f, "op": rng.choice([">=", "<="]), "value": float(min(max(v, meme.NUMERIC[f][0]), meme.NUMERIC[f][1]))}


def _one_per_field(filters: list[dict]) -> list[dict]:
    """One filter per field and direction: a second (score >= 60 beside score >= 80) only repeats or undoes the first."""
    by: dict = {}
    for f in filters:
        by.setdefault((f["field"], f["op"]), f)
    return list(by.values())


def random_meme(rng: random.Random) -> meme.MemeConfig:
    kinds = rng.choice([["caller"], ["caller"], ["caller"], ["fomo"], ["caller", "fomo"]])
    return meme.MemeConfig(kinds=kinds, filters=_one_per_field([_random_filter(rng) for _ in range(rng.randint(1, 3))]),
                           exit=rng.choice(list(meme.EXITS)))


def random_factor(rng: random.Random) -> factor.FactorConfig:
    parts = rng.sample(factor.PARTS, rng.randint(1, len(factor.PARTS)))
    return factor.FactorConfig(weights={p: rng.choice([-1, -0.5, 0.5, 1, 1]) for p in parts},
                               vol_days=rng.choice([14, 20, 30, 60]), high_days=rng.choice([10, 20, 40]),
                               volume_days=rng.choice([14, 30, 60]), mom_days=rng.choice([5, 14, 30, 60]),
                               hold_days=rng.choice([3, 7, 14]), slice=rng.choice([3, 5, 10]),
                               side=rng.choice(["long_short", "long_only", "long_hedged"]))


# ─── mutations and crossovers of the best so far ─────────────────────────────
def mutate_meme(cfg: meme.MemeConfig, rng: random.Random) -> meme.MemeConfig:
    d = cfg.model_dump(mode="json")
    fs, roll = d["filters"], rng.random()
    numeric = [f for f in fs if f["field"] in STEP]
    if numeric and roll < 0.45:                                   # nudge a threshold
        f = rng.choice(numeric)
        lo, hi = meme.NUMERIC[f["field"]]
        f["value"] = float(round(min(max(f["value"] + rng.choice([-2, -1, 1, 2]) * STEP[f["field"]], lo), hi), 4))
    elif roll < 0.65 and len(fs) < 6:                             # add a filter
        d["filters"] = fs = _one_per_field(fs + [_random_filter(rng)])
    elif roll < 0.8 and fs:                                       # drop one
        fs.pop(rng.randrange(len(fs)))
    elif roll < 0.9:
        d["exit"] = "take_profit_2x" if d["exit"] == "hold_24h" else "hold_24h"
    else:
        d["kinds"] = rng.choice([["caller"], ["fomo"], ["caller", "fomo"], ["bot"]])
    return meme.MemeConfig.model_validate(d)


def cross_meme(a: meme.MemeConfig, b: meme.MemeConfig, rng: random.Random) -> meme.MemeConfig:
    by = {}
    for f in a.filters + b.filters:                               # one filter per field and direction
        by.setdefault((f.field, f.op), f)
    fs = list(by.values())
    rng.shuffle(fs)
    return meme.MemeConfig(kinds=sorted(set(a.kinds) | set(b.kinds)) if rng.random() < 0.3 else a.kinds,
                           filters=fs[:6], exit=rng.choice([a.exit, b.exit]))


def mutate_factor(cfg: factor.FactorConfig, rng: random.Random) -> factor.FactorConfig:
    d, roll = cfg.model_dump(mode="json"), rng.random()
    if roll < 0.4:                                                # move a weight
        p = rng.choice(factor.PARTS)
        d["weights"][p] = max(-1.0, min(1.0, round(d["weights"].get(p, 0) + rng.choice([-0.5, -0.25, 0.25, 0.5]), 2)))
    elif roll < 0.7:                                              # another lookback
        k, opts = rng.choice([("vol_days", [14, 20, 30, 60]), ("high_days", [10, 20, 40]),
                              ("volume_days", [14, 30, 60]), ("mom_days", [5, 14, 30, 60])])
        d[k] = rng.choice(opts)
    elif roll < 0.85:
        d["hold_days"] = rng.choice([3, 7, 14])
    elif roll < 0.95:
        d["slice"] = rng.choice([3, 5, 10])
    else:
        d["side"] = rng.choice([x for x in ("long_short", "long_only", "long_hedged") if x != d["side"]])
    if not any(d["weights"].values()):
        d["weights"]["calm"] = 1.0
    return factor.FactorConfig.model_validate(d)


def cross_factor(a: factor.FactorConfig, b: factor.FactorConfig, rng: random.Random) -> factor.FactorConfig:
    d = a.model_dump(mode="json")
    other = b.model_dump(mode="json")
    d["weights"] = {p: round((a.weights.get(p, 0) + b.weights.get(p, 0)) / 2, 2) for p in factor.PARTS}
    for k in ("vol_days", "high_days", "volume_days", "mom_days", "hold_days", "slice", "side"):
        if rng.random() < 0.5:
            d[k] = other[k]
    if not any(d["weights"].values()):
        d["weights"] = dict(a.weights)
    return factor.FactorConfig.model_validate(d)
