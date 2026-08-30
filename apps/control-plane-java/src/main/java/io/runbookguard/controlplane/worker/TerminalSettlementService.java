package io.runbookguard.controlplane.worker;

import io.runbookguard.controlplane.audit.AuditEvent;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.messaging.FailureClass;
import io.runbookguard.controlplane.persistence.AgentRunMapper;
import io.runbookguard.controlplane.persistence.RunTerminalStateMapper;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;

/**
 * 无调用方身份的终态结算，供后台任务（Lease 回收）使用。与 AgentRunService.settleTerminal
 * 共用同一个数据库约束，因此两条路径的并发结算仍只有一个赢家（INV-5）。
 *
 * <p>拆成独立服务而不是给 AgentRunService 加一个"系统调用方"参数：后者会让每个调用点都
 * 有机会传入伪造的系统身份，绕过 RBAC。
 */
@Service
public class TerminalSettlementService {

    private final AgentRunMapper runMapper;
    private final RunTerminalStateMapper terminalMapper;
    private final AuditService audit;
    private final Clock clock;

    public TerminalSettlementService(AgentRunMapper runMapper, RunTerminalStateMapper terminalMapper,
                                     AuditService audit, Clock clock) {
        this.runMapper = runMapper;
        this.terminalMapper = terminalMapper;
        this.audit = audit;
        this.clock = clock;
    }

    /** @return true 表示本次是首次结算 */
    @Transactional
    public boolean settle(AgentRun run, RunStatus terminalStatus, FailureClass failureClass,
                          String decidedBy) {
        if (!terminalStatus.isTerminal()) {
            throw new IllegalArgumentException("not a terminal status: " + terminalStatus);
        }
        runMapper.lockForSettlement(run.runId());
        String wire = failureClass == null ? null : failureClass.wireValue();
        try {
            terminalMapper.insert(run.runId(), terminalStatus.name(), wire, decidedBy, clock.instant());
        } catch (DuplicateKeyException alreadySettled) {
            String existing = terminalMapper.findTerminalStatusFresh(run.runId());
            audit.record(run.tenantId(), decidedBy, "run.settle_terminal", "run", run.runId(),
                    AuditEvent.OUTCOME_DENIED, "terminal state already set to " + existing,
                    null, null);
            return false;
        }
        runMapper.applyTerminalStatus(run.runId(), terminalStatus, wire, clock.instant());
        audit.allowed(run.tenantId(), decidedBy, "run.settle_terminal", "run", run.runId(),
                "{\"terminalStatus\":\"%s\",\"failureClass\":%s}".formatted(
                        terminalStatus, wire == null ? "null" : "\"" + wire + "\""));
        return true;
    }
}
