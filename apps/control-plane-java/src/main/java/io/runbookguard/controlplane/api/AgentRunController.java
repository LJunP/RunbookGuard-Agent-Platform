package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.AgentRunService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/v1/runs")
public class AgentRunController {

    private final AgentRunService runService;

    public AgentRunController(AgentRunService runService) {
        this.runService = runService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public Dtos.RunResponse create(AuthenticatedCaller caller,
                                   @Valid @RequestBody Dtos.CreateRunRequest request) {
        return Dtos.RunResponse.from(runService.create(caller, request.incidentId(),
                request.graphVersion(), request.promptVersion(), request.modelId(),
                request.datasetVersion()));
    }

    @GetMapping("/{runId}")
    public Dtos.RunResponse get(AuthenticatedCaller caller, @PathVariable String runId) {
        return Dtos.RunResponse.from(runService.get(caller, runId));
    }

    @GetMapping
    public List<Dtos.RunResponse> listByIncident(AuthenticatedCaller caller,
                                                 @RequestParam String incidentId) {
        return runService.listByIncident(caller, incidentId).stream()
                .map(Dtos.RunResponse::from)
                .toList();
    }

    @PatchMapping("/{runId}/status")
    public Dtos.RunResponse advance(AuthenticatedCaller caller, @PathVariable String runId,
                                    @Valid @RequestBody Dtos.AdvanceRunRequest request) {
        return Dtos.RunResponse.from(runService.advance(caller, runId, request.status(),
                request.currentStep(), request.expectedVersion()));
    }

    /**
     * 幂等端点：重复调用返回既有终态且 firstSettlement=false，不报错。M2 的消息重投依赖这个语义。
     */
    @PostMapping("/{runId}/terminal")
    public Dtos.TerminalOutcomeResponse settleTerminal(
            AuthenticatedCaller caller, @PathVariable String runId,
            @Valid @RequestBody Dtos.SettleTerminalRequest request) {
        AgentRunService.TerminalOutcome outcome = runService.settleTerminal(
                caller, runId, request.terminalStatus(), request.failureClass());
        return new Dtos.TerminalOutcomeResponse(outcome.terminalStatus(), outcome.firstSettlement());
    }
}
