"""Provider 装配。

fake 是**默认**：CI、开发、评测的确定性路径都走它。真实 Provider 需要显式
设置 RUNBOOKGUARD_LLM_PROVIDER=openai-compatible 才会启用——默认走真实调用会让
一次误操作烧掉预算，也会让 CI 变得不确定。
"""

from __future__ import annotations

import os

from .base import ChatProvider
from .fake import FakeProvider
from .models import ProviderConfig
from .openai_compatible import OpenAICompatibleProvider


class ProviderConfigurationError(RuntimeError):
    """配置不足以构造请求的 Provider。不静默回退到 fake——那会让「以为在调真实模型」
    的验证变成自欺。"""


def build_provider(*, kind: str | None = None) -> ChatProvider:
    resolved = (kind or os.environ.get("RUNBOOKGUARD_LLM_PROVIDER", "fake")).strip().lower()

    if resolved == "fake":
        return FakeProvider()

    if resolved in {"openai-compatible", "openai", "real"}:
        # 先检查环境变量再构造 ProviderConfig：后者的 __post_init__ 会因 base_url 为空
        # 抛 ValueError，那个错误说不出「缺的是哪几个变量」。
        missing = [
            name
            for name in (
                "RUNBOOKGUARD_LLM_BASE_URL",
                "RUNBOOKGUARD_LLM_MODEL",
                "RUNBOOKGUARD_LLM_API_KEY",
            )
            if not os.environ.get(name)
        ]
        if missing:
            raise ProviderConfigurationError(
                "real provider requested but these variables are unset: " + ", ".join(missing)
            )
        return OpenAICompatibleProvider(ProviderConfig.from_env())

    raise ProviderConfigurationError(f"unknown provider kind: {resolved!r}")
