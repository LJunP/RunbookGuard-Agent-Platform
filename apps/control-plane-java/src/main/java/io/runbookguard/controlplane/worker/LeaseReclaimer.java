package io.runbookguard.controlplane.worker;

import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.config.RunbookGuardProperties;
import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.messaging.FailureClass;
import io.runbookguard.controlplane.messaging.RunDispatchPublisher;
import io.runbookguard.controlplane.persistence.AgentRunMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.util.List;

/**
 * 扫描 Lease 已过期但尚未落终态的 Run，重新派发。这是 kill -9 之后任务得以继续的机制：
 * 死掉的 Worker 不会自己 release，只能靠过期 + 扫描发现。
 *
 * <p>重新派发前先检查预算：deadline 已过或步数耗尽的 Run 不该被无限重新派发，应直接
 * 结算终态。否则一个卡住的 Run 会被反复派发，形成慢速无限循环（威胁 T-5）。
 */
@Service
public class LeaseReclaimer {

    private static final Logger log = LoggerFactory.getLogger(LeaseReclaimer.class);

    private final LeaseService leaseService;
    private final AgentRunMapper runMapper;
    private final RunDispatchPublisher publisher;
    private final TerminalSettlementService settlement;
    private final AuditService audit;
    private final RunbookGuardProperties props;
    private final Clock clock;

    public LeaseReclaimer(LeaseService leaseService, AgentRunMapper runMapper,
                          RunDispatchPublisher publisher, TerminalSettlementService settlement,
                          AuditService audit, RunbookGuardProperties props, Clock clock) {
        this.leaseService = leaseService;
        this.runMapper = runMapper;
        this.publisher = publisher;
        this.settlement = settlement;
        this.audit = audit;
        this.props = props;
        this.clock = clock;
    }

    /**
     * 报告中带上具体 runId，而不只是计数。回收器按设计扫描全局（它是后台任务），
     * 聚合计数因此不足以判断"我关心的那个 Run 被怎么处理了"。
     */
    public record ReclaimReport(List<String> redispatched, List<String> settledExhausted) {

        public int redispatchedCount() {
            return redispatched.size();
        }

        public int settledCount() {
            return settledExhausted.size();
        }
    }

    @Transactional
    public ReclaimReport reclaimOnce(int limit) {
        List<String> candidates = leaseService.findReclaimable(limit);
        List<String> redispatched = new java.util.ArrayList<>();
        List<String> settled = new java.util.ArrayList<>();

        for (String runId : candidates) {
            AgentRun run = runMapper.findByIdAnyTenant(runId);
            if (run == null || run.status().isTerminal()) {
                continue;
            }
            if (runMapper.findCancelRequestedAt(runId, run.tenantId()) != null) {
                settlement.settle(run, RunStatus.FAILED, FailureClass.CANCELLED, "lease-reclaimer");
                settled.add(runId);
                continue;
            }
            FailureClass exhausted = exhaustedBudget(run);
            if (exhausted != null) {
                settlement.settle(run, RunStatus.FAILED, exhausted, "lease-reclaimer");
                settled.add(runId);
                continue;
            }
            if (run.dispatchSequence() >= props.worker().maxDeliveryAttempts()) {
                settlement.settle(run, RunStatus.FAILED, FailureClass.MAX_DELIVERY_EXCEEDED,
                        "lease-reclaimer");
                settled.add(runId);
                continue;
            }
            publisher.dispatch(run, null);
            audit.allowed(run.tenantId(), "lease-reclaimer", "run.reclaim", "run", runId,
                    "{\"attempt\":%d}".formatted(run.dispatchSequence() + 1));
            redispatched.add(runId);
        }
        if (!redispatched.isEmpty() || !settled.isEmpty()) {
            log.info("lease reclaim: redispatched={} settledExhausted={}",
                    redispatched.size(), settled.size());
        }
        return new ReclaimReport(redispatched, settled);
    }

    private FailureClass exhaustedBudget(AgentRun run) {
        if (clock.instant().isAfter(run.deadline())) {
            return FailureClass.DEADLINE_EXCEEDED;
        }
        if (run.stepsUsed() >= run.maxSteps()) {
            return FailureClass.MAX_STEPS_EXHAUSTED;
        }
        if (run.costSpentMicros() >= run.costBudgetMicros()) {
            return FailureClass.COST_BUDGET_EXHAUSTED;
        }
        if (run.tokenSpent() >= run.tokenBudget()) {
            return FailureClass.TOKEN_BUDGET_EXHAUSTED;
        }
        if (run.toolCallCount() >= run.toolCallBudget()) {
            return FailureClass.TOOL_CALL_BUDGET_EXHAUSTED;
        }
        return null;
    }
}
