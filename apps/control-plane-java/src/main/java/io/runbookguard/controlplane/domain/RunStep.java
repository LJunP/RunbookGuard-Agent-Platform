package io.runbookguard.controlplane.domain;

import java.time.Instant;

/**
 * 一个已执行的步骤。M0 §5 的 RunStep。
 *
 * <p>failureClass 取自 contracts/events/README.md 的封闭枚举，不是自由文本：
 * M6 要按失败原因聚合统计，自由文本无法聚合。
 */
public record RunStep(
        String stepId,
        String runId,
        int sequence,
        String nodeName,
        String inputArtifact,
        String outputArtifact,
        String toolCallId,
        String status,
        String failureClass,
        Instant startedAt,
        Instant finishedAt) {
}
