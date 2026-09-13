package io.runbookguard.controlplane.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.runbookguard.controlplane.AbstractIntegrationTest;
import io.runbookguard.controlplane.domain.IncidentStatus;
import io.runbookguard.controlplane.domain.RunStatus;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;

import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * HTTP 层的成功与失败路径。M1 Gate 要求"成功路径和失败路径 API 都可重复复现"，
 * 所以每条失败路径都断言具体状态码与错误码，而不只断言"抛异常了"。
 */
@AutoConfigureMockMvc
@TestPropertySource(properties = "runbookguard.ratelimit.incident-create-per-minute=5")
class HttpApiIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    MockMvc mvc;
    @Autowired
    ObjectMapper json;

    private String bearer(CallerWithToken c) {
        return "Bearer " + c.token();
    }

    @Test
    @DisplayName("无凭据 -> 401 unauthenticated")
    void missingCredentialsYields401() throws Exception {
        mvc.perform(get("/api/v1/incidents"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.error").value("unauthenticated"));
    }

    @Test
    @DisplayName("GET /api/v1/runs?incidentId= 返回该 Incident 的 Run（控制台主流程）")
    void listRunsByIncidentReturnsCreatedRun() throws Exception {
        // 回归：listByIncident 的 SQL 曾因 Java 文本块剥掉行尾空格而成为
        // "SELECTrun_id"，只有控制台点击 Incident 才触发，测试从未覆盖，
        // 从 M1 坏到 M7（控制台 500）。
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        String incidentId = createIncident(operator);

        mvc.perform(post("/api/v1/runs")
                        .header("Authorization", bearer(operator))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "incidentId", incidentId, "graphVersion", "langgraph-v1",
                                "promptVersion", "p1", "modelId", "fake-model",
                                "datasetVersion", "incidents-dev"))))
                .andExpect(status().isCreated());

        mvc.perform(get("/api/v1/runs")
                        .header("Authorization", bearer(operator))
                        .queryParam("incidentId", incidentId))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].incidentId").value(incidentId))
                .andExpect(jsonPath("$[0].graphVersion").value("langgraph-v1"));
    }

    @Test
    @DisplayName("不存在的路径 -> 404 not_found，而不是 500")
    void unknownPathYields404() throws Exception {
        // @ExceptionHandler(Exception.class) 会把 NoResourceFoundException 吞成
        // internal_error。5xx 告警被「打错路径」淹没，与控制面真的坏了无法区分。
        mvc.perform(get("/no-such-path-at-all"))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.error").value("not_found"));
    }

    @Test
    @DisplayName("伪造 token -> 401")
    void invalidTokenYields401() throws Exception {
        mvc.perform(get("/api/v1/incidents").header("Authorization", "Bearer forged-token"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    @DisplayName("VIEWER 创建 Incident -> 403 forbidden")
    void viewerCannotCreateIncident() throws Exception {
        CallerWithToken viewer = callerWithToken(tenantA, "VIEWER");
        mvc.perform(post("/api/v1/incidents")
                        .header("Authorization", bearer(viewer))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "source", "synthetic-lab", "severity", "P2", "title", "t"))))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.error").value("forbidden"));
    }

    @Test
    @DisplayName("非法 severity -> 400 validation_failed")
    void invalidSeverityYields400() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        mvc.perform(post("/api/v1/incidents")
                        .header("Authorization", bearer(operator))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "source", "synthetic-lab", "severity", "CRITICAL", "title", "t"))))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.error").value("validation_failed"));
    }

    @Test
    @DisplayName("成功路径：创建 Incident -> 201，可重复读取")
    void createAndReadIncident() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        MvcResult created = mvc.perform(post("/api/v1/incidents")
                        .header("Authorization", bearer(operator))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "source", "synthetic-lab", "severity", "P1",
                                "title", "connection pool exhausted"))))
                .andExpect(status().isCreated())
                .andExpect(jsonPath("$.currentStatus").value("OPEN"))
                .andExpect(jsonPath("$.version").value(1))
                .andReturn();

        String incidentId = json.readTree(created.getResponse().getContentAsString())
                .get("incidentId").asText();

        mvc.perform(get("/api/v1/incidents/" + incidentId).header("Authorization", bearer(operator)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.title").value("connection pool exhausted"));
    }

    @Test
    @DisplayName("跨租户读取 -> 404，且不泄漏资源存在")
    void crossTenantReadYields404() throws Exception {
        CallerWithToken a = callerWithToken(tenantA, "OPERATOR");
        CallerWithToken b = callerWithToken(tenantB, "OPERATOR");
        String incidentId = createIncident(a);

        mvc.perform(get("/api/v1/incidents/" + incidentId).header("Authorization", bearer(b)))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.error").value("not_found"));
    }

    @Test
    @DisplayName("版本过期的状态更新 -> 409 version_conflict")
    void staleVersionYields409() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        String incidentId = createIncident(operator);

        mvc.perform(patch("/api/v1/incidents/" + incidentId + "/status")
                        .header("Authorization", bearer(operator))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "status", IncidentStatus.DIAGNOSING, "expectedVersion", 1))))
                .andExpect(status().isOk());

        mvc.perform(patch("/api/v1/incidents/" + incidentId + "/status")
                        .header("Authorization", bearer(operator))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "status", IncidentStatus.MITIGATING, "expectedVersion", 1))))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.error").value("version_conflict"));
    }

    @Test
    @DisplayName("超过限流 -> 429 rate_limited")
    void rateLimitYields429() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        for (int i = 0; i < 5; i++) {
            createIncident(operator);
        }
        mvc.perform(post("/api/v1/incidents")
                        .header("Authorization", bearer(operator))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "source", "synthetic-lab", "severity", "P2", "title", "over"))))
                .andExpect(status().isTooManyRequests())
                .andExpect(jsonPath("$.error").value("rate_limited"));
    }

    @Test
    @DisplayName("审批后篡改参数 -> 403 approval_verification_failed（HTTP 层验证 T-2）")
    void tamperedArgumentsYields403() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        CallerWithToken agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        CallerWithToken approver = callerWithToken(tenantA, "APPROVER");

        String incidentId = createIncident(operator);
        String runId = createRun(agent, incidentId);
        String approvalId = requestApproval(agent, runId,
                Map.of("service", "synthetic-orders", "target_version", "v1.4.2"));

        mvc.perform(post("/api/v1/approvals/" + approvalId + "/decision")
                        .header("Authorization", bearer(approver))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("approve", true, "reason", "ok"))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.decision").value("APPROVED"));

        mvc.perform(post("/api/v1/approvals/" + approvalId + "/consume")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "toolName", "rollback_synthetic_deployment",
                                "resourceRef", "svc:synthetic-orders",
                                "arguments", Map.of("service", "synthetic-db",
                                        "target_version", "v1.4.2")))))
                .andExpect(status().isForbidden())
                .andExpect(jsonPath("$.error").value("approval_verification_failed"));
    }

    @Test
    @DisplayName("完整审批链路成功路径：request -> decide -> consume")
    void fullApprovalHappyPath() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        CallerWithToken agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        CallerWithToken approver = callerWithToken(tenantA, "APPROVER");

        String incidentId = createIncident(operator);
        String runId = createRun(agent, incidentId);
        Map<String, Object> args = Map.of("service", "synthetic-orders", "target_version", "v1.4.2");
        String approvalId = requestApproval(agent, runId, args);

        mvc.perform(get("/api/v1/approvals/pending").header("Authorization", bearer(approver)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].approvalId").value(approvalId));

        mvc.perform(post("/api/v1/approvals/" + approvalId + "/decision")
                        .header("Authorization", bearer(approver))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("approve", true))))
                .andExpect(status().isOk());

        mvc.perform(post("/api/v1/approvals/" + approvalId + "/consume")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "toolName", "rollback_synthetic_deployment",
                                "resourceRef", "svc:synthetic-orders",
                                "arguments", args))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.consumedAt").exists());
    }

    @Test
    @DisplayName("终态结算幂等：第二次返回 firstSettlement=false")
    void terminalSettlementIsIdempotentOverHttp() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        CallerWithToken agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(agent, createIncident(operator));

        mvc.perform(post("/api/v1/runs/" + runId + "/terminal")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("terminalStatus", RunStatus.COMPLETE))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.firstSettlement").value(true));

        mvc.perform(post("/api/v1/runs/" + runId + "/terminal")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "terminalStatus", RunStatus.FAILED,
                                "failureClass", "deadline_exceeded"))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.firstSettlement").value(false))
                .andExpect(jsonPath("$.terminalStatus").value("COMPLETE"));
    }

    @Test
    @DisplayName("浮点参数被拒绝 -> 400 invalid_arguments（ADR-0002）")
    void floatArgumentsYield400() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        CallerWithToken agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(agent, createIncident(operator));

        mvc.perform(post("/api/v1/approvals")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("""
                                {"runId":"%s","toolName":"throttle_synthetic_traffic",
                                 "resourceRef":"svc:synthetic-notify",
                                 "arguments":{"rate_percent":12.5}}
                                """.formatted(runId)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.error").value("invalid_arguments"));
    }

    @Test
    @DisplayName("错误响应不含 token、密码等凭据")
    void errorResponsesDoNotLeakSecrets() throws Exception {
        MvcResult result = mvc.perform(get("/api/v1/incidents")
                        .header("Authorization", "Bearer super-secret-token-value-12345"))
                .andExpect(status().isUnauthorized())
                .andReturn();
        String body = result.getResponse().getContentAsString();
        assertFalse(body.contains("super-secret-token-value-12345"), body);
    }

    @Test
    @DisplayName("审计端点可查到刚才的操作，且只含本租户")
    void auditEndpointIsScopedAndPopulated() throws Exception {
        CallerWithToken a = callerWithToken(tenantA, "OPERATOR");
        CallerWithToken b = callerWithToken(tenantB, "OPERATOR");
        createIncident(a);

        MvcResult resA = mvc.perform(get("/api/v1/audit-events?limit=50")
                        .header("Authorization", bearer(a)))
                .andExpect(status().isOk())
                .andReturn();
        var eventsA = json.readTree(resA.getResponse().getContentAsString());
        assertTrue(eventsA.size() > 0, "应有审计事件");
        eventsA.forEach(e -> assertEquals(tenantA, e.get("tenantId").asText()));

        MvcResult resB = mvc.perform(get("/api/v1/audit-events?limit=50")
                        .header("Authorization", bearer(b)))
                .andExpect(status().isOk())
                .andReturn();
        json.readTree(resB.getResponse().getContentAsString())
                .forEach(e -> assertEquals(tenantB, e.get("tenantId").asText()));
    }

    @Test
    @DisplayName("actuator 已迁到管理端口：业务口上 /actuator 返回 404")
    void actuatorIsNotOnBusinessPort() throws Exception {
        // M7 §7.5 整改：management.server.port 分离后，主 servlet 不再挂 actuator。
        // 管理口本身的 200 由 compose 冒烟（scripts/smoke-m7.sh）验证——MockMvc
        // 只绑主端口的 servlet，测不到 9080。
        mvc.perform(get("/actuator/health")).andExpect(status().isNotFound());
    }

    private String createIncident(CallerWithToken caller) throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/incidents")
                        .header("Authorization", bearer(caller))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "source", "synthetic-lab", "severity", "P2", "title", "t"))))
                .andExpect(status().isCreated())
                .andReturn();
        return json.readTree(r.getResponse().getContentAsString()).get("incidentId").asText();
    }

    private String createRun(CallerWithToken caller, String incidentId) throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/runs")
                        .header("Authorization", bearer(caller))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "incidentId", incidentId, "graphVersion", "g1",
                                "promptVersion", "p1", "modelId", "fake-provider",
                                "datasetVersion", "d1"))))
                .andExpect(status().isCreated())
                .andReturn();
        return json.readTree(r.getResponse().getContentAsString()).get("runId").asText();
    }

    private String requestApproval(CallerWithToken caller, String runId,
                                   Map<String, Object> arguments) throws Exception {
        MvcResult r = mvc.perform(post("/api/v1/approvals")
                        .header("Authorization", bearer(caller))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "runId", runId,
                                "toolName", "rollback_synthetic_deployment",
                                "resourceRef", "svc:synthetic-orders",
                                "arguments", arguments))))
                .andExpect(status().isCreated())
                .andReturn();
        return json.readTree(r.getResponse().getContentAsString()).get("approvalId").asText();
    }
}
