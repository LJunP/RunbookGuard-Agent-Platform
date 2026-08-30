package io.runbookguard.controlplane.messaging;

import io.runbookguard.controlplane.domain.RunStatus;

import java.time.Instant;

/**
 * contracts/events/README.md 定义的消息体。字段名与契约文档严格一致——Python 侧按同一份
 * 契约实现，任何一侧擅自改名都会在运行时表现为字段丢失而不是编译错误。
 */
public final class RunMessages {

    private RunMessages() {
    }

    public static final int SCHEMA_VERSION = 1;

    public record RunDispatch(
            int schemaVersion,
            String runId,
            String incidentId,
            String tenantId,
            String principalId,
            int sequence,
            String graphVersion,
            String promptVersion,
            String modelId,
            String datasetVersion,
            int maxSteps,
            Instant deadline,
            long costBudgetMicros,
            long tokenBudget,
            int toolCallBudget,
            String resumeFromCheckpointId) {

        public String idempotencyKey() {
            return "run.dispatch:" + runId + ":" + sequence;
        }
    }

    public record RunProgress(
            int schemaVersion,
            String runId,
            String tenantId,
            long fencingToken,
            int stepSequence,
            String nodeName,
            RunStatus status,
            String toolCallId,
            String inputArtifact,
            String outputArtifact,
            long costSpentMicros,
            long tokenSpent,
            int toolCallCount,
            int stepsUsed,
            Instant occurredAt) {

        public String idempotencyKey() {
            return "run.progress:" + runId + ":" + stepSequence;
        }
    }

    public record RunTerminal(
            int schemaVersion,
            String runId,
            String tenantId,
            long fencingToken,
            RunStatus terminalStatus,
            String failureClass,
            Instant occurredAt) {

        public String idempotencyKey() {
            // 终态的幂等键不含序号：同一 Run 的终态只有一个，重投必须命中同一个键。
            return "run.terminal:" + runId;
        }
    }
}
