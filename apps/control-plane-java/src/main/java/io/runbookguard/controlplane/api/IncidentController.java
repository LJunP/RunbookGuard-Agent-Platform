package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.service.IncidentService;
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
@RequestMapping("/api/v1/incidents")
public class IncidentController {

    private final IncidentService incidentService;

    public IncidentController(IncidentService incidentService) {
        this.incidentService = incidentService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public Dtos.IncidentResponse create(AuthenticatedCaller caller,
                                        @Valid @RequestBody Dtos.CreateIncidentRequest request) {
        return Dtos.IncidentResponse.from(incidentService.create(caller, request.source(),
                request.severity(), request.title(), request.startedAt()));
    }

    @GetMapping
    public List<Dtos.IncidentResponse> list(AuthenticatedCaller caller,
                                            @RequestParam(defaultValue = "20") int limit,
                                            @RequestParam(defaultValue = "0") int offset) {
        return incidentService.list(caller, limit, offset).stream()
                .map(Dtos.IncidentResponse::from)
                .toList();
    }

    @GetMapping("/{incidentId}")
    public Dtos.IncidentResponse get(AuthenticatedCaller caller, @PathVariable String incidentId) {
        return Dtos.IncidentResponse.from(incidentService.get(caller, incidentId));
    }

    @PatchMapping("/{incidentId}/status")
    public Dtos.IncidentResponse updateStatus(AuthenticatedCaller caller,
                                              @PathVariable String incidentId,
                                              @Valid @RequestBody Dtos.UpdateIncidentStatusRequest request) {
        return Dtos.IncidentResponse.from(incidentService.updateStatus(
                caller, incidentId, request.status(), request.expectedVersion()));
    }
}
