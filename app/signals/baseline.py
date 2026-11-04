"""Running mean/std per feature (Welford). Stats shape: {feature: {"n", "mean", "m2"}}."""

from __future__ import annotations

import math


def update(stats: dict[str, dict[str, float]], features: dict[str, float]) -> dict:
    out = {k: dict(v) for k, v in stats.items()}
    for name, x in features.items():
        if x is None or isinstance(x, bool) or not isinstance(x, int | float) or math.isnan(x):
            continue
        s = out.setdefault(name, {"n": 0.0, "mean": 0.0, "m2": 0.0})
        s["n"] += 1
        delta = x - s["mean"]
        s["mean"] += delta / s["n"]
        s["m2"] += delta * (x - s["mean"])
    return out


def std(s: dict[str, float]) -> float:
    if s["n"] < 2:
        return 0.0
    return math.sqrt(s["m2"] / (s["n"] - 1))


def zscore(s: dict[str, float] | None, x: float) -> float | None:
    if not s or s["n"] < 3:
        return None
    sd = std(s)
    if sd < 1e-9:
        return 0.0
    return (x - s["mean"]) / sd


def n_turns(stats: dict[str, dict[str, float]]) -> int:
    return int(max((s["n"] for s in stats.values()), default=0))
