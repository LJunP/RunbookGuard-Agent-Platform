package io.runbookguard.controlplane.idempotency;

/**
 * 幂等键已被占位但结果尚未回填：另一个 Worker 正在处理。抛异常让消息重投——
 * 返回空结果会让调用方以为业务已完成。
 */
public class IdempotencyInProgressException extends RuntimeException {
    public IdempotencyInProgressException(String message) {
        super(message);
    }
}
