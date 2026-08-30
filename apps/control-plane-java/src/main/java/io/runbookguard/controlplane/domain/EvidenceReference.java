package io.runbookguard.controlplane.domain;

import java.time.Instant;

/**
 * 一条证据引用。M0 §5 的 EvidenceReference。
 *
 * <p>contentHash 是引用能被反查的前提：控制台展示引用时要能回到原文并核对内容未变。
 * 缺了它，「有引用」只是一个说法。
 */
public record EvidenceReference(
        String evidenceId,
        String runId,
        String tenantId,
        String sourceType,
        String sourceIdentity,
        String version,
        String location,
        String contentHash,
        Instant capturedAt) {
}
