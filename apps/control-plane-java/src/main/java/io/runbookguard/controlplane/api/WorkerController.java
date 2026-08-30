package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.domain.Role;
import io.runbookguard.controlplane.domain.RunStatus;
import io.runbookguard.controlplane.messaging.FailureClass;
import io.runbookguard.controlplane.persistence.AgentRunMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import io.runbookguard.controlplane.worker.CancellationService;
import io.runbookguard.controlplane.worker.Lease;
import io.runbookguard.controlplane.worker.LeaseService;
import io.runbookguard.controlplane.config.RunbookGuardProperties;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;

/**
 * Worker 生命周期 API。Python Agent Runtime 通过它获取/续租 Lease 并上报进度。
 *
 * <p>每个写入端点都要求 fencingToken：Lease 过期被接管后，原 Worker 可能从 GC 暂停中
 * 恢复并继续上报，那些上报必须被拒绝（ADR-0003 §3）。
 */
@RestController
@RequestMapping("/api/v1/worker")
public class WorkerController {

    private final LeaseService leaseService;
    private final AgentRunService runService;
    private final CancellationService cancellation;
    private final AgentRunMapper runMapper;
    private final RunbookGuardProperties props;

    public WorkerController(LeaseService leaseService, AgentRunService runService,
                            CancellationService cancellation, AgentRunMapper runMapper,
                            RunbookGuardProperties props) {
        this.leaseService = leaseService;
        this.runService = runService;
        this.cancellation = cancellation;
        this.runMapper = runMapper;
        this.props = props;
    }

    public record AcquireRequest(@NotBlank String workerId) {
    }

    public record LeaseResponse(
            String runId, String ownerId, long fencingToken,
            Instant expiresAt, boolean acquired, boolean cancelRequested) {
    }

    public record HeartbeatRequest(@NotBlank String workerId, @Min(1) long fencingToken) {
    }

    public record ProgressRequest(
            @NotBlank String workerId,
            @Min(1) long fencingToken,
            @NotNull RunStatus status,
            String currentStep,
            @Min(0) long costSpentMicros,
            @Min(0) long tokenSpent,
            @Min(0) int toolCallCount,
            @Min(0) int stepsUsed) {
    }

    public record TerminalRequest(
            @NotBlank String workerId,
            @Min(1) long fencingToken,
            @NotNull RunStatus terminalStatus,
            String failureClass) {
    }

    @PostMapping("/runs/{runId}/lease")
    public LeaseResponse acquire(AuthenticatedCaller caller, @PathVariable String runId,
                                 @Valid @RequestBody AcquireRequest request) {
        requireAgentRuntime(caller);
        runService.get(caller, runId);
        return leaseService.acquire(runId, caller.tenantId(), request.workerId(),
                        props.worker().leaseTtl())
                .map(l -> toResponse(l, true, caller.tenantId()))
                .orElseGet(() -> new LeaseResponse(runId, null, 0, null, false, false));
    }

    @PostMapping("/runs/{runId}/lease/heartbeat")
    public LeaseResponse heartbeat(AuthenticatedCaller caller, @PathVariable String runId,
                                   @Valid @RequestBody HeartbeatRequest request) {
        requireAgentRuntime(caller);
        boolean renewed = leaseService.heartbeat(runId, request.workerId(),
                request.fencingToken(), props.worker().leaseTtl());
        if (!renewed) {
            // 心跳失败意味着 Lease 已被接管或释放。返回 acquired=false 让 Worker 立刻停手，
            // 而不是继续跑到下一个写入点才被 fencing 拦住。
            return new LeaseResponse(runId, request.workerId(), request.fencingToken(),
                    null, false, false);
        }
        Lease lease = leaseService.find(runId).orElseThrow();
        return toResponse(lease, true, caller.tenantId());
    }

    @PostMapping("/runs/{runId}/lease/release")
    public LeaseResponse release(AuthenticatedCaller caller, @PathVariable String runId,
                                 @Valid @RequestBody HeartbeatRequest request) {
        requireAgentRuntime(caller);
        boolean released = leaseService.release(runId, request.workerId(), request.fencingToken());
        return new LeaseResponse(runId, request.workerId(), request.fencingToken(),
                null, !released, false);
    }

    @PostMapping("/runs/{runId}/progress")
    public Dtos.RunResponse progress(AuthenticatedCaller caller, @PathVariable String runId,
                                     @Valid @RequestBody ProgressRequest request) {
        requireAgentRuntime(caller);
        leaseService.requireValidToken(runId, request.workerId(), request.fencingToken());
        var run = runService.get(caller, runId);
        if (run.status().isTerminal()) {
            throw new io.runbookguard.controlplane.service.TerminalStateAlreadySetException(
                    "run " + runId + " already terminal: " + run.status());
        }
        runMapper.applyProgress(runId, caller.tenantId(), request.status(), request.currentStep(),
                request.costSpentMicros(), request.tokenSpent(), request.toolCallCount(),
                request.stepsUsed(), Instant.now());
        return Dtos.RunResponse.from(runService.get(caller, runId));
    }

    @PostMapping("/runs/{runId}/terminal")
    public Dtos.TerminalOutcomeResponse settle(AuthenticatedCaller caller, @PathVariable String runId,
                                               @Valid @RequestBody TerminalRequest request) {
        requireAgentRuntime(caller);
        leaseService.requireValidToken(runId, request.workerId(), request.fencingToken());
        if (request.failureClass() != null) {
            // 提前校验取值合法，避免自由文本进入数据库让 M6 的失败归因统计失效。
            FailureClass.fromWire(request.failureClass());
        }
        var outcome = runService.settleTerminal(caller, runId, request.terminalStatus(),
                request.failureClass());
        return new Dtos.TerminalOutcomeResponse(outcome.terminalStatus(), outcome.firstSettlement());
    }

    @PostMapping("/runs/{runId}/cancel")
    public java.util.Map<String, Object> cancel(AuthenticatedCaller caller,
                                                @PathVariable String runId) {
        boolean accepted = cancellation.requestCancel(caller, runId);
        return java.util.Map.of("runId", runId, "cancelRequested", true, "firstRequest", accepted);
    }

    private LeaseResponse toResponse(Lease lease, boolean acquired, String tenantId) {
        return new LeaseResponse(lease.runId(), lease.ownerId(), lease.fencingToken(),
                lease.expiresAt(), acquired,
                cancellation.isCancelRequested(lease.runId(), tenantId));
    }

    private void requireAgentRuntime(AuthenticatedCaller caller) {
        if (!caller.principal().hasRole(Role.AGENT_RUNTIME)) {
            throw new ForbiddenException("principal lacks role AGENT_RUNTIME");
        }
    }
}
