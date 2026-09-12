"""Provider 层的数据模型。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Completion(BaseModel):
    """Provider 的统一返回。fake 与真实 Adapter 必须产出同构的实例。"""

    text: str
    usage: Usage
    model: str
    provider_id: str
    finish_reason: str
    attempts: int = 1
    cost_micros: int = 0


class StreamItem(BaseModel):
    """流式产出。中间项带 delta，最后一项带完整 Completion。"""

    delta: str = ""
    is_final: bool = False
    completion: Completion | None = None


@dataclass
class ProviderConfig:
    base_url: str
    model: str
    # 只从环境变量注入。不支持从配置文件、命令行、请求体读取。
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (0.5, 2.0)
    # 进程级防呆闸：一个循环 bug 不该把预算烧光。
    max_calls: int = 20
    max_cost_micros: int = 0  # 0 = 不限
    # 计价参数，单位 micros（1 micro = 1e-6 元）。见 pricing.py 的核验记录。
    # 默认 0 表示未知——不猜测价格，成本记为 0 并在报告中标注未知。
    price_per_1k_prompt_micros: int = 0
    price_per_1k_completion_micros: int = 0
    # response_format 的支持情况因网关而异，实现为可开关，默认关闭（ADR-0005 待核验项）。
    json_mode: bool = False
    provider_id: str = "openai-compatible"

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("base_url is required")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._fail_closed_on_unknown_pricing()

    def _fail_closed_on_unknown_pricing(self) -> None:
        """配了成本预算但模型不在计价表里 → 拒绝构造。

        这曾是 M8 报告 §6 列出的头号缺口：计价表查不到时 cost_micros 恒为 0，
        cost_budget_exceeded 这条终止条件**看起来在工作、实际永远不触发**——
        一个静默失效的安全机制比没有更糟。fail-closed 的意思是：
        要么把模型加进计价表，要么显式承认预算不可执行（escape hatch）。
        """
        if self.max_cost_micros <= 0 or self.pricing_known():
            return
        import os

        if os.environ.get("RUNBOOKGUARD_ALLOW_UNKNOWN_PRICING", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            return
        raise ValueError(
            f"cost budget is configured (max_cost_micros={self.max_cost_micros}) but "
            f"model {self.model!r} has no price entry, so cost would be recorded as 0 "
            "and the budget would never terminate a run. Add the model to "
            "provider/pricing.py, or set RUNBOOKGUARD_ALLOW_UNKNOWN_PRICING=1 "
            "to accept an unenforceable budget."
        )

    def __repr__(self) -> str:
        """api_key 绝不出现在 repr 中——repr 会进日志与异常信息。"""
        return (
            f"ProviderConfig(base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key=***, timeout_seconds={self.timeout_seconds}, "
            f"max_attempts={self.max_attempts}, max_calls={self.max_calls}, "
            f"json_mode={self.json_mode})"
        )

    __str__ = __repr__

    @classmethod
    def from_env(cls, *, prefix: str = "RUNBOOKGUARD_LLM_") -> ProviderConfig:
        def env(name: str, default: str = "") -> str:
            return os.environ.get(prefix + name, default)

        model = env("MODEL")
        # 计价从价目表按模型名查，不从环境变量传——价格是事实而非配置，
        # 让它可被环境覆盖等于允许悄悄把成本记成 0。
        from .pricing import lookup

        priced = lookup(model) if model else None
        return cls(
            base_url=env("BASE_URL"),
            model=model,
            api_key=env("API_KEY"),
            timeout_seconds=float(env("TIMEOUT_SECONDS", "60")),
            max_attempts=int(env("MAX_ATTEMPTS", "3")),
            max_calls=int(env("MAX_CALLS", "20")),
            max_cost_micros=int(env("MAX_COST_MICROS", "0")),
            json_mode=env("JSON_MODE", "false").lower() == "true",
            price_per_1k_prompt_micros=priced.prompt_micros_per_1k if priced else 0,
            price_per_1k_completion_micros=priced.completion_micros_per_1k if priced else 0,
        )

    def pricing_known(self) -> bool:
        """False 时 cost_micros 恒为 0——那是「未知」，不是「免费」。"""
        return bool(self.price_per_1k_prompt_micros or self.price_per_1k_completion_micros)
