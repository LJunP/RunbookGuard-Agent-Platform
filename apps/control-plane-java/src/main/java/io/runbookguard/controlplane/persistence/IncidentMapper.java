package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.domain.Incident;
import io.runbookguard.controlplane.domain.IncidentStatus;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.Instant;
import java.util.List;

@Mapper
public interface IncidentMapper {

    @Insert("""
            INSERT INTO incident (incident_id, tenant_id, source, severity, title, started_at,
                                  current_status, version, created_at, updated_at)
            VALUES (#{incidentId}, #{tenantId}, #{source}, #{severity}, #{title}, #{startedAt},
                    #{currentStatus}, 1, #{createdAt}, #{updatedAt})
            """)
    int insert(Incident incident);

    /** tenant_id 进 WHERE 而不是查出来后比对——后过滤在漏写一处时就是跨租户泄漏。 */
    @Select("""
            SELECT incident_id, tenant_id, source, severity, title, started_at,
                   current_status, version, created_at, updated_at
            FROM incident
            WHERE incident_id = #{incidentId} AND tenant_id = #{tenantId}
            """)
    Incident findByIdInTenant(@Param("incidentId") String incidentId,
                              @Param("tenantId") String tenantId);

    @Select("""
            SELECT incident_id, tenant_id, source, severity, title, started_at,
                   current_status, version, created_at, updated_at
            FROM incident
            WHERE tenant_id = #{tenantId}
            ORDER BY started_at DESC, incident_id DESC
            LIMIT #{limit} OFFSET #{offset}
            """)
    List<Incident> listByTenant(@Param("tenantId") String tenantId,
                               @Param("limit") int limit,
                               @Param("offset") int offset);

    /**
     * 乐观锁：版本不匹配返回 0 行。调用方必须检查返回值，不能假定成功。
     */
    @Update("""
            UPDATE incident
            SET current_status = #{newStatus}, version = version + 1, updated_at = #{updatedAt}
            WHERE incident_id = #{incidentId} AND tenant_id = #{tenantId} AND version = #{expectedVersion}
            """)
    int updateStatusWithVersion(@Param("incidentId") String incidentId,
                                @Param("tenantId") String tenantId,
                                @Param("newStatus") IncidentStatus newStatus,
                                @Param("expectedVersion") long expectedVersion,
                                @Param("updatedAt") Instant updatedAt);
}
