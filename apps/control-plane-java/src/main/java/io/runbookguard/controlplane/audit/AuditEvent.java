package io.runbookguard.controlplane.audit;

import java.time.Instant;

public record AuditEvent(
        Long eventId,
        String tenantId,
        String principalId,
        String action,
        String resourceType,
        String resourceId,
        String outcome,
        String reason,
        String detailJson,
        String traceId,
        Instant occurredAt) {

    public static final String OUTCOME_ALLOWED = "ALLOWED";
    public static final String OUTCOME_DENIED = "DENIED";
    public static final String OUTCOME_FAILED = "FAILED";
}
