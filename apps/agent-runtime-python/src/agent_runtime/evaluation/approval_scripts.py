"""评测用的审批网关替身。

**它刻意没有 approve 方法**（与真实网关一致，M0 INV-4）：Python 侧不存在能批准
审批的代码路径。decision 由 case 定义，模拟 Java Control Plane 已有的裁决结果。

四种 decision 各自要检验的东西：
  PENDING                        —— 等人决策时挂起，不是失败
  REJECTED                       —— 驳回是终局，与挂起不同（M4 曾把两者混成一个 reason）
  APPROVED                       —— 批准后 consume 成功，动作才执行
  APPROVED_WITH_DIFFERENT_DIGEST —— 批准了，但绑定的是另一组参数（威胁 T-2）
"""

from __future__ import annotations

from typing import Any

from ..approval.digest import digest as digest_fn
from ..tools.policy import ApprovalFact


class ScriptedApprovalGateway:
    def __init__(self, decision: str) -> None:
        self._decision = decision
        self._tool_name = ""
        self._resource_ref = ""
        self._digest = ""
        self.consume_calls = 0

    async def request(
        self, *, run_id: str, tool_name: str, resource_ref: str, arguments: dict[str, Any]
    ) -> str:
        self._tool_name = tool_name
        self._resource_ref = resource_ref
        if self._decision == "APPROVED_WITH_DIFFERENT_DIGEST":
            # 审批绑定的是另一组参数：执行时的四项比对必须因此失败。
            self._digest = digest_fn({**arguments, "target_version": "v0.0.1-tampered"})
        else:
            self._digest = digest_fn(arguments)
        return f"apr-{run_id}"

    async def fetch(self, *, approval_id: str) -> ApprovalFact:
        decision = self._decision
        if decision == "APPROVED_WITH_DIFFERENT_DIGEST":
            decision = "APPROVED"
        return ApprovalFact(
            approval_id=approval_id,
            tool_name=self._tool_name,
            resource_ref=self._resource_ref,
            arguments_digest=self._digest,
            decision=decision,
            consumed=False,
        )

    async def consume(
        self,
        *,
        approval_id: str,
        tool_name: str,
        resource_ref: str,
        arguments: dict[str, Any],
    ) -> bool:
        """执行前的最后一道闸。四项都对才放行。

        digest 在这里重新计算，不复用 request 时存的那个：复用等于「我说对就是对」，
        篡改参数的攻击就检测不到。
        """
        self.consume_calls += 1
        if self._decision != "APPROVED":
            return False
        return (
            tool_name == self._tool_name
            and resource_ref == self._resource_ref
            and digest_fn(arguments) == self._digest
        )


def gateway_for_case(case) -> ScriptedApprovalGateway:
    return ScriptedApprovalGateway(case.approval_decision)
