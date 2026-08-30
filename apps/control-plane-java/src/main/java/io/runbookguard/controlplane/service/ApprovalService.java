package io.runbookguard.controlplane.service;

import io.runbookguard.controlplane.api.ForbiddenException;
import io.runbookguard.controlplane.api.ResourceNotFoundException;
import io.runbookguard.controlplane.approval.ArgumentsCanonicalizer;
import io.runbookguard.controlplane.audit.AuditEvent;
import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.config.RunbookGuardProperties;
import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.Approval;
import io.runbookguard.controlplane.domain.ApprovalDecision;
import io.runbookguard.controlplane.domain.Role;
import io.runbookguard.controlplane.persistence.ApprovalMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * 审批的权威判定点（M0 INV-4）。Python Agent Runtime 只能 request 与 verifyAndConsume，
 * 决策入口 decide() 要求 APPROVER 角色，而 AGENT_RUNTIME 角色不含 APPROVER。
 */
@Service
public class ApprovalService {

    private final ApprovalMapper approvalMapper;
    private final AgentRunService runService;
    private final ArgumentsCanonicalizer canonicalizer;
    private final AuditService audit;
    private final RunbookGuardProperties props;
    private final Clock clock;

    public ApprovalService(ApprovalMapper approvalMapper, AgentRunService runService,
                           ArgumentsCanonicalizer canonicalizer, AuditService audit,
                           RunbookGuardProperties props, Clock clock) {
        this.approvalMapper = approvalMapper;
        this.runService = runService;
        this.canonicalizer = canonicalizer;
        this.audit = audit;
        this.props = props;
        this.clock = clock;
    }

    @Transactional
    public Approval request(AuthenticatedCaller caller, String runId, String toolName,
                            String resourceRef, Map<String, Object> arguments) {
        requireRole(caller, Role.AGENT_RUNTIME);
        AgentRun run = runService.get(caller, runId);

        String canonical = canonicalizer.canonicalize(arguments);
        String digest = canonicalizer.digest(arguments);
        Instant now = clock.instant();

        Approval approval = new Approval(
                "apr-" + UUID.randomUUID(), run.runId(), caller.tenantId(), caller.principalId(),
                toolName, resourceRef, digest, ArgumentsCanonicalizer.DIGEST_ALG, canonical,
                ApprovalDecision.PENDING, null, now.plus(props.approval().defaultTtl()),
                null, null, 1, now);
        approvalMapper.insert(approval);
        audit.allowed(caller.tenantId(), caller.principalId(), "approval.request",
                "approval", approval.approvalId(),
                "{\"toolName\":\"%s\",\"resourceRef\":\"%s\",\"argumentsDigest\":\"%s\"}"
                        .formatted(toolName, resourceRef, digest));
        return approval;
    }

    @Transactional
    public Approval decide(AuthenticatedCaller caller, String approvalId, boolean approve,
                           String reason) {
        requireRole(caller, Role.APPROVER);
        Approval approval = load(caller, approvalId);

        // 审批人不能是发起人。自批自审会让四眼原则失效。
        if (approval.principalId().equals(caller.principalId())) {
            audit.denied(caller.tenantId(), caller.principalId(), "approval.decide",
                    "approval", approvalId, "requester cannot approve own request");
            throw new ForbiddenException("requester cannot approve their own request");
        }

        Instant now = clock.instant();
        ApprovalDecision decision = approve ? ApprovalDecision.APPROVED : ApprovalDecision.REJECTED;
        int updated = approvalMapper.decide(approvalId, caller.tenantId(), decision,
                caller.principalId(), now);
        if (updated == 0) {
            Approval latest = load(caller, approvalId);
            String why = latest.isExpiredAt(now)
                    ? "approval expired at " + latest.expiresAt()
                    : "approval no longer pending: " + latest.decision();
            audit.denied(caller.tenantId(), caller.principalId(), "approval.decide",
                    "approval", approvalId, why);
            throw new ApprovalNotDecidableException(why);
        }
        audit.allowed(caller.tenantId(), caller.principalId(), "approval.decide",
                "approval", approvalId, "{\"decision\":\"%s\",\"reason\":%s}"
                        .formatted(decision, reason == null ? "null" : "\"" + reason + "\""));
        return load(caller, approvalId);
    }

