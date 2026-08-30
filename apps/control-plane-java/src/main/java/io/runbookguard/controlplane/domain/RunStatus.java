package io.runbookguard.controlplane.domain;

import java.util.EnumSet;
import java.util.Set;

/**
 * M0 §6 的 Agent 状态机。终态判定集中在这里，避免各处散落 "status == COMPLETE || ..." 的判断
 * 导致新增终态时漏改。
 */
public enum RunStatus {
    CREATED,
    COLLECT_CONTEXT,
    RETRIEVE_RUNBOOK,
    FORM_HYPOTHESES,
    SELECT_TOOL,
    POLICY_CHECK,
    AWAITING_APPROVAL,
    EXECUTING_TOOL,
    OBSERVE,
    VERIFY,
    PROPOSE_ACTION,
    BLOCKED,
    COMPLETE,
    FAILED;

    private static final Set<RunStatus> TERMINAL = EnumSet.of(BLOCKED, COMPLETE, FAILED);

    public boolean isTerminal() {
        return TERMINAL.contains(this);
    }
}
