package io.runbookguard.controlplane.worker;

import io.runbookguard.controlplane.AbstractIntegrationTest;
import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.messaging.RabbitTopologyConfig;
import io.runbookguard.controlplane.messaging.RunDispatchPublisher;
import io.runbookguard.controlplane.messaging.RunMessages;
import io.runbookguard.controlplane.persistence.AgentRunMapper;
import io.runbookguard.controlplane.persistence.RunTerminalStateMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import io.runbookguard.controlplane.service.IncidentService;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.amqp.core.Message;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.TestPropertySource;

import java.time.Duration;
import java.time.Instant;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * kill -9 演练：Worker 拿到 Lease 后不 release 不 heartbeat（等价于被强杀），
 * 验证任务能被重新派发或落到唯一失败终态。
 *
 * <p>maxDeliveryAttempts 调到 3 以便在测试里触发上限。
 */
@TestPropertySource(properties = {
        "runbookguard.worker.max-delivery-attempts=3",
        "runbookguard.worker.lease-ttl=PT0.3S"
})
class WorkerRecoveryIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    IncidentService incidentService;
    @Autowired
    AgentRunService runService;
    @Autowired
    LeaseService leaseService;
    @Autowired
    LeaseReclaimer reclaimer;
    @Autowired
    CancellationService cancellation;
    @Autowired
    RunDispatchPublisher publisher;
    @Autowired
    AgentRunMapper runMapper;
    @Autowired
    RunTerminalStateMapper terminalMapper;
    @Autowired
    RabbitTemplate rabbit;

    private AgentRun newRun() {
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        var incident = incidentService.create(operator, "synthetic-lab", "P2", "t", Instant.now());
        return runService.create(agent, incident.incidentId(), "g1", "p1", "fake", "d1");
    }

    private void drainDispatchQueue() {
        while (rabbit.receive(RabbitTopologyConfig.QUEUE_DISPATCH, 200) != null) {
            // 清空队列，避免上一个测试的消息干扰断言
        }
    }

    @Test
    @DisplayName("派发消息带幂等键与租户头，序号自增")
    void dispatchCarriesIdempotencyKey() {
        drainDispatchQueue();
        AgentRun run = newRun();

        RunMessages.RunDispatch first = publisher.dispatch(run, null);
        assertEquals(1, first.sequence());
        assertEquals("run.dispatch:" + run.runId() + ":1", first.idempotencyKey());

        Message received = rabbit.receive(RabbitTopologyConfig.QUEUE_DISPATCH, 3000);
        assertNotNull(received, "消息应已进入 dispatch 队列");
        var props = received.getMessageProperties();
        assertEquals(first.idempotencyKey(), props.getHeader("x-idempotency-key"));
        assertEquals(tenantA, props.getHeader("x-tenant-id"));
        assertEquals(Integer.valueOf(RunMessages.SCHEMA_VERSION),
                props.getHeader("x-schema-version"));
    }

    @Test
    @DisplayName("重新派发时序号递增，幂等键随之变化")
    void redispatchIncrementsSequence() {
        drainDispatchQueue();
        AgentRun run = newRun();
        assertEquals(1, publisher.dispatch(run, null).sequence());
        AgentRun reloaded = runMapper.findByIdAnyTenant(run.runId());
        assertEquals(2, publisher.dispatch(reloaded, null).sequence());
    }

    @Test
    @DisplayName("kill -9：Lease 过期后回收器重新派发")
    void killedWorkerLeadsToRedispatch() throws Exception {
        drainDispatchQueue();
        AgentRun run = newRun();
        publisher.dispatch(run, null);
        drainDispatchQueue();

        // Worker 拿到 Lease 后"被杀"：不 release、不 heartbeat
        leaseService.acquire(run.runId(), tenantA, "worker-doomed", Duration.ofMillis(200));
        Thread.sleep(400);

        var report = reclaimer.reclaimOnce(50);
        assertTrue(report.redispatched().contains(run.runId()),
                "过期 Lease 的 Run 应被重新派发");
        assertNotNull(rabbit.receive(RabbitTopologyConfig.QUEUE_DISPATCH, 3000),
                "重新派发的消息应进入队列");
    }

    @Test
    @DisplayName("deadline 已过的 Run 不再重新派发，直接落 FAILED/deadline_exceeded")
    void expiredDeadlineSettlesInsteadOfRedispatch() throws Exception {
        drainDispatchQueue();
        AgentRun run = newRun();
        jdbcSetDeadlineInPast(run.runId());

        leaseService.acquire(run.runId(), tenantA, "worker-doomed", Duration.ofMillis(200));
        Thread.sleep(400);

        var report = reclaimer.reclaimOnce(50);
        assertTrue(report.settledExhausted().contains(run.runId()));
        assertFalse(report.redispatched().contains(run.runId()));
        assertEquals("FAILED", terminalMapper.findTerminalStatus(run.runId()));
        assertEquals(1, terminalMapper.countByRun(run.runId()), "唯一终态");
    }

    @Test
    @DisplayName("投递次数达上限后落 FAILED/max_delivery_exceeded，不悬挂在 DLQ")
    void maxDeliveryExceededSettlesTerminal() throws Exception {
        drainDispatchQueue();
        AgentRun run = newRun();

        // 连续派发到达上限（配置为 3）
        for (int i = 0; i < 3; i++) {
            publisher.dispatch(runMapper.findByIdAnyTenant(run.runId()), null);
        }
        drainDispatchQueue();

        leaseService.acquire(run.runId(), tenantA, "worker-doomed", Duration.ofMillis(200));
        Thread.sleep(400);

        var report = reclaimer.reclaimOnce(50);
        assertTrue(report.settledExhausted().contains(run.runId()));
        assertEquals("FAILED", terminalMapper.findTerminalStatus(run.runId()));
    }

    @Test
    @DisplayName("已落终态的 Run 不会被回收器再次处理")
    void terminalRunIsNotReclaimed() throws Exception {
        drainDispatchQueue();
        AgentRun run = newRun();
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        runService.settleTerminal(agent, run.runId(), RunStatus.COMPLETE, null);

        leaseService.acquire(run.runId(), tenantA, "worker-1", Duration.ofMillis(200));
        Thread.sleep(400);

        var report = reclaimer.reclaimOnce(50);
        assertFalse(report.redispatched().contains(run.runId()));
        assertFalse(report.settledExhausted().contains(run.runId()));
        assertEquals(1, terminalMapper.countByRun(run.runId()));
    }

    @Test
    @DisplayName("取消请求：回收器结算 FAILED/cancelled 而非重新派发")
    void cancelledRunSettlesAsCancelled() throws Exception {
        drainDispatchQueue();
        AgentRun run = newRun();
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        assertTrue(cancellation.requestCancel(operator, run.runId()));

        leaseService.acquire(run.runId(), tenantA, "worker-doomed", Duration.ofMillis(200));
        Thread.sleep(400);

        var report = reclaimer.reclaimOnce(50);
        assertTrue(report.settledExhausted().contains(run.runId()));
        assertFalse(report.redispatched().contains(run.runId()));
        assertEquals("FAILED", terminalMapper.findTerminalStatus(run.runId()));
    }

    @Test
    @DisplayName("重复取消请求是幂等的")
    void repeatedCancelIsIdempotent() {
        AgentRun run = newRun();
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        assertTrue(cancellation.requestCancel(operator, run.runId()));
        assertFalse(cancellation.requestCancel(operator, run.runId()),
                "第二次取消请求不应再次生效");
        assertTrue(cancellation.isCancelRequested(run.runId(), tenantA));
    }

    @Test
    @DisplayName("已达终态的 Run 无法被取消")
    void terminalRunCannotBeCancelled() {
        AgentRun run = newRun();
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller operator = caller(tenantA, "OPERATOR");
        runService.settleTerminal(agent, run.runId(), RunStatus.COMPLETE, null);
        assertFalse(cancellation.requestCancel(operator, run.runId()));
    }

    @Autowired
    org.springframework.jdbc.core.JdbcTemplate jdbc;

    private void jdbcSetDeadlineInPast(String runId) {
        jdbc.update("UPDATE agent_run SET deadline = NOW(3) - INTERVAL 1 HOUR WHERE run_id = ?", runId);
    }
}
