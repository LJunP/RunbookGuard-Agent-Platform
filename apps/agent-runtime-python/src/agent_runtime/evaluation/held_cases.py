"""incidents-held 的加载器。

**held 与 dev 严格分离**（DEV_PROMPT §11）：held 只在正式冻结评测时使用一次，
开发期不看不调。因此这里只有加载与校验逻辑，case 内容在 JSON 里，
由 scripts/generate-incidents-held.py 生成后**未被查看**。

加载时做结构校验并在不合法时抛错，不静默跳过：一个被跳过的 case 会让
「30 个 case 全过」变成「28 个 case 全过」，而报告里看不出来。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .dev_cases import GraderSpec, IncidentCase, RunOverrides
from .model_scripts import DiagnosisScript
from ..schemas import ConclusionType

HELD_SCHEMA_VERSION = "1"


class HeldDatasetError(Exception):
    """held 数据集本身有问题。抛出而不降级——静默跳过会让 case 数量悄悄变少。"""


def default_dir() -> Path:
    # src/agent_runtime/evaluation/held_cases.py → 仓库根需要向上 5 层。
    return Path(__file__).resolve().parents[5] / "datasets" / "incidents-held"


def dataset_digest(directory: Path | None = None) -> str:
    """整个 held 数据集的摘要。冻结清单里记它，证明「用的是这一版」。

    读文件字节而不是加载后的对象：前者能覆盖任何格式变化，
    后者会漏掉「加载器忽略了的字段」这种变化。
    """
    directory = directory or default_dir()
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.json")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_held_cases(directory: Path | None = None) -> list[IncidentCase]:
    directory = directory or default_dir()
    if not directory.is_dir():
        raise HeldDatasetError(
            f"{directory} does not exist; run scripts/generate-incidents-held.py first"
        )
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise HeldDatasetError(f"{manifest_path} is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != HELD_SCHEMA_VERSION:
        raise HeldDatasetError(
            f"held schema version {manifest.get('schema_version')!r} != "
            f"{HELD_SCHEMA_VERSION!r}"
        )

    cases: list[IncidentCase] = []
    for path in sorted(directory.glob("case-*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        cases.append(_case_from_dict(raw, path))

    declared = manifest.get("case_count")
    if declared != len(cases):
        raise HeldDatasetError(
            f"manifest declares {declared} cases but {len(cases)} files were loaded"
        )
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids):
        raise HeldDatasetError("duplicate case_id in held dataset")
    return cases


def _case_from_dict(raw: dict, path: Path) -> IncidentCase:
    try:
        grader_raw = raw["grader"]
        grader = GraderSpec(
            required_evidence_sources=frozenset(grader_raw.get("required_evidence_sources", [])),
            required_conclusion_type=(
                ConclusionType(grader_raw["required_conclusion_type"])
                if grader_raw.get("required_conclusion_type")
                else None
            ),
            forbidden_tools=frozenset(grader_raw.get("forbidden_tools", [])),
            acceptable_terminal_states=frozenset(
                grader_raw.get("acceptable_terminal_states", ["COMPLETE"])
            ),
            expected_failure_class=grader_raw.get("expected_failure_class"),
            require_valid_citations=grader_raw.get("require_valid_citations", True),
            require_zero_actions=grader_raw.get("require_zero_actions", True),
            required_deny_reasons=frozenset(grader_raw.get("required_deny_reasons", [])),
            grade_semantics=grader_raw.get("grade_semantics", False),
            expected_root_cause_service=grader_raw.get("expected_root_cause_service"),
            expects_no_attribution=grader_raw.get("expects_no_attribution", False),
            required_proposal=grader_raw.get("required_proposal"),
            requires_conflict_declaration=grader_raw.get("requires_conflict_declaration", False),
            min_exclusions=grader_raw.get("min_exclusions", 0),
        )
        script_raw = raw.get("model_script")
        script = (
            DiagnosisScript(
                behaviour=script_raw.get("behaviour", "diagnose"),
                root_cause=script_raw.get("root_cause", "the evidence points at one cause"),
                root_cause_service=script_raw.get("root_cause_service"),
                exclusions=tuple(script_raw.get("exclusions", [])),
                proposed_tool=script_raw.get("proposed_tool"),
                proposed_arguments=dict(script_raw.get("proposed_arguments", {})),
                missing_evidence=tuple(script_raw.get("missing_evidence", [])),
                claim_count=script_raw.get("claim_count", 1),
            )
            if script_raw
            else None
        )
        overrides_raw = raw.get("overrides") or {}
        overrides = RunOverrides(
            max_steps=overrides_raw.get("max_steps"),
            tool_call_budget=overrides_raw.get("tool_call_budget"),
            cost_budget_micros=overrides_raw.get("cost_budget_micros"),
            token_budget=overrides_raw.get("token_budget"),
            deadline_expired=overrides_raw.get("deadline_expired", False),
            safety_rule_hit=overrides_raw.get("safety_rule_hit", False),
            version_incompatible=overrides_raw.get("version_incompatible", False),
            provider_failure=overrides_raw.get("provider_failure"),
        )
        return IncidentCase(
            case_id=raw["case_id"],
            category=raw["category"],
            title=raw["title"],
            scenario_id=raw.get("scenario_id"),
            incident_summary=raw["incident_summary"],
            service=raw["service"],
            allowed_tools=frozenset(raw["allowed_tools"]),
            tool_plan=tuple((name, dict(args)) for name, args in raw["tool_plan"]),
            grader=grader,
            model_script=script,
            overrides=overrides,
            needs_approval_gateway=raw.get("needs_approval_gateway", False),
            approval_decision=raw.get("approval_decision", "PENDING"),
            execution_mode=raw.get("execution_mode", "loop"),
            expects_failure=raw.get("expects_failure", False),
            notes=raw.get("notes", ""),
        )
    except (KeyError, ValueError) as exc:
        raise HeldDatasetError(f"{path.name}: {exc}") from exc
