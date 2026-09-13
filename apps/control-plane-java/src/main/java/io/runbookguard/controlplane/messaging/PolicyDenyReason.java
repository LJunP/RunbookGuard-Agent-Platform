package io.runbookguard.controlplane.messaging;

/**
 * Policy 拒绝原因的封闭枚举，与 Python 侧
 * {@code agent_runtime.tools.policy.DenyReason} 逐值对应。
 *
 * <p>run_step.failure_code 的词汇表是**并集**：{@link FailureClass}
 * （终止/失败类）+ 本枚举（Policy 拒绝类）。两者是不同的事件——
 * "工具调用被 Policy 拦下"不是 Run 的失败，Trace 里必须分别可见。
 * 两侧各自有测试锁定取值，漂移会被摘要测试抓住。
 */
public enum PolicyDenyReason {
    UNKNOWN_TOOL("unknown_tool"),
    NOT_IN_ALLOWLIST("not_in_allowlist"),
    MISSING_BINDING("missing_binding"),
    TENANT_MISMATCH("tenant_mismatch"),
    RESOURCE_NOT_PERMITTED("resource_not_permitted"),
    ENVIRONMENT_NOT_ALLOWED("environment_not_allowed"),
    SCHEMA_VIOLATION("schema_violation"),
    NON_CANONICALIZABLE_ARGUMENTS("non_canonicalizable_arguments"),
    APPROVAL_REQUIRED_BUT_ABSENT("approval_required_but_absent"),
    APPROVAL_PENDING("approval_pending"),
    APPROVAL_NOT_GRANTED("approval_not_granted"),
    APPROVAL_TOOL_MISMATCH("approval_tool_mismatch"),
    APPROVAL_DIGEST_MISMATCH("approval_digest_mismatch"),
    TOOL_CALL_BUDGET_EXHAUSTED("tool_call_budget_exhausted"),
    WRITE_ACTION_IN_READ_ONLY_RUN("write_action_in_read_only_run");

    private final String wireValue;

    PolicyDenyReason(String wireValue) {
        this.wireValue = wireValue;
    }

    public String wireValue() {
        return wireValue;
    }

    public static PolicyDenyReason fromWire(String value) {
        for (PolicyDenyReason reason : values()) {
            if (reason.wireValue.equals(value)) {
                return reason;
            }
        }
        throw new IllegalArgumentException("unknown policyDenyReason: " + value);
    }
}
