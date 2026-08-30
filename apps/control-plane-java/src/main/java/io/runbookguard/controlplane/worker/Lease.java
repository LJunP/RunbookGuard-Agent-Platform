package io.runbookguard.controlplane.worker;

import java.time.Instant;

public record Lease(
        String runId,
        String tenantId,
        String ownerId,
        long fencingToken,
        Instant acquiredAt,
        Instant expiresAt,
        Instant heartbeatAt) {
}
