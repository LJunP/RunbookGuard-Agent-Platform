"""Runbook 数据模型与 chunk pipeline（ADR-0008 §5、§6）。

说明书 §16 要求 8 项元数据全部保留：「缺元数据就无法做引用校验，这是硬要求」。
因此实现为构造期强制，缺任一项抛异常，且不给默认值——尤其不给 document_version
默认值，那会让两个不同版本的文档看起来同版本。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

CHUNKER_VERSION = "section-aware-v1"
MAX_SECTION_CHARS = 1500


class SectionKind(str, Enum):
    """六段结构（说明书 §16）。顺序即文档中的出现顺序。"""

    SERVICE = "service"
    SYMPTOM = "symptom"
    PRECONDITION = "precondition"
    DIAGNOSIS = "diagnosis"
    SAFE_ACTION = "safe_action"
    ROLLBACK = "rollback"


REQUIRED_SECTIONS: tuple[SectionKind, ...] = tuple(SectionKind)


class RunbookError(ValueError):
    """Runbook 定义本身有问题。ingest 时抛出，不静默降级。"""


@dataclass(frozen=True)
class RunbookChunk:
    """一个 chunk = 一个段落（ADR-0008 §5）。

    不做滑动窗口 / overlap：段落是天然的语义边界，overlap 会让同一句话出现在两个
    chunk 里，引用指向哪一个都「对」，从而使引用失去精确性。
    """

    document_id: str
    document_version: str
    section_id: str
    section_kind: SectionKind
    service: str
    tenant_id: str
    content: str
    content_hash: str
    chunker_version: str
    embedding_model: str
    indexed_at: str

    def __post_init__(self) -> None:
        # 8 项必需元数据 + tenant + section_kind。缺一项就无法做引用校验。
        for name in (
            "document_id",
            "document_version",
            "section_id",
            "service",
            "tenant_id",
            "content",
            "content_hash",
            "chunker_version",
            "embedding_model",
            "indexed_at",
        ):
            if not getattr(self, name):
                raise RunbookError(
                    f"chunk metadata field {name!r} is required; "
                    "missing metadata makes citation verification impossible"
                )
        if len(self.content) > MAX_SECTION_CHARS:
            raise RunbookError(
                f"{self.section_id}: section exceeds {MAX_SECTION_CHARS} chars "
                f"({len(self.content)}); truncating silently would break content_hash"
            )
        expected = content_hash_of(self.content)
        if self.content_hash != expected:
            raise RunbookError(
                f"{self.section_id}: content_hash does not match content "
                f"(expected {expected[:16]}, got {self.content_hash[:16]})"
            )

    def citation(self) -> dict[str, str]:
        """引用三要素 + 定位信息。每条引用都必须能反查到原文。"""
        return {
            "document_id": self.document_id,
            "document_version": self.document_version,
            "section_id": self.section_id,
            "content_hash": self.content_hash,
        }

    def point_id(self) -> str:
        """向量库里的稳定主键。

        含 document_version：同一段落的不同版本是**不同**的 point，
        否则更新文档会覆盖旧版本，旧 Run 的引用就反查不到了（UJ5 的失败判据）。

        格式是 UUID：Qdrant 只接受无符号整数或 UUID 作为 point id，且会把提交的
        十六进制串规范化成带连字符的 UUID 形式再返回。不做这个转换的话，
        写入用的 key 与读回的 id 不同，本地 chunk 表就查不到了。
        """
        raw = f"{self.tenant_id}|{self.document_id}|{self.document_version}|{self.section_id}"
        digest = hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()
        return str(uuid.UUID(digest))


@dataclass(frozen=True)
class Runbook:
    document_id: str
    document_version: str
    service: str
    tenant_id: str
    title: str
    sections: dict[SectionKind, str]

    def __post_init__(self) -> None:
        if not self.document_id or not self.document_version:
            raise RunbookError("document_id and document_version are required")
        missing = [k.value for k in REQUIRED_SECTIONS if not self.sections.get(k)]
        if missing:
            raise RunbookError(
                f"{self.document_id}: missing required sections {missing}; "
                "the six-section structure is mandated by the spec"
            )

    def chunks(self, *, embedding_model: str, indexed_at: str | None = None) -> list[RunbookChunk]:
        stamp = indexed_at or datetime.now(UTC).replace(microsecond=0).isoformat()
        out: list[RunbookChunk] = []
        for kind in REQUIRED_SECTIONS:
            content = self.sections[kind].strip()
            out.append(
                RunbookChunk(
                    document_id=self.document_id,
                    document_version=self.document_version,
                    section_id=f"{self.document_id}#{kind.value}",
                    section_kind=kind,
                    service=self.service,
                    tenant_id=self.tenant_id,
                    content=content,
                    content_hash=content_hash_of(content),
                    chunker_version=CHUNKER_VERSION,
                    embedding_model=embedding_model,
                    indexed_at=stamp,
                )
            )
        return out


def content_hash_of(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


# -- Markdown 解析 ---------------------------------------------------------

_FRONT_MATTER = re.compile(r"\A---\n(?P<body>.*?)\n---\n", re.DOTALL)
_SECTION_HEADING = re.compile(r"^##\s+(?P<name>[a-z_]+)\s*$", re.MULTILINE)


def parse_runbook_markdown(text: str, *, default_tenant: str) -> Runbook:
    """解析 Runbook Markdown。

    格式刻意简单（frontmatter + 六个 `## section` 标题）：Runbook 是我们自建的，
    没必要支持任意 Markdown。解析器越简单，「文档写错了」就越容易在 ingest 时被发现，
    而不是变成一个语义奇怪的 chunk。
    """
    fm_match = _FRONT_MATTER.match(text)
    if not fm_match:
        raise RunbookError("runbook must start with a YAML front matter block")

    meta: dict[str, str] = {}
    for line in fm_match.group("body").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            raise RunbookError(f"malformed front matter line: {line!r}")
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')

    body = text[fm_match.end():]
    headings = list(_SECTION_HEADING.finditer(body))
    if not headings:
        raise RunbookError("runbook has no '## section' headings")

    sections: dict[SectionKind, str] = {}
    for index, match in enumerate(headings):
        name = match.group("name")
        try:
            kind = SectionKind(name)
        except ValueError as exc:
            raise RunbookError(
                f"unknown section {name!r}; allowed: {[k.value for k in SectionKind]}"
            ) from exc
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[kind] = body[match.end():end].strip()

    for required in ("id", "version", "service", "title"):
        if required not in meta:
            raise RunbookError(f"front matter is missing {required!r}")

    return Runbook(
        document_id=meta["id"],
        document_version=meta["version"],
        service=meta["service"],
        tenant_id=meta.get("tenant", default_tenant),
        title=meta["title"],
        sections=sections,
    )


def load_runbooks(directory: Path, *, default_tenant: str = "tenant-demo") -> list[Runbook]:
    books: list[Runbook] = []
    seen: dict[tuple[str, str], Path] = {}
    for path in sorted(directory.glob("*.md")):
        book = parse_runbook_markdown(path.read_text(encoding="utf-8"), default_tenant=default_tenant)
        key = (book.document_id, book.document_version)
        if key in seen:
            raise RunbookError(
                f"duplicate runbook {key} in {path} and {seen[key]}"
            )
        seen[key] = path
        books.append(book)
    if not books:
        raise RunbookError(f"no runbooks found in {directory}")
    return books
