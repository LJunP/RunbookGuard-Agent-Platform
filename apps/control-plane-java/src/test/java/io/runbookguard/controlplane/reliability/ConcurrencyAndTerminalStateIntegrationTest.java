package io.runbookguard.controlplane.reliability;

import io.runbookguard.controlplane.AbstractIntegrationTest;
import io.runbookguard.controlplane.domain.Incident;
import io.runbookguard.controlplane.domain.IncidentStatus;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.persistence.RunTerminalStateMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import io.runbookguard.controlplane.service.IncidentService;
import io.runbookguard.controlplane.service.OptimisticLockConflictException;
import io.runbookguard.controlplane.service.TerminalStateAlreadySetException;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 威胁 T-6 的数据库层地基：乐观锁与唯一终态。M2 的 Lease 接管、消息重投都建立在这两条之上，
 * 所以这里用真并发验证，而不是顺序调用两次。
 */
class ConcurrencyAndTerminalStateIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    IncidentService incidentService;
    @Autowired
    AgentRunService runService;
    @Autowired
    RunTerminalStateMapper terminalMapper;

    @Test
    @DisplayName("乐观锁：版本过期的更新失败，不静默覆盖")
    void staleVersionUpdateFails() {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        Incident incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());

        incidentService.updateStatus(operator, incident.incidentId(),
                IncidentStatus.DIAGNOSING, incident.version());

        assertThrows(OptimisticLockConflictException.class, () ->
                incidentService.updateStatus(operator, incident.incidentId(),
                        IncidentStatus.MITIGATING, incident.version()));
    }

    @Test
    @DisplayName("并发更新同一 Incident：恰好一个成功")
    void concurrentUpdatesLeaveExactlyOneWinner() throws Exception {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        Incident incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        long version = incident.version();

        int threads = 8;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        AtomicInteger success = new AtomicInteger();
        AtomicInteger conflict = new AtomicInteger();
        List<Callable<Void>> tasks = new ArrayList<>();
        for (int i = 0; i < threads; i++) {
            tasks.add(() -> {
                try {
                    incidentService.updateStatus(operator, incident.incidentId(),
                            IncidentStatus.DIAGNOSING, version);
                    success.incrementAndGet();
                } catch (OptimisticLockConflictException e) {
                    conflict.incrementAndGet();
                }
                return null;
            });
        }
        for (Future<Void> f : pool.invokeAll(tasks)) {
            f.get();
        }
        pool.shutdown();

        assertEquals(1, success.get(), "只应有一个更新成功");
        assertEquals(threads - 1, conflict.get(), "其余全部应为版本冲突");
    }

    @Test
    @DisplayName("唯一终态：第二次结算返回既有终态且 firstSettlement=false")
    void secondSettlementIsIdempotent() {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        Incident incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");

        var first = runService.settleTerminal(agent, run.runId(), RunStatus.COMPLETE, null);
        assertTrue(first.firstSettlement());
        assertEquals(RunStatus.COMPLETE, first.terminalStatus());

        var second = runService.settleTerminal(agent, run.runId(), RunStatus.FAILED, "budget_exhausted");
        assertFalse(second.firstSettlement(), "重复结算不是首次");
        assertEquals(RunStatus.COMPLETE, second.terminalStatus(), "终态不得被后来的结算改写");
        assertEquals(1, terminalMapper.countByRun(run.runId()), "终态记录必须只有一条");
    }

    @Test
    @DisplayName("消息投递三次：终态记录仍只有一条（INV-5）")
    void tripleDeliveryYieldsSingleTerminalState() {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        Incident incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");

        for (int i = 0; i < 3; i++) {
            runService.settleTerminal(agent, run.runId(), RunStatus.COMPLETE, null);
        }
        assertEquals(1, terminalMapper.countByRun(run.runId()));
    }

    @Test
    @DisplayName("并发结算终态：恰好一个 firstSettlement=true")
    void concurrentSettlementHasOneWinner() throws Exception {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        Incident incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");

        int threads = 6;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        AtomicInteger firsts = new AtomicInteger();
        List<Callable<Void>> tasks = new ArrayList<>();
        for (int i = 0; i < threads; i++) {
            final RunStatus status = (i % 2 == 0) ? RunStatus.COMPLETE : RunStatus.FAILED;
            tasks.add(() -> {
                var outcome = runService.settleTerminal(agent, run.runId(), status,
                        status == RunStatus.FAILED ? "deadline_exceeded" : null);
                if (outcome.firstSettlement()) {
                    firsts.incrementAndGet();
                }
                return null;
            });
        }
        for (Future<Void> f : pool.invokeAll(tasks)) {
            f.get();
        }
        pool.shutdown();

        assertEquals(1, firsts.get(), "并发结算只能有一个首次成功");
        assertEquals(1, terminalMapper.countByRun(run.runId()));
    }

    @Test
    @DisplayName("已达终态的 Run 不能再推进非终态")
    void terminalRunCannotAdvance() {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        Incident incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        runService.settleTerminal(agent, run.runId(), RunStatus.COMPLETE, null);

        var latest = runService.get(agent, run.runId());
        assertThrows(TerminalStateAlreadySetException.class, () ->
                runService.advance(agent, run.runId(), RunStatus.OBSERVE, "OBSERVE", latest.version()));
    }

    @Test
    @DisplayName("终态只能通过 settleTerminal 写入，advance 拒绝终态")
    void advanceRejectsTerminalStatus() {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        Incident incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");

        assertThrows(IllegalArgumentException.class, () ->
                runService.advance(agent, run.runId(), RunStatus.FAILED, null, run.version()));
    }
}
