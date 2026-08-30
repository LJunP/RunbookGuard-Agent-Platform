package io.runbookguard.controlplane.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.time.Duration;

@ConfigurationProperties(prefix = "runbookguard")
public record RunbookGuardProperties(
        ApprovalProps approval,
        RateLimitProps ratelimit,
        RunDefaults runDefaults,
        WorkerProps worker,
        ConsoleProps console) {

    public record ApprovalProps(Duration defaultTtl) {
    }

    /**
     * allowedOrigins 是显式清单而不是通配。带凭据的跨源请求配 * 时浏览器会拒绝，
     * 于是很容易被改成"回显请求的 Origin"绕过——那等于对任何站点开放。
     */
    public record ConsoleProps(java.util.List<String> allowedOrigins) {
    }

    public record RateLimitProps(int incidentCreatePerMinute, int runCreatePerMinute) {
    }

    public record WorkerProps(
            Duration leaseTtl,
            Duration heartbeatInterval,
            int maxDeliveryAttempts,
            Duration reclaimScanInterval) {
    }

    public record RunDefaults(
            int maxSteps,
            Duration wallClock,
            long costBudgetMicros,
            long tokenBudget,
            int toolCallBudget) {
    }
}
