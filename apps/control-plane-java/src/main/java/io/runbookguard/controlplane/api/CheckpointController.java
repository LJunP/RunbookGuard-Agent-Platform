package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.domain.CheckpointMeta;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.TraceService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.Instant;
import java.util.List;

/**
 * checkpoint 元数据端点。
 *
 * <p>路径挂在 run 下而不是 /trace 下：checkpoint 是恢复机制的产物，
 * Trace 是审计视图，两者相关但不是包含关系。
 * 控制面只存元数据（id、序号、摘要），状态本体在 Runtime 的 checkpointer 里。
 */
@RestController
@RequestMapping("/api/v1/runs/{runId}/checkpoints")
public class CheckpointController {

    private final TraceService traceService;

    public CheckpointController(TraceService traceService) {
        this.traceService = traceService;
    }

    @PostMapping
    public CheckpointRecordResponse record(
            AuthenticatedCaller caller, @PathVariable String runId,
            @Valid @RequestBody CheckpointRecordRequest request) {
        TraceService.CheckpointOutcome outcome = traceService.recordCheckpoints(
                caller, runId,
                request.checkpoints().stream()
                        .map(c -> c.toInput(request.graphVersion(), request.stateSchemaVersion()))
                        .toList());
        return new CheckpointRecordResponse(runId, outcome.submitted(), outcome.written());
    }

    @GetMapping
    public List<CheckpointResponse> list(
            AuthenticatedCaller caller, @PathVariable String runId) {
        return traceService.listCheckpoints(caller, runId).stream()
                .map(CheckpointResponse::from).toList();
    }

    public record CheckpointRecordRequest(
            @NotBlank @Size(max = 32) String graphVersion,
            @NotBlank @Size(max = 32) String stateSchemaVersion,
            @NotEmpty @Size(max = 200) List<@Valid CheckpointRequest> checkpoints) {
    }

    public record CheckpointRequest(
            @NotBlank @Size(max = 64) String checkpointId,
            @Min(1) int sequence,
            @NotBlank @Size(min = 64, max = 64) String stateDigest,
            @Size(max = 512) String stateLocation) {

        TraceService.CheckpointInput toInput(String graphVersion, String stateSchemaVersion) {
            return new TraceService.CheckpointInput(checkpointId, graphVersion,
                    stateSchemaVersion, sequence, stateDigest, stateLocation);
        }
    }

    public record CheckpointRecordResponse(String runId, int submitted, int written) {
    }

    public record CheckpointResponse(
            String checkpointId,
            String graphVersion,
            String stateSchemaVersion,
            int sequence,
            String stateDigest,
            String stateLocation,
            Instant createdAt) {

        static CheckpointResponse from(CheckpointMeta m) {
            return new CheckpointResponse(m.checkpointId(), m.graphVersion(),
                    m.stateSchemaVersion(), m.sequence(), m.stateDigest(),
                    m.stateLocation(), m.createdAt());
        }
    }
}
