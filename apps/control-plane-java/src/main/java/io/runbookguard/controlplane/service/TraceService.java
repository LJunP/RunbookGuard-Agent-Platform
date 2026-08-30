package io.runbookguard.controlplane.service;

import io.runbookguard.controlplane.api.ForbiddenException;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.Approval;
import io.runbookguard.controlplane.domain.EvidenceReference;
import io.runbookguard.controlplane.domain.Role;
import io.runbookguard.controlplane.domain.RunStep;
import io.runbookguard.controlplane.messaging.FailureClass;
import io.runbookguard.controlplane.persistence.ApprovalMapper;
import io.runbookguard.controlplane.persistence.EvidenceReferenceMapper;
import io.runbookguard.controlplane.persistence.RunStepMapper;
import io.runbookguard.controlplane.persistence.RunTerminalStateMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.security.SecretRedactor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.UUID;

/**
 * Trace 的读写。控制台的时间线、证据与引用视图都读这里。
 *
 * <p>Trace 是 Java 侧持有的业务事实，不是 Python 侧的内存对象：Worker 崩溃后控制台
 * 仍要能回答「它当时看到了什么、被拒绝了什么」。Python 的 evaluation/trace.py 是
 * 评测用的行为摘要，两者用途不同，刻意不共用一套结构。
 */
@Service
public class TraceService {

    private final RunStepMapper stepMapper;
    private final EvidenceReferenceMapper evidenceMapper;
    private final ApprovalMapper approvalMapper;
    private final RunTerminalStateMapper terminalMapper;
    private final AgentRunService runService;
    private final AuditService audit;
    private final Clock clock;

    public TraceService(RunStepMapper stepMapper, EvidenceReferenceMapper evidenceMapper,
                        ApprovalMapper approvalMapper, RunTerminalStateMapper terminalMapper,
                        AgentRunService runService, AuditService audit, Clock clock) {
        this.stepMapper = stepMapper;
        this.evidenceMapper = evidenceMapper;
        this.approvalMapper = approvalMapper;
        this.terminalMapper = terminalMapper;
        this.runService = runService;
        this.audit = audit;
        this.clock = clock;
    }

    /**
     * 记录一批步骤与证据。
     *
     * <p>幂等：重复上报同一 sequence 会被唯一键挡住并跳过。返回实际写入的条数，
     * 让调用方能区分「全部是新的」与「这批全是重投」——两者都不是错误，
     * 但混同会让「消息投递三次」这件事变得不可观测。
     */
    @Transactional
    public RecordOutcome record(AuthenticatedCaller caller, String runId,
                                List<StepInput> steps, List<EvidenceInput> evidence) {
        requireAnyRole(caller, Role.AGENT_RUNTIME, Role.OPERATOR);
        AgentRun run = runService.get(caller, runId);

        int stepsWritten = 0;
        for (StepInput input : steps) {
            if (input.failureClass() != null) {
                // 提前校验取值合法：自由文本进了库，M6 的失败归因统计就失效了。
                FailureClass.fromWire(input.failureClass());
            }
            stepsWritten += stepMapper.insertIfAbsent(new RunStep(
                    "stp-" + UUID.randomUUID(), run.runId(), input.sequence(),
                    input.nodeName(),
                    // 步骤的输入输出可能带上工具结果里的内容，出口集中脱敏（威胁 T-3）。
                    SecretRedactor.redact(input.inputArtifact()),
                    SecretRedactor.redact(input.outputArtifact()),
                    input.toolCallId(), input.status(), input.failureClass(),
                    input.startedAt() != null ? input.startedAt() : clock.instant(),
                    input.finishedAt()));
        }

        int evidenceWritten = 0;
        for (EvidenceInput input : evidence) {
            evidenceWritten += evidenceMapper.insertIfAbsent(new EvidenceReference(
                    input.evidenceId(), run.runId(), caller.tenantId(), input.sourceType(),
                    SecretRedactor.redact(input.sourceIdentity()), input.version(),
                    SecretRedactor.redact(input.location()), input.contentHash(),
                    input.capturedAt() != null ? input.capturedAt() : clock.instant()));
        }

        audit.allowed(caller.tenantId(), caller.principalId(), "trace.record", "run", runId,
                "{\"stepsSubmitted\":%d,\"stepsWritten\":%d,\"evidenceSubmitted\":%d,\"evidenceWritten\":%d}"
                        .formatted(steps.size(), stepsWritten, evidence.size(), evidenceWritten));
        return new RecordOutcome(steps.size(), stepsWritten, evidence.size(), evidenceWritten);
    }

    /** 控制台读取完整 Trace。VIEWER 就够——它是只读视图。 */
    @Transactional(readOnly = true)
    public Trace get(AuthenticatedCaller caller, String runId) {
        AgentRun run = runService.get(caller, runId);
        return new Trace(
                run,
                stepMapper.listByRun(runId),
                evidenceMapper.listByRunInTenant(runId, caller.tenantId()),
                approvalMapper.listByRun(runId, caller.tenantId()),
                terminalMapper.findTerminalStatus(runId));
    }

    public record StepInput(
            int sequence,
            String nodeName,
            String inputArtifact,
            String outputArtifact,
            String toolCallId,
            String status,
            String failureClass,
            Instant startedAt,
            Instant finishedAt) {
    }

    public record EvidenceInput(
            String evidenceId,
            String sourceType,
            String sourceIdentity,
            String version,
            String location,
            String contentHash,
            Instant capturedAt) {
    }

    public record RecordOutcome(int stepsSubmitted, int stepsWritten,
                                int evidenceSubmitted, int evidenceWritten) {
    }

    public record Trace(AgentRun run, List<RunStep> steps, List<EvidenceReference> evidence,
                        List<Approval> approvals, String terminalStatus) {
    }

    private void requireAnyRole(AuthenticatedCaller caller, Role... roles) {
        for (Role role : roles) {
            if (caller.principal().hasRole(role)) {
                return;
            }
        }
        audit.denied(caller.tenantId(), caller.principalId(), "rbac.check",
                "role", java.util.Arrays.toString(roles), "missing all of required roles");
        throw new ForbiddenException(
                "principal lacks any of roles " + java.util.Arrays.toString(roles));
    }
}
