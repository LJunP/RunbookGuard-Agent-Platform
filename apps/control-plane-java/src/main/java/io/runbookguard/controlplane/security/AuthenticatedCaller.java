package io.runbookguard.controlplane.security;

import io.runbookguard.controlplane.domain.Principal;

/**
 * 每个请求的已认证身份。tenantId 只从这里取，绝不从请求参数或请求体读取——
 * 客户端自报的 tenant 是不可信输入（威胁 T-4）。
 */
public record AuthenticatedCaller(Principal principal) {

    public String tenantId() {
        return principal.tenantId();
    }

    public String principalId() {
        return principal.principalId();
    }
}
