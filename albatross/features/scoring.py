"""Deterministic quant bias — the baseline every LLM agent has to beat.

Each component lands in [-1, 1]; the weighted sum is the continuous score, mapped to a
discrete -2..+2 bias. Confidence = how strongly the components agree with the final sign.
"""

import math

WEIGHTS = {"trend": 0.35, "structure": 0.25, "momentum": 0.25, "slope": 0.15}


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def components(f: dict) -> dict[str, float | None]:
    trend = {"bull": 1.0, "bear": -1.0}.get(f["ema_stack"])
    if trend is None and f.get("dist_ema50_pct") is not None:
        trend = 0.5 * math.copysign(1, f["dist_ema50_pct"])

    structure = {"up": 1.0, "down": -1.0, "range": 0.0}.get(f["structure"])

    momentum = None
    if f.get("rsi14") is not None and f.get("macd_hist") is not None:
        rsi_part = _clip((f["rsi14"] - 50) / 25)
        macd_part = 0.5 * math.copysign(1, f["macd_hist"]) + (0.5 if f.get("macd_hist_rising") else -0.5)
        momentum = _clip(0.5 * rsi_part + 0.5 * macd_part)

    slope = None
    if f.get("ema20_slope_pct") is not None and f.get("atr_pct"):
        # Normalise the 5-bar EMA slope by volatility so it means the same on 5m and 1w.
        slope = _clip(f["ema20_slope_pct"] / (2 * f["atr_pct"]))

    return {"trend": trend, "structure": structure, "momentum": momentum, "slope": slope}


def score(f: dict) -> tuple[float, int, float, dict]:
    """-> (score -1..1, bias -2..2, confidence 0..1, components)"""
    comps = components(f)
    present = {k: v for k, v in comps.items() if v is not None}
    if not present:
        return 0.0, 0, 0.0, comps
    w = sum(WEIGHTS[k] for k in present)
    s = sum(WEIGHTS[k] * v for k, v in present.items()) / w
    bias = max(-2, min(2, round(s * 2.5)))
    if bias == 0:
        confidence = 1 - abs(s) * 2.5  # confident it's neutral when the score sits near zero
    else:
        agree = sum(WEIGHTS[k] for k, v in present.items() if v * s > 0)
        confidence = agree / sum(WEIGHTS.values())
    return round(s, 4), bias, round(max(0.0, min(1.0, confidence)), 3), comps
