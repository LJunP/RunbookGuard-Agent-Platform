package io.runbookguard.controlplane.worker;

import io.runbookguard.controlplane.AbstractIntegrationTest;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.persistence.RunTerminalStateMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import io.runbookguard.controlplane.service.IncidentService;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** ADR-0003 的验收测试。这些是 M2 Gate 的核心证据。 */
class LeaseIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    IncidentService incidentService;
    @Autowired
    AgentRunService runService;
    @Autowired
    LeaseService leaseService;
    @Autowired
    RunTerminalStateMapper terminalMapper;

    private String newRun() {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        var incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        return runService.create(agent, incident.incidentId(), "g1", "p1", "fake", "d1").runId();
    }

    @Test
    @DisplayName("首次获取 Lease 成功，fencing_token 从 1 开始")
    void firstAcquireSucceeds() {
        String runId = newRun();
        Optional<Lease> lease = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofSeconds(30));
        assertTrue(lease.isPresent());
        assertEquals(1L, lease.get().fencingToken());
        assertEquals("worker-1", lease.get().ownerId());
    }

    @Test
    @DisplayName("Lease 未过期时第二个 Worker 无法接管")
    void unexpiredLeaseCannotBeStolen() {
        String runId = newRun();
        assertTrue(leaseService.acquire(runId, tenantA, "worker-1", Duration.ofSeconds(30)).isPresent());
        assertTrue(leaseService.acquire(runId, tenantA, "worker-2", Duration.ofSeconds(30)).isEmpty(),
                "未过期的 Lease 不得被其他 Worker 抢走");
    }

    @Test
    @DisplayName("原持有者可以重入（重试同一消息时不必等自己的 Lease 过期）")
    void ownerCanReacquire() {
        String runId = newRun();
        Lease first = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofSeconds(30)).orElseThrow();
        Lease second = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofSeconds(30)).orElseThrow();
        assertEquals(first.fencingToken() + 1, second.fencingToken(),
                "重入也应递增 token，使旧 token 失效");
    }

    @Test
    @DisplayName("Lease 过期后可被另一个 Worker 接管，fencing_token 递增")
    void expiredLeaseCanBeTakenOver() throws Exception {
        String runId = newRun();
        Lease first = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofMillis(300)).orElseThrow();
        Thread.sleep(600);

        Lease taken = leaseService.acquire(runId, tenantA, "worker-2", Duration.ofSeconds(30)).orElseThrow();
        assertEquals("worker-2", taken.ownerId());
        assertTrue(taken.fencingToken() > first.fencingToken(),
                "接管必须递增 fencing token，否则僵尸 Worker 的写入无法被识别");
    }

    @Test
    @DisplayName("Heartbeat 顺延过期时间")
    void heartbeatExtendsLease() throws Exception {
        String runId = newRun();
        Lease lease = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofMillis(500)).orElseThrow();
        Thread.sleep(300);
        assertTrue(leaseService.heartbeat(runId, "worker-1", lease.fencingToken(), Duration.ofSeconds(30)));
        Thread.sleep(400);

        // 原 TTL 已过，但 heartbeat 顺延过，别人仍抢不到
        assertTrue(leaseService.acquire(runId, tenantA, "worker-2", Duration.ofSeconds(5)).isEmpty());
    }

    @Test
    @DisplayName("非持有者的 heartbeat 被拒绝")
    void heartbeatFromNonOwnerIsRejected() {
        String runId = newRun();
        Lease lease = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofSeconds(30)).orElseThrow();
        assertFalse(leaseService.heartbeat(runId, "worker-2", lease.fencingToken(), Duration.ofSeconds(30)));
    }

    @Test
    @DisplayName("旧 fencing token 的 heartbeat 被拒绝（僵尸 Worker）")
    void heartbeatWithStaleTokenIsRejected() throws Exception {
        String runId = newRun();
        Lease first = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofMillis(200)).orElseThrow();
        Thread.sleep(400);
        leaseService.acquire(runId, tenantA, "worker-2", Duration.ofSeconds(30)).orElseThrow();

        assertFalse(leaseService.heartbeat(runId, "worker-1", first.fencingToken(), Duration.ofSeconds(30)),
                "被接管后原 Worker 的 heartbeat 必须失败");
    }

    @Test
    @DisplayName("释放后 Lease 立即可被接管")
    void releasedLeaseIsImmediatelyAvailable() {
        String runId = newRun();
        Lease lease = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofSeconds(30)).orElseThrow();
        assertTrue(leaseService.release(runId, "worker-1", lease.fencingToken()));
        assertTrue(leaseService.acquire(runId, tenantA, "worker-2", Duration.ofSeconds(30)).isPresent());
    }

    @Test
    @DisplayName("并发获取同一 Lease：恰好一个成功")
    void concurrentAcquireHasOneWinner() throws Exception {
        String runId = newRun();
        int threads = 8;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        AtomicInteger winners = new AtomicInteger();
        List<Callable<Void>> tasks = new ArrayList<>();
        for (int i = 0; i < threads; i++) {
            final String worker = "worker-" + i;
            tasks.add(() -> {
                if (leaseService.acquire(runId, tenantA, worker, Duration.ofSeconds(30)).isPresent()) {
                    winners.incrementAndGet();
                }
                return null;
            });
        }
        for (Future<Void> f : pool.invokeAll(tasks)) {
            f.get();
        }
        pool.shutdown();
        assertEquals(1, winners.get(), "并发获取只能有一个赢家");
    }

    @Test
    @DisplayName("持旧 token 上报进度被拒绝（fencing 生效）")
    void staleTokenProgressIsRejected() throws Exception {
        String runId = newRun();
        Lease first = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofMillis(200)).orElseThrow();
        Thread.sleep(400);
        Lease taken = leaseService.acquire(runId, tenantA, "worker-2", Duration.ofSeconds(30)).orElseThrow();

        assertThrows(StaleFencingTokenException.class, () ->
                leaseService.requireValidToken(runId, "worker-1", first.fencingToken()));
        leaseService.requireValidToken(runId, "worker-2", taken.fencingToken());
    }

    @Test
    @DisplayName("kill -9 模拟：Worker 消失后 Lease 过期，接管者结算唯一终态")
    void killedWorkerLeadsToSingleTerminalState() throws Exception {
        String runId = newRun();
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");

        // worker-1 拿到 Lease 后"消失"：不 release、不 heartbeat
        Lease dead = leaseService.acquire(runId, tenantA, "worker-1", Duration.ofMillis(300)).orElseThrow();
        Thread.sleep(600);

        Lease taken = leaseService.acquire(runId, tenantA, "worker-2", Duration.ofSeconds(30)).orElseThrow();
        leaseService.requireValidToken(runId, "worker-2", taken.fencingToken());
        runService.settleTerminal(agent, runId, RunStatus.COMPLETE, null);

        // 僵尸 worker-1 恢复后试图结算
        assertThrows(StaleFencingTokenException.class, () ->
                leaseService.requireValidToken(runId, "worker-1", dead.fencingToken()));
        assertEquals(1, terminalMapper.countByRun(runId), "唯一终态（INV-5）");
    }

    @Test
    @DisplayName("过期 Lease 扫描能找出可接管的 Run")
    void expiredLeasesAreDiscoverable() throws Exception {
        String runId = newRun();
        leaseService.acquire(runId, tenantA, "worker-1", Duration.ofMillis(200));
        Thread.sleep(400);
        assertTrue(leaseService.findReclaimable(50).contains(runId));
    }

    @Test
    @DisplayName("未过期的 Lease 不出现在可接管列表里")
    void activeLeasesAreNotReclaimable() {
        String runId = newRun();
        leaseService.acquire(runId, tenantA, "worker-1", Duration.ofSeconds(60));
        assertFalse(leaseService.findReclaimable(50).contains(runId));
    }
}
