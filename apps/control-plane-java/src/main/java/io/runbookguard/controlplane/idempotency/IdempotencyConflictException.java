package io.runbookguard.controlplane.idempotency;

/**
 * 同一幂等键被用于不同内容的请求。这**不是**重复投递——返回首次结果会静默丢弃第二个请求，
 * 是幂等实现最常见的错误（ADR-0003 §4）。
 */
public class IdempotencyConflictException extends RuntimeException {
    public IdempotencyConflictException(String message) {
        super(message);
    }
}
