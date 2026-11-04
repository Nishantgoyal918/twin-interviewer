"""Features → baseline-relative tags with confidence; low-confidence tags are dropped.

Never emits emotion labels, only measurable deviations (plan 7.5). Until the twin baseline
has `min_baseline_turns`, tags come from the within-session baseline with capped confidence.
"""

from __future__ import annotations

from app.models import Tag
from app.settings import get_settings
from app.signals import baseline

Z_MIN = 1.0


def make_tags(features: dict[str, float], twin_stats: dict, session_stats: dict) -> list[Tag]:
    cfg = get_settings().cfg
    min_turns = int(cfg("signals.min_baseline_turns", 20))
    cap = float(cfg("signals.cold_start_confidence_cap", 0.4))
    threshold = float(cfg("signals.action_confidence", 0.5))
    use_twin = baseline.n_turns(twin_stats) >= min_turns
    ref = twin_stats if use_twin else session_stats

    tags: list[Tag] = []
    for name, x in features.items():
        s = ref.get(name)
        z = baseline.zscore(s, x)
        if z is None or abs(z) < Z_MIN:
            continue
        conf = min(0.9, 0.3 + s["n"] / 50)
        if not use_twin:
            conf = min(conf, cap)
        tags.append(Tag(tag=name, value=f"{z:+.1f} sd vs baseline", z=round(z, 2),
                        confidence=round(conf, 2)))

    ttfv = features.get("ttfv_s")
    if ttfv is not None and ttfv >= 2.5:
        tags.append(Tag(tag="long_pause_before_answer", value=f"{ttfv:.1f}s", confidence=0.9))
    return [t for t in tags if t.confidence >= min(threshold, cap)]


def confident(tags: list[Tag]) -> list[Tag]:
    threshold = float(get_settings().cfg("signals.action_confidence", 0.5))
    return [t for t in tags if t.confidence >= threshold]
