"""跨语言摘要向量：Python 侧校验。

Java 侧读同一份 JSON（DigestCrossLanguageVectorTest）。两套独立实现 + 同一份向量，
比共享一段规范化代码更能印证规则一致——共享会引入必须同步的隐式依赖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_runtime.approval.digest import (
    DIGEST_ALG,
    NonCanonicalizableArgumentError,
    canonicalize,
    digest,
)

VECTORS_PATH = Path(__file__).resolve().parents[3] / "contracts" / "tools" / "digest-test-vectors.json"


def load_vectors() -> dict:
    assert VECTORS_PATH.exists(), f"缺少跨语言测试向量：{VECTORS_PATH}"
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


VECTORS = load_vectors()


def test_algorithm_identifier_matches() -> None:
    assert VECTORS["digest_alg"] == DIGEST_ALG


def test_vector_counts_are_meaningful() -> None:
    assert len(VECTORS["accept"]) >= 15
    assert len(VECTORS["reject"]) >= 6


@pytest.mark.parametrize(
    "vector", VECTORS["accept"], ids=[v["name"] for v in VECTORS["accept"]]
)
def test_accept_vectors(vector: dict) -> None:
    args = vector["arguments"]
    assert canonicalize(args) == vector["canonical"], f"canonical mismatch: {vector['name']}"
    assert digest(args) == vector["digest"], f"digest mismatch: {vector['name']}"


@pytest.mark.parametrize(
    "vector", VECTORS["reject"], ids=[v["name"] for v in VECTORS["reject"]]
)
def test_reject_vectors(vector: dict) -> None:
    with pytest.raises(NonCanonicalizableArgumentError):
        digest(vector["arguments"])


def test_key_order_vectors_agree() -> None:
    by_name = {v["name"]: v for v in VECTORS["accept"]}
    assert digest(by_name["basic"]["arguments"]) == digest(
        by_name["key-order-differs"]["arguments"]
    )


def test_explicit_null_is_not_folded() -> None:
    by_name = {v["name"]: v for v in VECTORS["accept"]}
    assert digest(by_name["explicit-null"]["arguments"]) != digest(
        by_name["empty-object"]["arguments"]
    )


def test_bool_is_not_treated_as_integer() -> None:
    """Python 里 isinstance(True, int) 为真。若 bool 判定晚于 int，True 会被
    序列化成 1，与 Java 侧的 true 不一致。"""
    assert canonicalize({"flag": True}) == '{"flag":true}'
    assert canonicalize({"flag": False}) == '{"flag":false}'
    assert digest({"flag": True}) != digest({"flag": 1})


def test_unpaired_surrogate_is_rejected() -> None:
    assert "\ud800" not in "ok"
    with pytest.raises(NonCanonicalizableArgumentError):
        digest({"k": "a\ud800b"})


def test_non_string_key_is_rejected() -> None:
    with pytest.raises(NonCanonicalizableArgumentError):
        digest({1: "x"})


def test_unsupported_type_is_rejected() -> None:
    with pytest.raises(NonCanonicalizableArgumentError):
        digest({"when": object()})
