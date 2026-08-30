"""模型计价表。

单位一律 micros（1 micro = 1e-6 元），整数。理由与 ADR-0002 一致：浮点货币在跨语言
链路里会与「摘要必须逐字节一致」冲突，而且 0.0004 元这种量级用浮点累加会累积误差。

**按原价而非折扣价配置。** 促销是限时的；若按折扣价配置预算，促销结束后所有
cost budget 的实际约束力减半，而那时没人会记得改这个常量。高估费用是安全的方向。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    model: str
    prompt_micros_per_1k: int
    completion_micros_per_1k: int
    currency: str
    verified_at: str
    note: str = ""


# glm-5.3-flash：2026-08-27 由项目所有者提供的官方价目表核验。
# 页面列出 0.4 元 / 1.4 元（每百万 tokens），标注「5折限时两周」，原价 0.8 / 2.8。
# 这里用原价：0.8 元/1M = 0.0008 元/1k = 800 micros/1k。
_GLM_53_FLASH = ModelPricing(
    model="glm-5.3-flash",
    prompt_micros_per_1k=800,
    completion_micros_per_1k=2800,
    currency="CNY",
    verified_at="2026-08-27",
    note=(
        "按原价配置（0.8/2.8 元每百万 tokens）。当日实际为 5 折限时两周（0.4/1.4），"
        "促销结束后价格翻倍，按折扣价配置会让预算约束力静默减半。"
        "缓存命中价（0.115 元/1M）未建模——Adapter 无法从响应中区分缓存命中，"
        "因此所有 prompt token 均按未命中计价，这会高估成本。"
    ),
)

_TABLE: dict[str, ModelPricing] = {
    _GLM_53_FLASH.model: _GLM_53_FLASH,
}


def lookup(model: str) -> ModelPricing | None:
    """未知模型返回 None。调用方据此把成本记为 0 并标注「未知」，而不是猜一个价格。"""
    return _TABLE.get(model.strip().lower())


def known_models() -> tuple[str, ...]:
    return tuple(sorted(_TABLE))