    /**
     * 执行前的最后一道闸（威胁 T-2）。四个字段全部比对，不只比对摘要——摘要相同但工具名或
     * 资源不同的调用，不是被批准过的那一个。
     *
     * <p>消费与校验在同一个事务里完成：先校验后消费之间的窗口足够让重放挤进来。
     */
    @Transactional
    public Approval verifyAndConsume(AuthenticatedCaller caller, String approvalId,
                                     String toolName, String resourceRef,
                                     Map<String, Object> actualArguments) {
        requireRole(caller, Role.AGENT_RUNTIME);
        Approval approval = load(caller, approvalId);
        Instant now = clock.instant();

        if (approval.decision() != ApprovalDecision.APPROVED) {
            return denyExecution(caller, approval, "approval is not APPROVED: " + approval.decision());
        }
        if (approval.isExpiredAt(now)) {
            return denyExecution(caller, approval, "approval expired at " + approval.expiresAt());
        }
        if (approval.isConsumed()) {
            return denyExecution(caller, approval, "approval already consumed at " + approval.consumedAt());
        }
        if (!approval.toolName().equals(toolName)) {
            return denyExecution(caller, approval,
                    "tool mismatch: approved=" + approval.toolName() + " actual=" + toolName);
        }
        if (!approval.resourceRef().equals(resourceRef)) {
            return denyExecution(caller, approval,
                    "resource mismatch: approved=" + approval.resourceRef() + " actual=" + resourceRef);
        }

        String actualDigest = canonicalizer.digest(actualArguments);
        if (!approval.argumentsDigest().equals(actualDigest)) {
            return denyExecution(caller, approval,
                    "arguments digest mismatch: approved=" + approval.argumentsDigest()
                            + " actual=" + actualDigest);
        }

        int consumed = approvalMapper.consume(approvalId, caller.tenantId(), now);
        if (consumed == 0) {
            // 并发的第二个执行者走到这里：数据库已把消费权判给了另一方。
            return denyExecution(caller, approval, "approval consumed concurrently");
        }
        audit.allowed(caller.tenantId(), caller.principalId(), "approval.consume",
                "approval", approvalId,
                "{\"toolName\":\"%s\",\"argumentsDigest\":\"%s\"}".formatted(toolName, actualDigest));
        return load(caller, approvalId);
    }

    @Transactional(readOnly = true)
    public List<Approval> listPending(AuthenticatedCaller caller) {
        return approvalMapper.listPending(caller.tenantId());
    }

    @Transactional
    public int expireStale(AuthenticatedCaller caller) {
        return approvalMapper.markExpired(caller.tenantId(), clock.instant());
    }

    private Approval denyExecution(AuthenticatedCaller caller, Approval approval, String reason) {
        audit.record(caller.tenantId(), caller.principalId(), "approval.consume",
                "approval", approval.approvalId(), AuditEvent.OUTCOME_DENIED, reason, null, null);
        throw new ApprovalVerificationFailedException(reason);
    }

    /**
     * 单个审批的读取。
     *
     * <p>没有它时 Agent Runtime 的 fetch() 只能靠 /pending 列表，因此对**已决**的审批
     * 一律返回 UNKNOWN —— Policy 于是无法区分「已批准」与「查不到」，只能保守拒绝。
     * 这条端点补上的正是那个缺口。
     *
     * <p>只需 VIEWER 之外的任一身份：读一条审批不构成放行，放行仍要走 verifyAndConsume。
     */
    @Transactional(readOnly = true)
    public Approval get(AuthenticatedCaller caller, String approvalId) {
        return load(caller, approvalId);
    }

    private Approval load(AuthenticatedCaller caller, String approvalId) {
        Approval approval = approvalMapper.findByIdInTenant(approvalId, caller.tenantId());
        if (approval == null) {
            audit.denied(caller.tenantId(), caller.principalId(), "approval.read",
                    "approval", approvalId, "not found in tenant");
            throw new ResourceNotFoundException("approval not found: " + approvalId);
        }
        return approval;
    }

    private void requireRole(AuthenticatedCaller caller, Role role) {
        if (!caller.principal().hasRole(role)) {
            audit.denied(caller.tenantId(), caller.principalId(), "rbac.check",
                    "role", role.name(), "missing role " + role);
            throw new ForbiddenException("principal lacks role " + role);
        }
    }
}
