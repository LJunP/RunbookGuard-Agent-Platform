package io.runbookguard.controlplane.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.runbookguard.controlplane.AbstractIntegrationTest;
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

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/** Worker 生命周期 API 的 HTTP 层验证，重点是 fencing token 在真实请求路径上生效。 */
@AutoConfigureMockMvc
@TestPropertySource(properties = "runbookguard.worker.lease-ttl=PT0.4S")
class WorkerApiIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    MockMvc mvc;
    @Autowired
    ObjectMapper json;

    private String bearer(CallerWithToken c) {
        return "Bearer " + c.token();
    }

    private String createRun(CallerWithToken operator, CallerWithToken agent) throws Exception {
        MvcResult inc = mvc.perform(post("/api/v1/incidents")
                        .header("Authorization", bearer(operator))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "source", "synthetic-lab", "severity", "P2", "title", "t"))))
                .andExpect(status().isCreated()).andReturn();
        String incidentId = json.readTree(inc.getResponse().getContentAsString())
                .get("incidentId").asText();

        MvcResult run = mvc.perform(post("/api/v1/runs")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "incidentId", incidentId, "graphVersion", "g1",
                                "promptVersion", "p1", "modelId", "fake", "datasetVersion", "d1"))))
                .andExpect(status().isCreated()).andReturn();
        return json.readTree(run.getResponse().getContentAsString()).get("runId").asText();
    }

    @Test
    @DisplayName("获取 Lease -> acquired=true，fencingToken=1")
    void acquireLease() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.acquired").value(true))
                .andExpect(jsonPath("$.fencingToken").value(1))
                .andExpect(jsonPath("$.cancelRequested").value(false));
    }

    @Test
    @DisplayName("第二个 Worker 在 Lease 有效期内 -> acquired=false")
    void secondWorkerCannotAcquire() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andExpect(jsonPath("$.acquired").value(true));

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w2"))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.acquired").value(false));
    }

    @Test
    @DisplayName("上报进度需要有效 fencing token，旧 token -> 409 stale_fencing_token")
    void staleTokenProgressYields409() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        MvcResult first = mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andReturn();
        long staleToken = json.readTree(first.getResponse().getContentAsString())
                .get("fencingToken").asLong();

        Thread.sleep(600);
        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w2"))))
                .andExpect(jsonPath("$.acquired").value(true));

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/progress")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "workerId", "w1", "fencingToken", staleToken,
                                "status", RunStatus.OBSERVE, "currentStep", "OBSERVE",
                                "costSpentMicros", 100, "tokenSpent", 50,
                                "toolCallCount", 1, "stepsUsed", 1))))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.error").value("stale_fencing_token"));
    }

    @Test
    @DisplayName("有效 token 上报进度 -> 200，预算消耗被记录")
    void validProgressIsAccepted() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        MvcResult lease = mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andReturn();
        long token = json.readTree(lease.getResponse().getContentAsString())
                .get("fencingToken").asLong();

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/progress")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "workerId", "w1", "fencingToken", token,
                                "status", RunStatus.OBSERVE, "currentStep", "OBSERVE",
                                "costSpentMicros", 1200, "tokenSpent", 800,
                                "toolCallCount", 2, "stepsUsed", 3))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("OBSERVE"))
                .andExpect(jsonPath("$.costSpentMicros").value(1200))
                .andExpect(jsonPath("$.stepsUsed").value(3));
    }

    @Test
    @DisplayName("非法 failureClass -> 400")
    void invalidFailureClassYields400() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        MvcResult lease = mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andReturn();
        long token = json.readTree(lease.getResponse().getContentAsString())
                .get("fencingToken").asLong();

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/terminal")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "workerId", "w1", "fencingToken", token,
                                "terminalStatus", RunStatus.FAILED,
                                "failureClass", "something_went_wrong"))))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.error").value("invalid_request"));
    }

    @Test
    @DisplayName("合法 failureClass 结算成功，重复结算幂等")
    void terminalWithValidFailureClass() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        MvcResult lease = mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andReturn();
        long token = json.readTree(lease.getResponse().getContentAsString())
                .get("fencingToken").asLong();

        String body = json.writeValueAsString(Map.of(
                "workerId", "w1", "fencingToken", token,
                "terminalStatus", RunStatus.FAILED, "failureClass", "insufficient_evidence"));

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/terminal")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.firstSettlement").value(true));

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/terminal")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.firstSettlement").value(false));
    }

    @Test
    @DisplayName("取消后 Lease 响应携带 cancelRequested=true，Worker 能在节点边界看到")
    void cancelIsVisibleThroughLease() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/cancel")
                        .header("Authorization", bearer(operator)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.firstRequest").value(true));

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andExpect(jsonPath("$.cancelRequested").value(true));
    }

    @Test
    @DisplayName("AGENT_RUNTIME 不能请求取消（取消是运维动作）")
    void agentCannotCancel() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/cancel")
                        .header("Authorization", bearer(agent)))
                .andExpect(status().isForbidden());
    }

    @Test
    @DisplayName("被接管后 heartbeat 返回 acquired=false，让 Worker 立刻停手")
    void heartbeatAfterTakeoverSignalsStop() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        MvcResult first = mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w1"))))
                .andReturn();
        long token = json.readTree(first.getResponse().getContentAsString())
                .get("fencingToken").asLong();

        Thread.sleep(600);
        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w2"))))
                .andExpect(jsonPath("$.acquired").value(true));

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease/heartbeat")
                        .header("Authorization", bearer(agent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of(
                                "workerId", "w1", "fencingToken", token))))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.acquired").value(false));
    }

    @Test
    @DisplayName("跨租户获取 Lease -> 404")
    void crossTenantLeaseYields404() throws Exception {
        var operator = callerWithToken(tenantA, "OPERATOR");
        var agent = callerWithToken(tenantA, "AGENT_RUNTIME");
        var foreignAgent = callerWithToken(tenantB, "AGENT_RUNTIME");
        String runId = createRun(operator, agent);

        mvc.perform(post("/api/v1/worker/runs/" + runId + "/lease")
                        .header("Authorization", bearer(foreignAgent))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(json.writeValueAsString(Map.of("workerId", "w-foreign"))))
                .andExpect(status().isNotFound());
    }
}
