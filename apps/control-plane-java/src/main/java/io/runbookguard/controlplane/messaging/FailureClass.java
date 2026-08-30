package io.runbookguard.controlplane.messaging;

/** contracts/events/README.md 的 failureClass 枚举。自由文本会让 M6 的失败归因统计无法进行。 */
public enum FailureClass {
    MAX_STEPS_EXHAUSTED("max_steps_exhausted"),
    DEADLINE_EXCEEDED("deadline_exceeded"),
    COST_BUDGET_EXHAUSTED("cost_budget_exhausted"),
    TOKEN_BUDGET_EXHAUSTED("token_budget_exhausted"),
    TOOL_CALL_BUDGET_EXHAUSTED("tool_call_budget_exhausted"),
    REPEATED_STATE_DETECTED("repeated_state_detected"),
    AUTHORIZATION_FAILED("authorization_failed"),
    VERSION_INCOMPATIBLE("version_incompatible"),
    SAFETY_RULE_TRIGGERED("safety_rule_triggered"),
    INSUFFICIENT_EVIDENCE("insufficient_evidence"),
    PROVIDER_FAILURE("provider_failure"),
    TOOL_FAILURE("tool_failure"),
    CANCELLED("cancelled"),
    MAX_DELIVERY_EXCEEDED("max_delivery_exceeded"),
    INTERNAL_ERROR("internal_error");

    private final String wireValue;

    FailureClass(String wireValue) {
        this.wireValue = wireValue;
    }

    public String wireValue() {
        return wireValue;
    }

    public static FailureClass fromWire(String value) {
        for (FailureClass fc : values()) {
            if (fc.wireValue.equals(value)) {
                return fc;
            }
        }
        throw new IllegalArgumentException("unknown failureClass: " + value);
    }
}
