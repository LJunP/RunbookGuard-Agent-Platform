package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/v1/audit-events")
public class AuditController {

    private final AuditService auditService;

    public AuditController(AuditService auditService) {
        this.auditService = auditService;
    }

    @GetMapping
    public List<Dtos.AuditEventResponse> list(AuthenticatedCaller caller,
                                              @RequestParam(defaultValue = "50") int limit,
                                              @RequestParam(required = false) String resourceType,
                                              @RequestParam(required = false) String resourceId) {
        var events = (resourceType != null && resourceId != null)
                ? auditService.listByResource(caller.tenantId(), resourceType, resourceId)
                : auditService.listRecent(caller.tenantId(), limit);
        return events.stream().map(Dtos.AuditEventResponse::from).toList();
    }
}
