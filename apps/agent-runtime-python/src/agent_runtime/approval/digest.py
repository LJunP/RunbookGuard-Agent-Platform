"""跨语言 arguments_digest 测试向量的生成器与校验器。

M1 Gate 报告 §5.2 的遗留项：Java 与 Python 两侧必须对同一组参数算出同一个摘要，
否则审批时绑定的 digest 与执行时重算的 digest 不一致，威胁 T-2 的缓解直接失效
——表现为「合法请求被误拒」，或更糟，两个语义不同的参数拿到同一摘要。

这个文件同时是 Python 侧的实现与向量的权威来源。Java 侧读同一份 JSON 校验。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

DIGEST_ALG = "JCS-SHA256-V1"

MAX_SAFE_INTEGER = 9007199254740991
MAX_DEPTH = 8
MAX_CANONICAL_BYTES = 8 * 1024


class NonCanonicalizableArgumentError(ValueError):
    """规范化失败一律抛出，不允许「尽力处理」后继续——一个错误的摘要比一个被拒绝的
    请求危险得多。"""


def canonicalize(arguments: dict[str, Any]) -> str:
    """RFC 8785 JCS 的受限子集（ADR-0002）。

    是子集而非完整实现：浮点数被拒绝而不是按 ECMAScript Number::toString 序列化，
    因此不得对外声称实现了 RFC 8785。
    """
    if not isinstance(arguments, dict):
        raise NonCanonicalizableArgumentError(
            f"top level must be a JSON object, got {type(arguments).__name__}"
        )
    out: list[str] = []
    _write_object(arguments, out, 1)
    canonical = "".join(out)
    size = len(canonical.encode("utf-8"))
    if size > MAX_CANONICAL_BYTES:
        raise NonCanonicalizableArgumentError(
            f"canonical form exceeds {MAX_CANONICAL_BYTES} bytes: {size}"
        )
    return canonical


def digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonicalize(arguments).encode("utf-8")).hexdigest()


def _require_depth(depth: int) -> None:
    if depth > MAX_DEPTH:
        raise NonCanonicalizableArgumentError(f"nesting depth exceeds {MAX_DEPTH}")


def _write_object(node: dict[str, Any], out: list[str], depth: int) -> None:
    _require_depth(depth)
    keys: list[str] = []
    for raw_key in node:
        if not isinstance(raw_key, str):
            raise NonCanonicalizableArgumentError(
                f"object keys must be strings, got {type(raw_key).__name__}"
            )
        keys.append(raw_key)
    # Python dict 不可能有重复键，但 JSON 文本可以——解析入口需自行拒绝。
    # 排序按 UTF-16 码元序列，与 Java 的 String.compareTo 一致（RFC 8785 §3.2.3）。
    keys.sort(key=_utf16_sort_key)

    out.append("{")
    for index, key in enumerate(keys):
        if index:
            out.append(",")
        _write_string(key, out)
        out.append(":")
        _write_value(node[key], out, depth + 1)
    out.append("}")


def _utf16_sort_key(s: str) -> tuple[int, ...]:
    """UTF-16 码元序列。

    Python 的 str 比较按 Unicode 码点，Java 的按 UTF-16 码元。两者在 BMP 内一致，
    但对代理项之外的字符（U+10000 以上）会给出不同顺序——emoji 作为键名时两侧
    会排出不同的键序，摘要随之不同。
    """
    return tuple(s.encode("utf-16-be").hex(" ", 2).split())  # type: ignore[arg-type]


def _write_value(value: Any, out: list[str], depth: int) -> None:
    _require_depth(depth)
    if value is None:
        out.append("null")
    elif isinstance(value, str):
        _write_string(value, out)
    elif isinstance(value, bool):
        # bool 必须先于 int 判定：Python 里 isinstance(True, int) 为真。
        out.append("true" if value else "false")
    elif isinstance(value, int):
        _write_integer(value, out)
    elif isinstance(value, float):
        raise NonCanonicalizableArgumentError(
            f"non-integer numbers are rejected (ADR-0002); use integers or strings: {value}"
        )
    elif isinstance(value, dict):
        _write_object(value, out, depth)
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write_value(item, out, depth + 1)
        out.append("]")
    else:
        raise NonCanonicalizableArgumentError(
            f"unsupported value type: {type(value).__name__}"
        )


def _write_integer(value: int, out: list[str]) -> None:
    if value > MAX_SAFE_INTEGER or value < -MAX_SAFE_INTEGER:
        raise NonCanonicalizableArgumentError(
            f"integer outside +/-(2^53-1), not exactly representable as double: {value}"
        )
    out.append(str(value))


_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _write_string(s: str, out: list[str]) -> None:
    out.append('"')
    for ch in s:
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif ch < "\x20":
            out.append(f"\\u{ord(ch):04x}")
        elif "\ud800" <= ch <= "\udfff":
            # Python 的 str 可以持有未配对代理项（surrogatepass 等场景）。
            raise NonCanonicalizableArgumentError(
                f"unpaired surrogate U+{ord(ch):04X} in string"
            )
        else:
            # 非 ASCII 不转义，直接以 UTF-8 输出（RFC 8785）。
            out.append(ch)
    out.append('"')
