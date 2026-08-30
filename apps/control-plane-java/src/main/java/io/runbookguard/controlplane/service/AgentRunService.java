package io.runbookguard.controlplane.service;

import io.runbookguard.controlplane.api.ForbiddenException;
import io.runbookguard.controlplane.api.RateLimitExceededException;
import io.runbookguard.controlplane.api.ResourceNotFoundException;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.config.RunbookGuardProperties;
import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.Incident;
import io.runbookguard.controlplane.domain.Role;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.persistence.AgentRunMapper;
import io.runbookguard.controlplane.persistence.RunTerminalStateMapper;
import io.runbookguard.controlplane.ratelimit.RedisRateLimiter;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

@Service
public class AgentRunService {

    private final AgentRunMapper runMapper;
    private final RunTerminalStateMapper terminalMapper;
    private final io.runbookguard.controlplane.persistence.WorkerLeaseMapper leaseMapper;
    private final IncidentService incidentService;
    private final AuditService audit;
    private final RedisRateLimiter rateLimiter;
    private final RunbookGuardProperties props;
    private final Clock clock;

    public AgentRunService(AgentRunMapper runMapper, RunTerminalStateMapper terminalMapper,
                           io.runbookguard.controlplane.persistence.WorkerLeaseMapper leaseMapper,
                           IncidentService incidentService, AuditService audit,
                           RedisRateLimiter rateLimiter, RunbookGuardProperties props, Clock clock) {
        this.runMapper = runMapper;
        this.terminalMapper = terminalMapper;
        this.leaseMapper = leaseMapper;
        this.incidentService = incidentService;
        this.audit = audit;
        this.rateLimiter = rateLimiter;
        this.props = props;
        this.clock = clock;
    }

    @Transactional
    public AgentRun create(AuthenticatedCaller caller, String incidentId, String graphVersion,
                           String promptVersion, String modelId, String datasetVersion) {
        requireAnyRole(caller, Role.OPERATOR, Role.AGENT_RUNTIME);
        if (!rateLimiter.tryAcquire("run:create:" + caller.principalId(),
                props.ratelimit().runCreatePerMinute(), Duration.ofMinutes(1))) {
            audit.denied(caller.tenantId(), caller.principalId(), "run.create",
                    "run", null, "rate limit exceeded");
            throw new RateLimitExceededException("run creation rate limit exceeded");
        }
        Incident incident = incidentService.get(caller, incidentId);

        Instant now = clock.instant();
        RunbookGuardProperties.RunDefaults d = props.runDefaults();
        AgentRun run = new AgentRun(
                "run-" + UUID.randomUUID(), incident.incidentId(), caller.tenantId(),
                caller.principalId(), graphVersion, promptVersion, modelId, datasetVersion,
                RunStatus.CREATED, null, d.maxSteps(), now.plus(d.wallClock()),
                d.costBudgetMicros(), 0L, d.tokenBudget(), 0L, d.toolCallBudget(), 0, 0,
                null, null, 0, 1, now, now);
        runMapper.insert(run);
        // 随 Run 一并建 Lease 占位行，使后续 acquire 是纯 UPDATE。并发 INSERT 同一主键会在
        // InnoDB 的插入意图锁上死锁，而并发 UPDATE 只是在行锁上排队。
        leaseMapper.insertPlaceholder(run.runId(), caller.tenantId());
        audit.allowed(caller.tenantId(), caller.principalId(), "run.create",
                "run", run.runId(), "{\"incidentId\":\"" + incidentId + "\"}");
        return run;
    }

    @Transactional(readOnly = true)
    public AgentRun get(AuthenticatedCaller caller, String runId) {
        AgentRun run = runMapper.findByIdInTenant(runId, caller.tenantId());
        if (run == null) {
            audit.denied(caller.tenantId(), caller.principalId(), "run.read",
                    "run", runId, "not found in tenant");
            throw new ResourceNotFoundException("run not found: " + runId);
        }
        return run;
    }

    @Transactional(readOnly = true)
    public List<AgentRun> listByIncident(AuthenticatedCaller caller, String incidentId) {
        incidentService.get(caller, incidentId);
        return runMapper.listByIncident(incidentId, caller.tenantId());
    }

    @Transactional
    public AgentRun advance(AuthenticatedCaller caller, String runId, RunStatus newStatus,
                            String currentStep, long expectedVersion) {
        requireAnyRole(caller, Role.OPERATOR, Role.AGENT_RUNTIME);
        AgentRun run = get(caller, runId);
        if (run.status().isTerminal()) {
            audit.denied(caller.tenantId(), caller.principalId(), "run.advance",
                    "run", runId, "run already terminal: " + run.status());
            throw new TerminalStateAlreadySetException(
                    "run " + runId + " already terminal: " + run.status());
        }
        if (newStatus.isTerminal()) {
            throw new IllegalArgumentException(
                    "terminal status must go through settleTerminal(): " + newStatus);
        }
        int updated = runMapper.updateStatusWithVersion(
                runId, caller.tenantId(), newStatus, currentStep, expectedVersion, clock.instant());
        if (updated == 0) {
            throw new OptimisticLockConflictException("run " + runId + " was modified concurrently");
        }
        return runMapper.findByIdInTenant(runId, caller.tenantId());
    }

    /**
     * 唯一终态（INV-5）。终态的唯一性由 run_terminal_state 主键裁决，不靠"先查有没有终态再写"——
     * 后者在两个 Worker 并发接管时会双写。第二次调用不是错误：它是重复投递的正常表现，
     * 返回既有终态即可，但必须留下审计痕迹以便区分"重投"与"真的有两个执行者在竞争"。
     */
    @Transactional
    public TerminalOutcome settleTerminal(AuthenticatedCaller caller, String runId,
                                          RunStatus terminalStatus, String failureClass) {
        requireAnyRole(caller, Role.OPERATOR, Role.AGENT_RUNTIME);
        if (!terminalStatus.isTerminal()) {
            throw new IllegalArgumentException("not a terminal status: " + terminalStatus);
        }
        get(caller, runId);
        runMapper.lockForSettlement(runId);

        try {
            terminalMapper.insert(runId, terminalStatus.name(), failureClass,
                    caller.principalId(), clock.instant());
        } catch (DuplicateKeyException e) {
            String existing = terminalMapper.findTerminalStatusFresh(runId);
            audit.record(caller.tenantId(), caller.principalId(), "run.settle_terminal",
                    "run", runId, io.runbookguard.controlplane.audit.AuditEvent.OUTCOME_DENIED,
                    "terminal state already set to " + existing, null, null);
            return new TerminalOutcome(RunStatus.valueOf(existing), false);
        }

        runMapper.applyTerminalStatus(runId, terminalStatus, failureClass, clock.instant());
        audit.allowed(caller.tenantId(), caller.principalId(), "run.settle_terminal",
                "run", runId, "{\"terminalStatus\":\"" + terminalStatus + "\"}");
        return new TerminalOutcome(terminalStatus, true);
    }

    public record TerminalOutcome(RunStatus terminalStatus, boolean firstSettlement) {
    }

    private void requireAnyRole(AuthenticatedCaller caller, Role... roles) {
        for (Role role : roles) {
            if (caller.principal().hasRole(role)) {
                return;
            }
        }
        audit.denied(caller.tenantId(), caller.principalId(), "rbac.check",
                "role", java.util.Arrays.toString(roles), "missing all of required roles");
        throw new ForbiddenException("principal lacks any of roles " + java.util.Arrays.toString(roles));
    }
}
