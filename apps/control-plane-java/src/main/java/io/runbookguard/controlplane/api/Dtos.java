package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.Approval;
import io.runbookguard.controlplane.domain.Incident;
import io.runbookguard.controlplane.domain.IncidentStatus;
import io.runbookguard.controlplane.domain.RunStatus;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

import java.time.Instant;
import java.util.Map;

/**
 * 跨语言契约的 Java 侧。字段名与 contracts/http/ 下的 schema 一致；DTO 与领域对象分开，
 * 避免内部字段（version、token hash）意外出现在响应里。
 */
public final class Dtos {

    private Dtos() {
    }

    public record CreateIncidentRequest(
            @NotBlank @Size(max = 64) String source,
            @NotBlank @Pattern(regexp = "P[1-4]") String severity,
            @NotBlank @Size(max = 512) String title,
            Instant startedAt) {
    }

    public record IncidentResponse(
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

        public static IncidentResponse from(Incident i) {
            return new IncidentResponse(i.incidentId(), i.tenantId(), i.source(), i.severity(),
                    i.title(), i.startedAt(), i.currentStatus(), i.version(), i.createdAt(),
                    i.updatedAt());
        }
    }

    public record UpdateIncidentStatusRequest(
            @NotNull IncidentStatus status,
            @Min(1) long expectedVersion) {
    }

    public record CreateRunRequest(
            @NotBlank String incidentId,
            @NotBlank @Size(max = 32) String graphVersion,
            @NotBlank @Size(max = 32) String promptVersion,
            @NotBlank @Size(max = 128) String modelId,
            @NotBlank @Size(max = 32) String datasetVersion) {
    }

    public record RunResponse(
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
            long version) {

        public static RunResponse from(AgentRun r) {
            return new RunResponse(r.runId(), r.incidentId(), r.tenantId(), r.principalId(),
                    r.graphVersion(), r.promptVersion(), r.modelId(), r.datasetVersion(),
                    r.status(), r.currentStep(), r.maxSteps(), r.deadline(),
                    r.costBudgetMicros(), r.costSpentMicros(), r.tokenBudget(), r.tokenSpent(),
                    r.toolCallBudget(), r.toolCallCount(), r.stepsUsed(), r.failureClass(),
                    r.version());
        }
    }

    public record AdvanceRunRequest(
            @NotNull RunStatus status,
            @Size(max = 64) String currentStep,
            @Min(1) long expectedVersion) {
    }

    public record SettleTerminalRequest(
            @NotNull RunStatus terminalStatus,
            @Size(max = 64) String failureClass) {
    }

    public record TerminalOutcomeResponse(RunStatus terminalStatus, boolean firstSettlement) {
    }

    public record RequestApprovalRequest(
            @NotBlank String runId,
            @NotBlank @Size(max = 128) String toolName,
            @NotBlank @Size(max = 256) String resourceRef,
            @NotNull Map<String, Object> arguments) {
    }

    public record ApprovalResponse(
            String approvalId,
            String runId,
            String tenantId,
            String requestedBy,
            String toolName,
            String resourceRef,
            String argumentsDigest,
            String digestAlg,
            String argumentsCanonical,
            String decision,
            String decidedBy,
            Instant expiresAt,
            Instant decidedAt,
            Instant consumedAt) {

        public static ApprovalResponse from(Approval a) {
            return new ApprovalResponse(a.approvalId(), a.runId(), a.tenantId(), a.principalId(),
                    a.toolName(), a.resourceRef(), a.argumentsDigest(), a.digestAlg(),
                    a.argumentsCanonical(), a.decision().name(), a.decidedBy(), a.expiresAt(),
                    a.decidedAt(), a.consumedAt());
        }
    }

    public record DecideApprovalRequest(
            @NotNull Boolean approve,
            @Size(max = 512) String reason) {
    }

    public record ConsumeApprovalRequest(
            @NotBlank @Size(max = 128) String toolName,
            @NotBlank @Size(max = 256) String resourceRef,
            @NotNull Map<String, Object> arguments) {
    }

    public record AuditEventResponse(
            Long eventId,
            String tenantId,
            String principalId,
            String action,
            String resourceType,
            String resourceId,
            String outcome,
            String reason,
            Instant occurredAt) {

        public static AuditEventResponse from(io.runbookguard.controlplane.audit.AuditEvent e) {
            return new AuditEventResponse(e.eventId(), e.tenantId(), e.principalId(), e.action(),
                    e.resourceType(), e.resourceId(), e.outcome(), e.reason(), e.occurredAt());
        }
    }

    public record PageQuery(
            @Min(1) @Max(100) int limit,
            @Min(0) int offset) {
    }
}
