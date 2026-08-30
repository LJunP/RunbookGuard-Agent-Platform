package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.domain.EvidenceReference;
import io.runbookguard.controlplane.domain.RunStep;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.TraceService;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
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
 * Trace 的读写端点。控制台的时间线与证据视图读 GET，Agent Runtime 上报走 POST。
 *
 * <p>没有 PUT/DELETE：一个已发生的步骤不该能被改写。
 */
@RestController
@RequestMapping("/api/v1/runs/{runId}/trace")
public class TraceController {

    private final TraceService traceService;

    public TraceController(TraceService traceService) {
        this.traceService = traceService;
    }

    @GetMapping
    public TraceResponse get(AuthenticatedCaller caller, @PathVariable String runId) {
        TraceService.Trace trace = traceService.get(caller, runId);
        return new TraceResponse(
                Dtos.RunResponse.from(trace.run()),
                trace.terminalStatus(),
                trace.steps().stream().map(StepResponse::from).toList(),
                trace.evidence().stream().map(EvidenceResponse::from).toList(),
                trace.approvals().stream().map(Dtos.ApprovalResponse::from).toList());
    }

    @PostMapping
    public RecordResponse record(AuthenticatedCaller caller, @PathVariable String runId,
                                 @Valid @RequestBody RecordRequest request) {
        TraceService.RecordOutcome outcome = traceService.record(
                caller, runId,
                request.steps().stream().map(StepRequest::toInput).toList(),
                request.evidence().stream().map(EvidenceRequest::toInput).toList());
        return new RecordResponse(runId, outcome.stepsSubmitted(), outcome.stepsWritten(),
                outcome.evidenceSubmitted(), outcome.evidenceWritten());
    }

    public record RecordRequest(
            @NotNull @Size(max = 500) List<@Valid StepRequest> steps,
            @NotNull @Size(max = 500) List<@Valid EvidenceRequest> evidence) {
    }

    public record StepRequest(
            @Min(1) int sequence,
            @NotBlank @Size(max = 64) String nodeName,
            @Size(max = 512) String inputArtifact,
            @Size(max = 512) String outputArtifact,
            @Size(max = 64) String toolCallId,
            @NotBlank @Size(max = 32) String status,
            @Size(max = 64) String failureClass,
            Instant startedAt,
            Instant finishedAt) {

        TraceService.StepInput toInput() {
            return new TraceService.StepInput(sequence, nodeName, inputArtifact, outputArtifact,
                    toolCallId, status, failureClass, startedAt, finishedAt);
        }
    }

    public record EvidenceRequest(
            @NotBlank @Size(max = 64) String evidenceId,
            @NotBlank @Size(max = 32) String sourceType,
            @NotBlank @Size(max = 256) String sourceIdentity,
            @NotBlank @Size(max = 64) String version,
            @NotBlank @Size(max = 512) String location,
            @NotBlank @Size(min = 64, max = 64) String contentHash,
            Instant capturedAt) {

        TraceService.EvidenceInput toInput() {
            return new TraceService.EvidenceInput(evidenceId, sourceType, sourceIdentity,
                    version, location, contentHash, capturedAt);
        }
    }

    /**
     * written 与 submitted 分开返回：两者不等说明这批里有重投。
     * 只回一个 count 会让「消息投递三次」变得不可观测。
     */
    public record RecordResponse(String runId, int stepsSubmitted, int stepsWritten,
                                 int evidenceSubmitted, int evidenceWritten) {
    }

    public record TraceResponse(
            Dtos.RunResponse run,
            String terminalStatus,
            List<StepResponse> steps,
            List<EvidenceResponse> evidence,
            List<Dtos.ApprovalResponse> approvals) {
    }

    public record StepResponse(
            int sequence,
            String nodeName,
            String status,
            String toolCallId,
            String failureClass,
            String inputArtifact,
            String outputArtifact,
            Instant startedAt,
            Instant finishedAt) {

        static StepResponse from(RunStep s) {
            return new StepResponse(s.sequence(), s.nodeName(), s.status(), s.toolCallId(),
                    s.failureClass(), s.inputArtifact(), s.outputArtifact(),
                    s.startedAt(), s.finishedAt());
        }
    }

    public record EvidenceResponse(
            String evidenceId,
            String sourceType,
            String sourceIdentity,
            String version,
            String location,
            String contentHash,
            Instant capturedAt) {

        static EvidenceResponse from(EvidenceReference e) {
            return new EvidenceResponse(e.evidenceId(), e.sourceType(), e.sourceIdentity(),
                    e.version(), e.location(), e.contentHash(), e.capturedAt());
        }
    }
}
