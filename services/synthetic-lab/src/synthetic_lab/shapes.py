"""信号形态。

词表是刻意封闭的（ADR-0004 §1）：无限的表达能力会让「连续 3 次一致」无法保证。
需要新形态时扩这个模块，而不是让剧本 YAML 里写逻辑。
"""

from __future__ import annotations

from .determinism import jitter

FLAT = "flat"
RAMP = "ramp"
STEP = "step"
SPIKE = "spike"
SAWTOOTH = "sawtooth"

SHAPES = {FLAT, RAMP, STEP, SPIKE, SAWTOOTH}


def value_at(
    shape: str,
    baseline: float,
    incident: float,
    progress: float,
    *,
    seed: int,
    coords: tuple,
    noise_ratio: float = 0.03,
    sawtooth_periods: int = 6,
) -> float:
    """按形态计算某个进度点的值。

    progress < 0 表示 T0 之前（基线期），0..1 表示故障期内的相对位置。
    """
    if shape not in SHAPES:
        raise ValueError(f"unknown shape: {shape}")

    if progress < 0.0:
        raw = baseline
    elif shape == FLAT:
        raw = baseline
    elif shape == RAMP:
        raw = baseline + (incident - baseline) * progress
    elif shape == STEP:
        raw = incident
    elif shape == SPIKE:
        # 在故障期中段达到峰值后回落，用于「一次性尖刺」类信号
        raw = baseline + (incident - baseline) * (1.0 - abs(progress * 2.0 - 1.0))
    else:  # SAWTOOTH
        # 锯齿：反复爬升到 incident 后被重置回 baseline。OOMKilled 的内存曲线。
        phase = (progress * sawtooth_periods) % 1.0
        raw = baseline + (incident - baseline) * phase

    noisy = raw + jitter(seed, *coords, amplitude=abs(raw) * noise_ratio)
    return max(0.0, noisy)
