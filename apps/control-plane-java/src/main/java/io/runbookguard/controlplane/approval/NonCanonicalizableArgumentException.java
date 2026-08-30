package io.runbookguard.controlplane.approval;

/**
 * 规范化失败一律抛出，不允许"尽力处理"后继续——一个错误的摘要比一个被拒绝的请求危险得多。
 */
public class NonCanonicalizableArgumentException extends RuntimeException {

    public NonCanonicalizableArgumentException(String message) {
        super(message);
    }
}
