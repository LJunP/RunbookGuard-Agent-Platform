package io.runbookguard.controlplane.api;

/** 已认证但无权限。与 UnauthenticatedException 分开，因为一个是 401 一个是 403。 */
public class ForbiddenException extends RuntimeException {
    public ForbiddenException(String message) {
        super(message);
    }
}
