"""Replay Jev's correction against its graded record, to choose and re-check jev_calibration.HALF_LIFE_H.

Every STEP_S it refits each timeframe's map on what had been graded by then (weighted with a candidate half-life),
predicts the candidates scored in the next STEP_S, and scores those predictions: log loss, the overall level (said
vs won), and the average gap between said and won per 3-hour window. Read-only.

    docker compose exec ingest python -m albatross.agents.jev_replay [days]        # on the server, default 3 days

2026-10-01, the first 25 hours (51,965 graded). The 15m log loss was 0.2957 with no decay, 0.2948 at a 6 h half-life,
0.2942 at 3 h and 0.2940 at 1.5 h. Its predicted level went 9.8%, 9.4%, 8.8% and 8.3% against 8.2% that won. 5m was
flat throughout, at 0.2793-0.2811. On 1h a memory of 12-48 h was no better than none (0.1635-0.1646 against 0.1627),
and anything shorter had too few grades to fit: its outcomes arrive 6 hours late, so a short memory chases a regime
that has already passed. Predicting the recent hit rate and ignoring Jev was worse than the map on every timeframe,
so Jev's ranking carries information.
"""

import asyncio
import math
import sys
from datetime import timedelta

import numpy as np

from albatross import db
from albatross.agents import jev_calibration as jc

STEP_S = 600                # a refit every 10 minutes, as jev_grade does
WARMUP_S = 6 * 3600         # no predictions in the first 6 hours of the record
HALF_LIVES = [None, 48.0, 24.0, 12.0, 6.0, 3.0, 1.5]


def replay(rows: dict[str, np.ndarray], half_lives=HALF_LIVES, step_s=STEP_S, warmup_s=WARMUP_S) -> list[dict]:
    """rows: tf -> array of (p_win, hit, scored epoch s, graded epoch s). -> one result per timeframe and half-life.
    Every half-life predicts the same candidates: when its weighted record is under MIN_N, that step falls back to the
    whole record's map (counted in "fallback"), so the rows compare like with like."""
    out = []
    for tf, a in rows.items():
        p, hit, scored, graded = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
        b = np.floor(p / jc.WIDTH + 1e-9).astype(int)
        nb = int(b.max()) + 1

        def mapped(train, w, x):
            W, H = np.bincount(b[train], w, nb), np.bincount(b[train], w * hit[train], nb)
            bins = jc.fit([(round(i * jc.WIDTH, 4), W[i], H[i]) for i in range(nb) if W[i] > 1e-9])
            return [jc.apply(v, bins) for v in x]

        pred = {h: [] for h in half_lives}
        fell = dict.fromkeys(half_lives, 0)
        y, t = [], []
        for T in np.arange(scored.min() + warmup_s, scored.max(), step_s):
            test, train = (scored > T) & (scored <= T + step_s), graded <= T
            if not test.any() or train.sum() < jc.MIN_N:
                continue
            whole = mapped(train, np.ones(int(train.sum())), p[test])
            for h in half_lives:
                w = 0.5 ** np.minimum((T - scored[train]) / 3600 / h, 1000) if h else None
                if w is None or w.sum() < jc.MIN_N:
                    fell[h] += bool(h) * int(test.sum())
                    pred[h] += whole
                else:
                    pred[h] += mapped(train, w, p[test])
            y += list(hit[test])
            t += list(scored[test])
        if not y:
            continue
        y, t = np.array(y), np.array(t)
        windows = np.floor(t / 10800)
        for h in half_lives:
            pr = np.clip(np.array(pred[h]), 0.002, 0.998)
            gaps = [abs(pr[windows == k].mean() - y[windows == k].mean()) for k in np.unique(windows)]
            out.append({"tf": tf, "half_life_h": h, "n": len(y),
                        "logloss": round(float(-np.mean(y * np.log(pr) + (1 - y) * np.log(1 - pr))), 4),
                        "said": round(float(pr.mean()), 3), "won": round(float(y.mean()), 3),
                        "gap_3h": round(float(np.mean(gaps)), 3), "fallback": round(fell[h] / len(y), 2)})
    return out


async def main(days: float) -> None:
    pool = await db.pool()
    recs = await pool.fetch(
        """SELECT tf, p_win, (exit_reason = 'target')::int AS hit, extract(epoch FROM scored_at)::float8 AS scored,
                  extract(epoch FROM graded_at)::float8 AS graded
           FROM jev_scores WHERE outcome = 'graded' AND scored_at > now() - $1::interval""",
        timedelta(days=days))
    rows: dict[str, list] = {}
    for r in recs:
        rows.setdefault(r["tf"], []).append((r["p_win"], r["hit"], r["scored"], r["graded"]))
    res = replay({tf: np.array(v, dtype=float) for tf, v in rows.items() if len(v) >= jc.MIN_N})
    print(f"{'tf':<4} {'half-life':>9} {'n':>7} {'logloss':>8} {'said':>6} {'won':>6} {'gap/3h':>7} {'fallback':>8}   in use")
    for r in sorted(res, key=lambda r: (jc.ORDER.index(r["tf"]) if r["tf"] in jc.ORDER else 9,
                                       -(r["half_life_h"] or math.inf))):
        h = f"{r['half_life_h']:g} h" if r["half_life_h"] else "none"
        used = "<-" if r["half_life_h"] == jc.HALF_LIFE_H.get(r["tf"]) else ""
        print(f"{r['tf']:<4} {h:>9} {r['n']:>7} {r['logloss']:>8} {r['said']:>6} {r['won']:>6} {r['gap_3h']:>7} "
              f"{r['fallback']:>8.0%}   {used}")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 3))
