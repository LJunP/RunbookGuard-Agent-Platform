package io.runbookguard.controlplane.audit;

import io.runbookguard.controlplane.persistence.AuditEventMapper;
import io.runbookguard.controlplane.security.SecretRedactor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;
import java.util.List;

@Service
public class AuditService {

    private final AuditEventMapper mapper;
    private final Clock clock;

    public AuditService(AuditEventMapper mapper, Clock clock) {
        this.mapper = mapper;
        this.clock = clock;
    }

    /**
     * REQUIRES_NEW：拒绝类事件必须在调用方事务回滚后仍然留存。否则"越权尝试被拒绝并回滚"
     * 会连审计记录一起消失，攻击者的尝试变得不可见。
     */
    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void record(String tenantId, String principalId, String action, String resourceType,
                       String resourceId, String outcome, String reason, String detailJson,
                       String traceId) {
        AuditEvent event = new AuditEvent(
                null, tenantId, principalId, action, resourceType, resourceId, outcome,
                SecretRedactor.redact(reason), SecretRedactor.redact(detailJson),
                traceId, clock.instant());
        mapper.insert(event);
    }

    /**
     * allowed/denied 各自标注 REQUIRES_NEW 而不是依赖 record() 的注解：它们内部对 record() 的
     * 调用是自调用，走不到代理，注解不会生效。少了这个标注，从 readOnly 事务里记审计会直接报
     * "Connection is read-only"。
     */
    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void allowed(String tenantId, String principalId, String action,
                        String resourceType, String resourceId, String detailJson) {
        record(tenantId, principalId, action, resourceType, resourceId,
                AuditEvent.OUTCOME_ALLOWED, null, detailJson, null);
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void denied(String tenantId, String principalId, String action,
                       String resourceType, String resourceId, String reason) {
        record(tenantId, principalId, action, resourceType, resourceId,
                AuditEvent.OUTCOME_DENIED, reason, null, null);
    }

    @Transactional(readOnly = true)
    public List<AuditEvent> listRecent(String tenantId, int limit) {
        return mapper.listRecent(tenantId, Math.min(Math.max(limit, 1), 200));
    }

    @Transactional(readOnly = true)
    public List<AuditEvent> listByResource(String tenantId, String resourceType, String resourceId) {
        return mapper.listByResource(tenantId, resourceType, resourceId);
    }
}
