package io.runbookguard.controlplane.approval;

import io.runbookguard.controlplane.AbstractIntegrationTest;
import io.runbookguard.controlplane.audit.AuditEvent;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.domain.Approval;
import io.runbookguard.controlplane.domain.ApprovalDecision;
import io.runbookguard.controlplane.persistence.ApprovalMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import io.runbookguard.controlplane.service.ApprovalNotDecidableException;
import io.runbookguard.controlplane.service.ApprovalService;
import io.runbookguard.controlplane.service.ApprovalVerificationFailedException;
import io.runbookguard.controlplane.service.IncidentService;
import io.runbookguard.controlplane.api.ForbiddenException;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 威胁 T-2 的验收测试。这些是 M1 Gate 的核心证据：审批之后改参数必须执行不了。
 */
class ApprovalSecurityIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    IncidentService incidentService;
    @Autowired
    AgentRunService runService;
    @Autowired
    ApprovalService approvalService;
    @Autowired
    ApprovalMapper approvalMapper;
    @Autowired
    AuditService auditService;

    private Map<String, Object> args() {
        Map<String, Object> a = new LinkedHashMap<>();
        a.put("service", "synthetic-orders");
        a.put("target_version", "v1.4.2");
        return a;
    }

    private Approval approvedFixture(AuthenticatedCaller agent, AuthenticatedCaller approver) {
        var incident = incidentService.create(operator(), "synthetic-lab", "P2", "pool exhausted", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        Approval requested = approvalService.request(agent, run.runId(),
                "rollback_synthetic_deployment", "svc:synthetic-orders", args());
        return approvalService.decide(approver, requested.approvalId(), true, "evidence looks solid");
    }

    private AuthenticatedCaller operatorCache;

    private AuthenticatedCaller operator() {
        if (operatorCache == null) {
            operatorCache = caller(tenantA, "OPERATOR");
        }
        return operatorCache;
    }

    @Test
    @DisplayName("审批后篡改参数：digest 不符则拒绝执行，并留下 DENIED 审计")
    void tamperedArgumentsAreRejected() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller approver = caller(tenantA, "APPROVER");
        Approval approved = approvedFixture(agent, approver);

        Map<String, Object> tampered = new LinkedHashMap<>();
        tampered.put("service", "synthetic-db");
        tampered.put("target_version", "v1.4.2");

        var ex = assertThrows(ApprovalVerificationFailedException.class, () ->
                approvalService.verifyAndConsume(agent, approved.approvalId(),
                        "rollback_synthetic_deployment", "svc:synthetic-orders", tampered));
        assertTrue(ex.getMessage().contains("arguments digest mismatch"), ex.getMessage());

        Approval after = approvalMapper.findByIdInTenant(approved.approvalId(), tenantA);
        assertEquals(null, after.consumedAt(), "拒绝的执行不得消费审批");

        List<AuditEvent> events = auditService.listByResource(tenantA, "approval", approved.approvalId());
        assertTrue(events.stream().anyMatch(e ->
                        "approval.consume".equals(e.action())
                                && AuditEvent.OUTCOME_DENIED.equals(e.outcome())),
                "篡改尝试必须留下 DENIED 审计事件");
    }

    @Test
    @DisplayName("键顺序不同但语义相同的参数可以正常执行（避免误拒）")
    void reorderedArgumentsStillMatch() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller approver = caller(tenantA, "APPROVER");
        Approval approved = approvedFixture(agent, approver);

        Map<String, Object> reordered = new LinkedHashMap<>();
        reordered.put("target_version", "v1.4.2");
        reordered.put("service", "synthetic-orders");

        Approval consumed = approvalService.verifyAndConsume(agent, approved.approvalId(),
                "rollback_synthetic_deployment", "svc:synthetic-orders", reordered);
        assertNotNull(consumed.consumedAt());
    }

    @Test
    @DisplayName("摘要一致但工具名不同：拒绝")
    void toolMismatchIsRejected() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller approver = caller(tenantA, "APPROVER");
        Approval approved = approvedFixture(agent, approver);

        var ex = assertThrows(ApprovalVerificationFailedException.class, () ->
                approvalService.verifyAndConsume(agent, approved.approvalId(),
                        "restart_synthetic_service", "svc:synthetic-orders", args()));
        assertTrue(ex.getMessage().contains("tool mismatch"), ex.getMessage());
    }

    @Test
    @DisplayName("摘要一致但资源不同：拒绝")
    void resourceMismatchIsRejected() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller approver = caller(tenantA, "APPROVER");
        Approval approved = approvedFixture(agent, approver);

        var ex = assertThrows(ApprovalVerificationFailedException.class, () ->
                approvalService.verifyAndConsume(agent, approved.approvalId(),
                        "rollback_synthetic_deployment", "svc:synthetic-db", args()));
        assertTrue(ex.getMessage().contains("resource mismatch"), ex.getMessage());
    }

    @Test
    @DisplayName("同一份审批不能用两次（防重放）")
    void approvalIsSingleUse() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller approver = caller(tenantA, "APPROVER");
        Approval approved = approvedFixture(agent, approver);

        approvalService.verifyAndConsume(agent, approved.approvalId(),
                "rollback_synthetic_deployment", "svc:synthetic-orders", args());

        var ex = assertThrows(ApprovalVerificationFailedException.class, () ->
                approvalService.verifyAndConsume(agent, approved.approvalId(),
                        "rollback_synthetic_deployment", "svc:synthetic-orders", args()));
        assertTrue(ex.getMessage().contains("already consumed"), ex.getMessage());
    }

    @Test
    @DisplayName("未批准的审批不能被消费")
    void pendingApprovalCannotBeConsumed() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        var incident = incidentService.create(operator(), "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        Approval pending = approvalService.request(agent, run.runId(),
                "restart_synthetic_service", "svc:synthetic-report", args());

        var ex = assertThrows(ApprovalVerificationFailedException.class, () ->
                approvalService.verifyAndConsume(agent, pending.approvalId(),
                        "restart_synthetic_service", "svc:synthetic-report", args()));
        assertTrue(ex.getMessage().contains("not APPROVED"), ex.getMessage());
    }

    @Test
    @DisplayName("被拒绝的审批不能被消费")
    void rejectedApprovalCannotBeConsumed() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller approver = caller(tenantA, "APPROVER");
        var incident = incidentService.create(operator(), "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        Approval requested = approvalService.request(agent, run.runId(),
                "restart_synthetic_service", "svc:synthetic-report", args());
        Approval rejected = approvalService.decide(approver, requested.approvalId(), false,
                "restart does not fix OOMKilled");
        assertEquals(ApprovalDecision.REJECTED, rejected.decision());

        assertThrows(ApprovalVerificationFailedException.class, () ->
                approvalService.verifyAndConsume(agent, rejected.approvalId(),
                        "restart_synthetic_service", "svc:synthetic-report", args()));
    }

    @Test
    @DisplayName("过期审批不能被决策")
    void expiredApprovalCannotBeDecided() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller approver = caller(tenantA, "APPROVER");
        var incident = incidentService.create(operator(), "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        Approval requested = approvalService.request(agent, run.runId(),
                "restart_synthetic_service", "svc:synthetic-report", args());

        // 直接把 expires_at 推到过去，模拟审批人拖太久。
        approvalMapper.markExpired(tenantA, requested.expiresAt().plusSeconds(1));

        assertThrows(ApprovalNotDecidableException.class, () ->
                approvalService.decide(approver, requested.approvalId(), true, "too late"));
    }

    @Test
    @DisplayName("AGENT_RUNTIME 角色不能批准审批（INV-4）")
    void agentRuntimeCannotApprove() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        var incident = incidentService.create(operator(), "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        Approval requested = approvalService.request(agent, run.runId(),
                "restart_synthetic_service", "svc:synthetic-report", args());

        var ex = assertThrows(ForbiddenException.class, () ->
                approvalService.decide(agent, requested.approvalId(), true, "self approve"));
        assertTrue(ex.getMessage().contains("APPROVER"), ex.getMessage());
    }

    @Test
    @DisplayName("发起人即使有 APPROVER 角色也不能批准自己的请求")
    void requesterCannotApproveOwnRequest() {
        AuthenticatedCaller dual = caller(tenantA, "AGENT_RUNTIME,APPROVER");
        var incident = incidentService.create(operator(), "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(dual, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        Approval requested = approvalService.request(dual, run.runId(),
                "restart_synthetic_service", "svc:synthetic-report", args());

        var ex = assertThrows(ForbiddenException.class, () ->
                approvalService.decide(dual, requested.approvalId(), true, "self"));
        assertTrue(ex.getMessage().contains("own request"), ex.getMessage());
    }

    @Test
    @DisplayName("审批记录持久化了 digest_alg 与规范化参数原文")
    void approvalPersistsDigestAlgAndCanonicalForm() {
        AuthenticatedCaller agent = caller(tenantA, "AGENT_RUNTIME");
        var incident = incidentService.create(operator(), "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(agent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        Approval requested = approvalService.request(agent, run.runId(),
                "rollback_synthetic_deployment", "svc:synthetic-orders", args());

        assertEquals("JCS-SHA256-V1", requested.digestAlg());
        assertEquals("{\"service\":\"synthetic-orders\",\"target_version\":\"v1.4.2\"}",
                requested.argumentsCanonical());
        assertEquals(64, requested.argumentsDigest().length());
    }
}
