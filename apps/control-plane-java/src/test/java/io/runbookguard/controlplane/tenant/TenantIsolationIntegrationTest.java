package io.runbookguard.controlplane.tenant;

import io.runbookguard.controlplane.AbstractIntegrationTest;
import io.runbookguard.controlplane.api.ResourceNotFoundException;
import io.runbookguard.controlplane.audit.AuditEvent;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.domain.Incident;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import io.runbookguard.controlplane.service.ApprovalService;
import io.runbookguard.controlplane.service.IncidentService;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.time.Instant;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 威胁 T-4：跨租户访问必须失败，且不泄漏"该 id 在别处存在"这一信息。 */
class TenantIsolationIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    IncidentService incidentService;
    @Autowired
    AgentRunService runService;
    @Autowired
    ApprovalService approvalService;
    @Autowired
    AuditService auditService;

    @Test
    @DisplayName("读取其它租户的 Incident 返回 not found 而非 forbidden")
    void crossTenantIncidentReadIsNotFound() {
        AuthenticatedCaller a = caller(tenantA, "OPERATOR");
        AuthenticatedCaller b = caller(tenantB, "OPERATOR");
        Incident incident = incidentService.create(a, "synthetic-lab", "P2", "tenant A only", Instant.now());

        assertThrows(ResourceNotFoundException.class, () -> incidentService.get(b, incident.incidentId()));
    }

    @Test
    @DisplayName("跨租户读取尝试留下 DENIED 审计事件")
    void crossTenantAttemptIsAudited() {
        AuthenticatedCaller a = caller(tenantA, "OPERATOR");
        AuthenticatedCaller b = caller(tenantB, "OPERATOR");
        Incident incident = incidentService.create(a, "synthetic-lab", "P2", "t", Instant.now());

        assertThrows(ResourceNotFoundException.class, () -> incidentService.get(b, incident.incidentId()));

        var events = auditService.listByResource(tenantB, "incident", incident.incidentId());
        assertTrue(events.stream().anyMatch(e -> AuditEvent.OUTCOME_DENIED.equals(e.outcome())),
                "跨租户尝试必须在被访问方之外的租户审计流里可见");
    }

    @Test
    @DisplayName("列表接口不会返回其它租户的数据")
    void listIsScopedToTenant() {
        AuthenticatedCaller a = caller(tenantA, "OPERATOR");
        AuthenticatedCaller b = caller(tenantB, "OPERATOR");
        incidentService.create(a, "synthetic-lab", "P2", "A-1", Instant.now());
        incidentService.create(a, "synthetic-lab", "P2", "A-2", Instant.now());
        incidentService.create(b, "synthetic-lab", "P2", "B-1", Instant.now());

        assertTrue(incidentService.list(a, 100, 0).stream()
                .allMatch(i -> i.tenantId().equals(tenantA)));
        assertTrue(incidentService.list(b, 100, 0).stream()
                .allMatch(i -> i.tenantId().equals(tenantB)));
    }

    @Test
    @DisplayName("不能在其它租户的 Incident 上创建 Run")
    void cannotCreateRunOnForeignIncident() {
        AuthenticatedCaller a = caller(tenantA, "OPERATOR");
        AuthenticatedCaller bAgent = caller(tenantB, "AGENT_RUNTIME");
        Incident incident = incidentService.create(a, "synthetic-lab", "P2", "t", Instant.now());

        assertThrows(ResourceNotFoundException.class, () ->
                runService.create(bAgent, incident.incidentId(), "g1", "p1", "fake-model", "d1"));
    }

    @Test
    @DisplayName("不能读取或消费其它租户的审批")
    void cannotTouchForeignApproval() {
        AuthenticatedCaller aOperator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller aAgent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller bAgent = caller(tenantB, "AGENT_RUNTIME");

        Incident incident = incidentService.create(aOperator, "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(aAgent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        var approval = approvalService.request(aAgent, run.runId(),
                "restart_synthetic_service", "svc:synthetic-report", Map.of("service", "synthetic-report"));

        assertThrows(ResourceNotFoundException.class, () ->
                approvalService.verifyAndConsume(bAgent, approval.approvalId(),
                        "restart_synthetic_service", "svc:synthetic-report",
                        Map.of("service", "synthetic-report")));
    }

    @Test
    @DisplayName("待审批列表只包含本租户")
    void pendingListIsScoped() {
        AuthenticatedCaller aOperator = caller(tenantA, "OPERATOR");
        AuthenticatedCaller aAgent = caller(tenantA, "AGENT_RUNTIME");
        AuthenticatedCaller bApprover = caller(tenantB, "APPROVER");

        Incident incident = incidentService.create(aOperator, "synthetic-lab", "P2", "t", Instant.now());
        var run = runService.create(aAgent, incident.incidentId(), "g1", "p1", "fake-model", "d1");
        approvalService.request(aAgent, run.runId(), "restart_synthetic_service",
                "svc:synthetic-report", Map.of("service", "synthetic-report"));

        assertTrue(approvalService.listPending(bApprover).isEmpty(),
                "tenant B 的审批人不应看到 tenant A 的待审批");
    }
}
