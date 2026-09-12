package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.domain.CheckpointMeta;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * checkpoint 元数据。只增不改：元数据描述的是「历史上写过什么 checkpoint」，
 * 改写它等于允许伪造恢复历史。
 *
 * <p>唯一键 (run_id, sequence) + INSERT IGNORE：Runtime 重投同一批元数据时跳过，
 * 与 run_step 同一套幂等语义（重复上报是消息重投，不是需要审计的事件）。
 */
@Mapper
public interface CheckpointMapper {

    String COLUMNS = """
            checkpoint_id, run_id, tenant_id, graph_version, state_schema_version,
            sequence, state_digest, state_location, created_at
            """;

    @Insert("""
            INSERT IGNORE INTO checkpoint (checkpoint_id, run_id, tenant_id, graph_version,
                                           state_schema_version, sequence, state_digest,
                                           state_location, created_at)
            VALUES (#{checkpointId}, #{runId}, #{tenantId}, #{graphVersion},
                    #{stateSchemaVersion}, #{sequence}, #{stateDigest},
                    #{stateLocation}, #{createdAt})
            """)
    int insertIfAbsent(CheckpointMeta meta);

    @Select("SELECT " + COLUMNS + """
            FROM checkpoint
            WHERE run_id = #{runId} AND tenant_id = #{tenantId}
            ORDER BY sequence ASC
            """)
    List<CheckpointMeta> listByRunInTenant(@Param("runId") String runId,
                                           @Param("tenantId") String tenantId);

    @Select("SELECT COUNT(*) FROM checkpoint WHERE run_id = #{runId} AND tenant_id = #{tenantId}")
    int countByRunInTenant(@Param("runId") String runId, @Param("tenantId") String tenantId);
}
