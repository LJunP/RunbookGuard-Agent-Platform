package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.ApprovalService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/v1/approvals")
public class ApprovalController {

    private final ApprovalService approvalService;

    public ApprovalController(ApprovalService approvalService) {
        this.approvalService = approvalService;
    }

    /** Agent Runtime 发起。它只能请求，不能放行。 */
    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public Dtos.ApprovalResponse request(AuthenticatedCaller caller,
                                         @Valid @RequestBody Dtos.RequestApprovalRequest request) {
        return Dtos.ApprovalResponse.from(approvalService.request(caller, request.runId(),
                request.toolName(), request.resourceRef(), request.arguments()));
    }

    /**
     * 单个审批的读取。Agent Runtime 的 Policy 用它判断「审批是否已批准且参数一致」。
     * 读到 APPROVED 不等于可以执行 —— 执行前仍要走 consume 的四项比对（M0 INV-4）。
     */
    @GetMapping("/{approvalId}")
    public Dtos.ApprovalResponse get(AuthenticatedCaller caller, @PathVariable String approvalId) {
        return Dtos.ApprovalResponse.from(approvalService.get(caller, approvalId));
    }

    @GetMapping("/pending")
    public List<Dtos.ApprovalResponse> listPending(AuthenticatedCaller caller) {
        return approvalService.listPending(caller).stream()
                .map(Dtos.ApprovalResponse::from)
                .toList();
    }

    /** 人工决策，要求 APPROVER 角色。 */
    @PostMapping("/{approvalId}/decision")
    public Dtos.ApprovalResponse decide(AuthenticatedCaller caller, @PathVariable String approvalId,
                                        @Valid @RequestBody Dtos.DecideApprovalRequest request) {
        return Dtos.ApprovalResponse.from(approvalService.decide(
                caller, approvalId, request.approve(), request.reason()));
    }

    /**
     * 执行前校验并消费。参数与审批时不一致（digest 不符）、工具或资源不符、已过期、已消费，
     * 全部返回 403。
     */
    @PostMapping("/{approvalId}/consume")
    public Dtos.ApprovalResponse consume(AuthenticatedCaller caller, @PathVariable String approvalId,
                                         @Valid @RequestBody Dtos.ConsumeApprovalRequest request) {
        return Dtos.ApprovalResponse.from(approvalService.verifyAndConsume(
                caller, approvalId, request.toolName(), request.resourceRef(), request.arguments()));
    }
}
