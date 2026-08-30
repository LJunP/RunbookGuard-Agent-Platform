package io.runbookguard.controlplane.domain;

import java.time.Instant;

public record Approval(
        String approvalId,
        String runId,
        String tenantId,
        String principalId,
        String toolName,
        String resourceRef,
        String argumentsDigest,
        String digestAlg,
        String argumentsCanonical,
        ApprovalDecision decision,
        String decidedBy,
        Instant expiresAt,
        Instant decidedAt,
        Instant consumedAt,
        long version,
        Instant createdAt) {

    public boolean isConsumed() {
        return consumedAt != null;
    }

    public boolean isExpiredAt(Instant now) {
        return now.isAfter(expiresAt);
    }
}
