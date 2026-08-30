package io.runbookguard.controlplane.domain;

import java.time.Instant;

public record Incident(
        String incidentId,
        String tenantId,
        String source,
        String severity,
        String title,
        Instant startedAt,
        IncidentStatus currentStatus,
        long version,
        Instant createdAt,
        Instant updatedAt) {
}
