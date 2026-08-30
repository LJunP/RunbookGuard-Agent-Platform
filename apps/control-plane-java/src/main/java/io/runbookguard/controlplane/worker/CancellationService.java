package io.runbookguard.controlplane.worker;

import io.runbookguard.controlplane.api.ForbiddenException;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.Role;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.messaging.FailureClass;
import io.runbookguard.controlplane.persistence.AgentRunMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Instant;

/**
 * 取消走数据库标记而非消息（ADR-0003 §5）：消息可能到达已不持有 Lease 的 Worker，
 * 或在 Worker 重启后丢失。标记是持久的，接管者能立即看到。
 *
 * <p>代价是取消不即时，粒度为一个节点。这与 ADR-0001 C-6 的协作式取消语义一致。
 */
@Service
public class CancellationService {

    private final AgentRunMapper runMapper;
    private final AgentRunService runService;
    private final AuditService audit;
    private final Clock clock;

    public CancellationService(AgentRunMapper runMapper, AgentRunService runService,
                               AuditService audit, Clock clock) {
        this.runMapper = runMapper;
        this.runService = runService;
        this.audit = audit;
        this.clock = clock;
    }

    /** @return false 表示已经请求过取消（幂等） */
    @Transactional
    public boolean requestCancel(AuthenticatedCaller caller, String runId) {
        if (!caller.principal().hasRole(Role.OPERATOR)) {
            audit.denied(caller.tenantId(), caller.principalId(), "run.cancel",
                    "run", runId, "missing role OPERATOR");
            throw new ForbiddenException("principal lacks role OPERATOR");
        }
        AgentRun run = runService.get(caller, runId);
        if (run.status().isTerminal()) {
            audit.denied(caller.tenantId(), caller.principalId(), "run.cancel",
                    "run", runId, "run already terminal: " + run.status());
            return false;
        }
        int updated = runMapper.requestCancel(runId, caller.tenantId(),
                caller.principalId(), clock.instant());
        if (updated == 0) {
            return false;
        }
        audit.allowed(caller.tenantId(), caller.principalId(), "run.cancel", "run", runId, null);
        return true;
    }

    @Transactional(readOnly = true)
    public boolean isCancelRequested(String runId, String tenantId) {
        return runMapper.findCancelRequestedAt(runId, tenantId) != null;
    }

    /**
     * Worker 在节点边界调用。观察到取消标记后由 Worker 主动结算终态，而不是由取消请求方
     * 直接写终态——后者会绕过 Lease 与 fencing token，可能与正在执行的 Worker 竞争。
     */
    @Transactional
    public AgentRunService.TerminalOutcome settleCancelled(AuthenticatedCaller caller, String runId) {
        Instant requestedAt = runMapper.findCancelRequestedAt(runId, caller.tenantId());
        if (requestedAt == null) {
            throw new IllegalStateException("no cancel requested for run " + runId);
        }
        return runService.settleTerminal(caller, runId, RunStatus.FAILED,
                FailureClass.CANCELLED.wireValue());
    }
}
