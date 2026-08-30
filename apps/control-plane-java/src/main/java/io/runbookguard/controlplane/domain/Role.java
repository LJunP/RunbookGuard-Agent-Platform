package io.runbookguard.controlplane.domain;

/**
 * 最小权限：四个角色对应四类不同的信任。AGENT_RUNTIME 刻意不含 APPROVER —— Python 侧
 * 不能自己批准自己的请求（M0 INV-4）。
 */
public enum Role {
    /** 只读控制台访问。 */
    VIEWER,
    /** 创建 Incident 与 Run。 */
    OPERATOR,
    /** 批准或拒绝审批请求。 */
    APPROVER,
    /** Agent Runtime 使用：发起审批请求、上报步骤，但不能批准。 */
    AGENT_RUNTIME
}
