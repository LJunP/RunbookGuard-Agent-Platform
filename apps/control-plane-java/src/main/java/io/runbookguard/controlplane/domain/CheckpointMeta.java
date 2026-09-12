package io.runbookguard.controlplane.domain;

/**
 * 一条 checkpoint 元数据。M0 §5 的 Checkpoint 实体。
 *
 * <p>控制面只存**元数据**（id、序号、摘要、位置），不存状态本体——
 * 状态在 Agent Runtime 的 checkpointer 里。stateDigest 让恢复前可以核对
 * 「读回来的状态就是写下去的那份」，而不是只看 id 存在。
 */
public record CheckpointMeta(
        String checkpointId,
        String runId,
        String tenantId,
        String graphVersion,
        String stateSchemaVersion,
        int sequence,
        String stateDigest,
        String stateLocation,
        java.time.Instant createdAt) {
}
