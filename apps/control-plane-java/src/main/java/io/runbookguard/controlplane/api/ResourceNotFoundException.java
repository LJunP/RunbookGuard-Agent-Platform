package io.runbookguard.controlplane.api;

/**
 * 资源不存在，或存在但不属于调用方租户。两种情况返回同一个错误是刻意的：
 * 区分它们会泄漏"某个 id 在别的租户下存在"这一信息（威胁 T-4）。
 */
public class ResourceNotFoundException extends RuntimeException {
    public ResourceNotFoundException(String message) {
        super(message);
    }
}
