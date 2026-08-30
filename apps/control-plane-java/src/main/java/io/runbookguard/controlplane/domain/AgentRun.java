package io.runbookguard.controlplane.domain;

import java.time.Instant;

public record AgentRun(
        String runId,
        String incidentId,
        String tenantId,
        String principalId,
        String graphVersion,
        String promptVersion,
        String modelId,
        String datasetVersion,
        RunStatus status,
        String currentStep,
        int maxSteps,
        Instant deadline,
        long costBudgetMicros,
        long costSpentMicros,
        long tokenBudget,
        long tokenSpent,
        int toolCallBudget,
        int toolCallCount,
        int stepsUsed,
        String failureClass,
        Instant cancelRequestedAt,
        int dispatchSequence,
        long version,
        Instant createdAt,
        Instant updatedAt) {
}
