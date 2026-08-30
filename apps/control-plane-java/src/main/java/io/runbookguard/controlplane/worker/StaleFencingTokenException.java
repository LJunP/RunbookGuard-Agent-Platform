package io.runbookguard.controlplane.worker;

/**
 * Lease 已被接管，调用方持有的 token 已失效。典型场景：Worker 被 GC 停顿超过 Lease TTL，
 * 期间任务被接管，随后它恢复运行并继续上报——那些上报必须被拒绝。
 */
public class StaleFencingTokenException extends RuntimeException {
    public StaleFencingTokenException(String message) {
        super(message);
    }
}
