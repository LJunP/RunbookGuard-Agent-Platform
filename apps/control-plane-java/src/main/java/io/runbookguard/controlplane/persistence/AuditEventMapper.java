package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.audit.AuditEvent;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * 只有 insert 和 select。没有 update/delete 方法不是遗漏——V2 migration 的触发器会拒绝它们，
 * 这里不提供入口是第二道防线。
 */
@Mapper
public interface AuditEventMapper {

    @Insert("""
            INSERT INTO audit_event (tenant_id, principal_id, action, resource_type, resource_id,
                                     outcome, reason, detail_json, trace_id, occurred_at)
            VALUES (#{tenantId}, #{principalId}, #{action}, #{resourceType}, #{resourceId},
                    #{outcome}, #{reason}, #{detailJson}, #{traceId}, #{occurredAt})
            """)
    int insert(AuditEvent event);

    @Select("""
            SELECT event_id, tenant_id, principal_id, action, resource_type, resource_id,
                   outcome, reason, detail_json, trace_id, occurred_at
            FROM audit_event
            WHERE tenant_id = #{tenantId}
            ORDER BY event_id DESC
            LIMIT #{limit}
            """)
    List<AuditEvent> listRecent(@Param("tenantId") String tenantId, @Param("limit") int limit);

    @Select("""
            SELECT event_id, tenant_id, principal_id, action, resource_type, resource_id,
                   outcome, reason, detail_json, trace_id, occurred_at
            FROM audit_event
            WHERE tenant_id = #{tenantId} AND resource_type = #{resourceType}
              AND resource_id = #{resourceId}
            ORDER BY event_id ASC
            """)
    List<AuditEvent> listByResource(@Param("tenantId") String tenantId,
                                    @Param("resourceType") String resourceType,
                                    @Param("resourceId") String resourceId);
}
