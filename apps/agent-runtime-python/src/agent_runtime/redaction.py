"""出口集中脱敏（威胁 T-3）的 Python 侧实现。

与 Java 侧 SecretRedactor 同源同规则。刻意重复实现而不共享：跨语言共享一段正则
会引入一个必须同步的隐式依赖，而两侧各有测试反而能互相印证规则是否一致。

已知局限：基于模式匹配，无法覆盖任意格式的凭据。它是纵深防御的一层，
真正的防线是 Secret 只从环境变量注入、不进入任何数据结构。
"""

from __future__ import annotations

import re

MASK = "***REDACTED***"

# 顺序重要：具体模式先跑，通用 key=value 最后。反过来会让
# "Authorization: Bearer xxx" 被通用模式匹配成 key=Authorization、value=Bearer，
# 只遮住 "Bearer" 而漏掉真正的 token。
_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), 0),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), 0),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"), 0),
    (re.compile(r"(?i)(jdbc:[^\s]*?password=)([^&\s]+)"), 2),
    (re.compile(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), 0),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|credential|authorization|bearer)\b\"?\s*[:=]\s*"
            r"\"?([^\"\s,;}]{4,})\"?"
        ),
        2,
    ),
)


def redact(text: str | None) -> str | None:
    if not text:
        return text
    out = text
    for pattern, value_group in _PATTERNS:
        if value_group == 0:
            out = pattern.sub(MASK, out)
        else:
            # 保留键名，只遮盖值，便于排查「哪个凭据被误传了」而不暴露值本身。
            out = pattern.sub(lambda m: m.group(0)[: m.start(value_group) - m.start(0)] + MASK, out)
    return out
