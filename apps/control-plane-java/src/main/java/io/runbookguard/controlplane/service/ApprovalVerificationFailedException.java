package io.runbookguard.controlplane.service;

/**
 * 执行前校验失败。抛异常而不是返回布尔值：调用方无法"忘记检查返回值"就继续执行。
 */
public class ApprovalVerificationFailedException extends RuntimeException {
    public ApprovalVerificationFailedException(String message) {
        super(message);
    }
}
