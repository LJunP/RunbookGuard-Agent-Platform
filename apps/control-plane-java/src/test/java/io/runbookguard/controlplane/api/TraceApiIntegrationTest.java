package io.runbookguard.controlplane.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.runbookguard.controlplane.AbstractIntegrationTest;
import io.runbookguard.controlplane.domain.RunStatus;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import java.util.List;
import java.util.Map;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * Trace 端点的读写。M7 的控制台完全依赖它，因此这里的失败路径比成功路径更重要：
 * 一个能被改写的 Trace 等于没有 Trace。
 */
@AutoConfigureMockMvc
class TraceApiIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    MockMvc mvc;
    @Autowired
    ObjectMapper json;

    private String bearer(CallerWithToken c) {
        return "Bearer " + c.token();
    }

    private String createRun(CallerWithToken caller) throws Exception {
        String incidentBody = mvc.perform(post("/api/v1/incidents")
                        .header("Authorization", bearer(caller))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "source", "synthetic-lab", "severity", "P2",
                                "title", "trace test"))))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        String incidentId = json.readTree(incidentBody).get("incidentId").asText();

        String runBody = mvc.perform(post("/api/v1/runs")
                        .header("Authorization", bearer(caller))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "incidentId", incidentId, "graphVersion", "langgraph-v1",
                                "promptVersion", "p1", "modelId", "fake-model",
                                "datasetVersion", "incidents-dev"))))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        return json.readTree(runBody).get("runId").asText();
    }

    private Map<String, Object> step(int sequence, String node, String status) {
        return Map.of("sequence", sequence, "nodeName", node, "status", status);
    }

    private Map<String, Object> evidence(String id, String hash) {
        return Map.of("evidenceId", id, "sourceType", "get_service_metrics",
                "sourceIdentity", "service:synthetic-orders", "version", "v1",
                "location", "lab:/v1/metrics", "contentHash", hash);
    }

    @Test
    @DisplayName("上报后可读回完整时间线")
    void recordThenReadBack() throws Exception {
        CallerWithToken runtime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(runtime);

        mvc.perform(post("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "steps", List.of(
                                        step(1, "COLLECT_CONTEXT", "COMPLETED"),
                                        step(2, "EXECUTING_TOOL", "COMPLETED")),
                                "evidence", List.of(evidence("ev-abc123456789", "a".repeat(64)))))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.stepsWritten").value(2))
                .andExpect(jsonPath("$.evidenceWritten").value(1));

        mvc.perform(get("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.run.runId").value(runId))
                .andExpect(jsonPath("$.steps.length()").value(2))
                .andExpect(jsonPath("$.steps[0].nodeName").value("COLLECT_CONTEXT"))
                .andExpect(jsonPath("$.evidence.length()").value(1))
                .andExpect(jsonPath("$.evidence[0].contentHash").value("a".repeat(64)));
    }

    @Test
    @DisplayName("重复上报同一 sequence 不产生重复步骤，且 written 与 submitted 不等")
    void duplicateSubmissionIsIdempotent() throws Exception {
        CallerWithToken runtime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(runtime);
        String body = json.writeValueAsString(Map.of(
                "steps", List.of(step(1, "COLLECT_CONTEXT", "COMPLETED")),
                "evidence", List.of(evidence("ev-dedup000001", "b".repeat(64)))));

        mvc.perform(post("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(jsonPath("$.stepsWritten").value(1));

        // 第二次投递：submitted 仍是 1，written 是 0。两者相等会让重投不可观测。
        mvc.perform(post("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.stepsSubmitted").value(1))
                .andExpect(jsonPath("$.stepsWritten").value(0))
                .andExpect(jsonPath("$.evidenceWritten").value(0));

        mvc.perform(get("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime)))
                .andExpect(jsonPath("$.steps.length()").value(1))
                .andExpect(jsonPath("$.evidence.length()").value(1));
    }

    @Test
    @DisplayName("步骤里的凭据被脱敏（M0 INV-6）")
    void secretsInArtifactsAreRedacted() throws Exception {
        CallerWithToken runtime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(runtime);

        mvc.perform(post("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "steps", List.of(Map.of(
                                        "sequence", 1, "nodeName", "OBSERVE", "status", "COMPLETED",
                                        "outputArtifact", "upstream said api_key=sk-abcdefghijklmnopqrst")),
                                "evidence", List.of()))))
                .andExpect(status().isOk());

        String body = mvc.perform(get("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime)))
                .andReturn().getResponse().getContentAsString();
        org.junit.jupiter.api.Assertions.assertFalse(
                body.contains("sk-abcdefghijklmnopqrst"),
                "Trace 里出现了未脱敏的凭据");
        org.junit.jupiter.api.Assertions.assertTrue(body.contains("REDACTED"));
    }

    @Test
    @DisplayName("非法 failureClass -> 400，自由文本不进库")
    void unknownFailureClassIsRejected() throws Exception {
        CallerWithToken runtime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(runtime);

        mvc.perform(post("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "steps", List.of(Map.of(
                                        "sequence", 1, "nodeName", "FAILED", "status", "FAILED",
                                        "failureClass", "something_went_wrong")),
                                "evidence", List.of()))))
                .andExpect(status().isBadRequest());
    }

    @Test
    @DisplayName("VIEWER 不能上报 Trace")
    void viewerCannotRecord() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        String runId = createRun(operator);
        CallerWithToken viewer = callerWithToken(tenantA, "VIEWER");

        mvc.perform(post("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(viewer))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "steps", List.of(step(1, "COLLECT_CONTEXT", "COMPLETED")),
                                "evidence", List.of()))))
                .andExpect(status().isForbidden());
    }

    @Test
    @DisplayName("VIEWER 可以读 Trace —— 它是只读视图")
    void viewerCanRead() throws Exception {
        CallerWithToken operator = callerWithToken(tenantA, "OPERATOR");
        String runId = createRun(operator);
        CallerWithToken viewer = callerWithToken(tenantA, "VIEWER");

        mvc.perform(get("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(viewer)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.run.runId").value(runId));
    }

    @Test
    @DisplayName("跨租户读 Trace -> 404，不泄漏存在性")
    void crossTenantReadIsNotFound() throws Exception {
        CallerWithToken ownerRuntime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(ownerRuntime);
        CallerWithToken intruder = callerWithToken(tenantB, "OPERATOR");

        mvc.perform(get("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(intruder)))
                .andExpect(status().isNotFound());
    }

    @Test
    @DisplayName("跨租户上报 Trace -> 404")
    void crossTenantRecordIsNotFound() throws Exception {
        CallerWithToken ownerRuntime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(ownerRuntime);
        CallerWithToken intruder = callerWithToken(tenantB, "AGENT_RUNTIME");

        mvc.perform(post("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(intruder))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "steps", List.of(step(1, "COLLECT_CONTEXT", "COMPLETED")),
                                "evidence", List.of()))))
                .andExpect(status().isNotFound());
    }

    @Test
    @DisplayName("checkpoint 元数据：上报、幂等、读回")
    void checkpointMetadataRoundTrip() throws Exception {
        CallerWithToken runtime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(runtime);
        String digest = "c".repeat(64);

        String req = json.writeValueAsString(Map.of(
                "graphVersion", "langgraph-v1",
                "stateSchemaVersion", "1",
                "checkpoints", List.of(
                        Map.of("checkpointId", "ckpt-0001", "sequence", 1,
                                "stateDigest", digest, "stateLocation", "sqlite://checkpoints.db"),
                        Map.of("checkpointId", "ckpt-0002", "sequence", 2,
                                "stateDigest", digest))));

        mvc.perform(post("/api/v1/runs/" + runId + "/checkpoints")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON).content(req))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.submitted").value(2))
                .andExpect(jsonPath("$.written").value(2));

        // 重投：幂等跳过。submitted 与 written 不等使重投可观测。
        mvc.perform(post("/api/v1/runs/" + runId + "/checkpoints")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON).content(req))
                .andExpect(jsonPath("$.submitted").value(2))
                .andExpect(jsonPath("$.written").value(0));

        mvc.perform(get("/api/v1/runs/" + runId + "/checkpoints")
                        .header("Authorization", bearer(runtime)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(2))
                .andExpect(jsonPath("$[0].checkpointId").value("ckpt-0001"))
                .andExpect(jsonPath("$[0].graphVersion").value("langgraph-v1"))
                .andExpect(jsonPath("$[1].sequence").value(2));
    }

    @Test
    @DisplayName("checkpoint 元数据：跨租户 404，VIEWER 不能写")
    void checkpointMetadataAccessControl() throws Exception {
        CallerWithToken owner = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(owner);
        CallerWithToken intruder = callerWithToken(tenantB, "AGENT_RUNTIME");
        CallerWithToken viewer = callerWithToken(tenantA, "VIEWER");

        // 带一条合法条目：@NotEmpty 会先于 RBAC 拦截空列表，
        // 那样测到的是校验层而不是租户隔离。
        String oneCkpt = json.writeValueAsString(Map.of(
                "graphVersion", "langgraph-v1", "stateSchemaVersion", "1",
                "checkpoints", List.of(Map.of(
                        "checkpointId", "ckpt-t", "sequence", 1,
                        "stateDigest", "d".repeat(64)))));

        mvc.perform(post("/api/v1/runs/" + runId + "/checkpoints")
                        .header("Authorization", bearer(intruder))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(oneCkpt))
                .andExpect(status().isNotFound());

        mvc.perform(post("/api/v1/runs/" + runId + "/checkpoints")
                        .header("Authorization", bearer(viewer))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(oneCkpt))
                .andExpect(status().isForbidden());

        // stateDigest 长度错误 -> 400
        mvc.perform(post("/api/v1/runs/" + runId + "/checkpoints")
                        .header("Authorization", bearer(owner))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "graphVersion", "langgraph-v1", "stateSchemaVersion", "1",
                                "checkpoints", List.of(Map.of(
                                        "checkpointId", "ckpt-x", "sequence", 1,
                                        "stateDigest", "short"))))))
                .andExpect(status().isBadRequest());
    }

    @Test
    @DisplayName("Trace 含终态与审批链路")
    void traceIncludesTerminalStateAndApprovals() throws Exception {
        CallerWithToken runtime = callerWithToken(tenantA, "AGENT_RUNTIME,OPERATOR");
        String runId = createRun(runtime);

        mvc.perform(post("/api/v1/approvals")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "runId", runId,
                                "toolName", "rollback_synthetic_deployment",
                                "resourceRef", "svc:synthetic-orders",
                                "arguments", Map.of("service", "synthetic-orders",
                                        "target_version", "v1.4.2")))))
                .andExpect(status().isCreated());

        mvc.perform(post("/api/v1/runs/" + runId + "/terminal")
                        .header("Authorization", bearer(runtime))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "terminalStatus", RunStatus.COMPLETE.name()))))
                .andExpect(status().isOk());

        mvc.perform(get("/api/v1/runs/" + runId + "/trace")
                        .header("Authorization", bearer(runtime)))
                .andExpect(jsonPath("$.terminalStatus").value("COMPLETE"))
                .andExpect(jsonPath("$.approvals.length()").value(1))
                .andExpect(jsonPath("$.approvals[0].decision").value("PENDING"))
                // 审批的参数原文供人核对，但摘要必须一并给出，否则「参数没被改」无法验证。
                .andExpect(jsonPath("$.approvals[0].argumentsDigest").exists());
    }
}
