"""结构化输出解析（ADR-0005 §3）。

DEV_PROMPT 要求「模型返回不合法 JSON 时走 typed failure，不许静默修补后当成功」。

允许剥离 markdown 代码围栏，但必须标记 unwrapped=True——关键词是「不许**静默**修补」，
被记录、可断言、会进 Trace 的解包不是静默修补。

明确不做的修补：不补全引号括号、不猜测截断的 JSON、不换引号、不删尾随逗号、
不从散文里找 JSON、schema 不匹配时不填默认值。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .errors import MalformedJsonError, SchemaViolationError

T = TypeVar("T", bound=BaseModel)

# 整个文本必须是一个代码围栏，不接受「散文中夹着围栏」。
_FENCE = re.compile(r"\A\s*```[a-zA-Z0-9_-]*\s*\n(?P<body>.*?)\n?\s*```\s*\Z", re.DOTALL)


@dataclass(frozen=True)
class ParsedOutput[M: BaseModel]:
    value: M
    unwrapped: bool
    raw_text: str


def parse_structured(text: str, model: type[T]) -> ParsedOutput[T]:
    if not text or not text.strip():
        raise MalformedJsonError("model returned empty text")

    candidate = text
    unwrapped = False
    fence_match = _FENCE.match(text)
    if fence_match:
        candidate = fence_match.group("body")
        unwrapped = True

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise MalformedJsonError(
            f"model output is not valid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}"
        ) from exc

    if not isinstance(payload, dict):
        raise MalformedJsonError(
            f"expected a JSON object at top level, got {type(payload).__name__}"
        )

    try:
        # strict=True 关掉隐式类型转换（"1" 不会变成 1，1 不会变成 True）——
        # 那类转换会让「模型返回了错类型」变成静默成功。
        #
        # 但枚举字段是例外：JSON 里的枚举只能是字符串，strict 模式不会把它转成
        # 枚举成员。因此对声明了枚举的字段先做一次显式转换，取值仍受枚举限制，
        # 只是接受它的字面形式。这不是放松校验。
        payload = _coerce_declared_enums(payload, model)
        value = model.model_validate(payload, strict=True)
    except ValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        raise SchemaViolationError(
            f"model output violates {model.__name__} schema at: {fields}"
        ) from exc

    return ParsedOutput(value=value, unwrapped=unwrapped, raw_text=text)


def _coerce_declared_enums(payload: dict, model: type[BaseModel]) -> dict:
    """把顶层字段里声明为枚举的字符串转成枚举成员。

    只处理**声明了枚举类型**的字段：未声明的字段不动，因此这不会掩盖
    「模型返回了一个不该有的字段」或「返回了错类型」。
    """
    import enum

    coerced = dict(payload)
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if not (isinstance(annotation, type) and issubclass(annotation, enum.Enum)):
            continue
        raw = coerced.get(name)
        if isinstance(raw, str):
            try:
                coerced[name] = annotation(raw)
            except ValueError:
                # 不是合法取值：留给 Pydantic 报错，错误信息里会指出字段名。
                pass
    return coerced
