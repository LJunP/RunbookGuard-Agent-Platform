"""Provider 的抽象协议。

用 Protocol 而非 ABC：真实 Adapter 与 fake 之间没有实现共享，只有形状约定。
继承会诱使把逻辑塞进基类，而 fake 的价值恰恰在于它不共享真实实现的任何代码路径。
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from .models import ChatMessage, Completion, StreamItem


@runtime_checkable
class ChatProvider(Protocol):
    provider_id: str
    calls_made: int

    async def complete(self, messages: list[ChatMessage]) -> Completion:
        """非流式调用。失败一律抛 ProviderError 子类，不返回部分结果。"""
        ...

    def stream(self, messages: list[ChatMessage]) -> AsyncIterator[StreamItem]:
        """流式调用。未收到终止标记即断开的流必须抛错，不得把已收到的部分当结果。"""
        ...
