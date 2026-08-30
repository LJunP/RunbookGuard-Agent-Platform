package io.runbookguard.controlplane.service;

import io.runbookguard.controlplane.api.ForbiddenException;
import io.runbookguard.controlplane.api.RateLimitExceededException;
import io.runbookguard.controlplane.api.ResourceNotFoundException;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.config.RunbookGuardProperties;
import io.runbookguard.controlplane.domain.Incident;
import io.runbookguard.controlplane.domain.IncidentStatus;
import io.runbookguard.controlplane.domain.Role;
import io.runbookguard.controlplane.persistence.IncidentMapper;
import io.runbookguard.controlplane.ratelimit.RedisRateLimiter;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

@Service
public class IncidentService {

    private final IncidentMapper incidentMapper;
    private final AuditService audit;
    private final RedisRateLimiter rateLimiter;
    private final RunbookGuardProperties props;
    private final Clock clock;

    public IncidentService(IncidentMapper incidentMapper, AuditService audit,
                           RedisRateLimiter rateLimiter, RunbookGuardProperties props, Clock clock) {
        this.incidentMapper = incidentMapper;
        this.audit = audit;
        this.rateLimiter = rateLimiter;
        this.props = props;
        this.clock = clock;
    }

    @Transactional
    public Incident create(AuthenticatedCaller caller, String source, String severity,
                           String title, Instant startedAt) {
        requireRole(caller, Role.OPERATOR);
        if (!rateLimiter.tryAcquire("incident:create:" + caller.principalId(),
                props.ratelimit().incidentCreatePerMinute(), Duration.ofMinutes(1))) {
            audit.denied(caller.tenantId(), caller.principalId(), "incident.create",
                    "incident", null, "rate limit exceeded");
            throw new RateLimitExceededException("incident creation rate limit exceeded");
        }

        Instant now = clock.instant();
        Incident incident = new Incident(
                "inc-" + UUID.randomUUID(), caller.tenantId(), source, severity, title,
                startedAt == null ? now : startedAt, IncidentStatus.OPEN, 1, now, now);
        incidentMapper.insert(incident);
        audit.allowed(caller.tenantId(), caller.principalId(), "incident.create",
                "incident", incident.incidentId(), null);
        return incident;
    }

    @Transactional(readOnly = true)
    public Incident get(AuthenticatedCaller caller, String incidentId) {
        Incident incident = incidentMapper.findByIdInTenant(incidentId, caller.tenantId());
        if (incident == null) {
            // 跨租户访问在这里表现为 404 而非 403，同时记审计——审计里能看出是谁在探测。
            audit.denied(caller.tenantId(), caller.principalId(), "incident.read",
                    "incident", incidentId, "not found in tenant");
            throw new ResourceNotFoundException("incident not found: " + incidentId);
        }
        return incident;
    }

    @Transactional(readOnly = true)
    public List<Incident> list(AuthenticatedCaller caller, int limit, int offset) {
        return incidentMapper.listByTenant(caller.tenantId(),
                Math.min(Math.max(limit, 1), 100), Math.max(offset, 0));
    }

    /**
     * 乐观锁更新。调用方必须提供它读到的 version；不匹配则抛冲突，由客户端重读后重试。
     * 不在服务端自动重试——自动重试会掩盖并发冲突，而并发冲突往往说明有两个执行者在竞争。
     */
    @Transactional
    public Incident updateStatus(AuthenticatedCaller caller, String incidentId,
                                 IncidentStatus newStatus, long expectedVersion) {
        requireRole(caller, Role.OPERATOR);
        Incident current = get(caller, incidentId);
        int updated = incidentMapper.updateStatusWithVersion(
                incidentId, caller.tenantId(), newStatus, expectedVersion, clock.instant());
        if (updated == 0) {
            audit.denied(caller.tenantId(), caller.principalId(), "incident.update_status",
                    "incident", incidentId,
                    "version conflict: expected " + expectedVersion + ", actual " + current.version());
            throw new OptimisticLockConflictException(
                    "incident " + incidentId + " was modified concurrently");
        }
        audit.allowed(caller.tenantId(), caller.principalId(), "incident.update_status",
                "incident", incidentId, "{\"newStatus\":\"" + newStatus + "\"}");
        return incidentMapper.findByIdInTenant(incidentId, caller.tenantId());
    }

    private void requireRole(AuthenticatedCaller caller, Role role) {
        if (!caller.principal().hasRole(role)) {
            audit.denied(caller.tenantId(), caller.principalId(), "rbac.check",
                    "role", role.name(), "missing role " + role);
            throw new ForbiddenException("principal lacks role " + role);
        }
    }
}
