package io.runbookguard.controlplane.domain;

/** 与 M0 状态机的 Incident 生命周期对应。DIAGNOSING 之后不再回退到 OPEN。 */
public enum IncidentStatus {
    OPEN,
    DIAGNOSING,
    AWAITING_APPROVAL,
    MITIGATING,
    RESOLVED,
    CLOSED
}
