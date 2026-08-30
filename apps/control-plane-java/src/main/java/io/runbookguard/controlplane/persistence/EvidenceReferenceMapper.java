package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.domain.EvidenceReference;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * 证据引用。与 run_step 一样只增不改。
 *
 * <p>evidence_id 由 Agent Runtime 按内容摘要派生（不是序号），因此重复上报同一份证据
 * 会撞主键。用 INSERT IGNORE 跳过：同一份数据被查两次是同一份证据，不是新证据。
 */
@Mapper
public interface EvidenceReferenceMapper {

    String COLUMNS = """
            evidence_id, run_id, tenant_id, source_type, source_identity, version,
            location, content_hash, captured_at
            """;

    @Insert("""
            INSERT IGNORE INTO evidence_reference (evidence_id, run_id, tenant_id, source_type,
                                                   source_identity, version, location,
                                                   content_hash, captured_at)
            VALUES (#{evidenceId}, #{runId}, #{tenantId}, #{sourceType}, #{sourceIdentity},
                    #{version}, #{location}, #{contentHash}, #{capturedAt})
            """)
    int insertIfAbsent(EvidenceReference evidence);

    @Select("SELECT " + COLUMNS + """
            FROM evidence_reference
            WHERE run_id = #{runId} AND tenant_id = #{tenantId}
            ORDER BY captured_at ASC, evidence_id ASC
            """)
    List<EvidenceReference> listByRunInTenant(@Param("runId") String runId,
                                              @Param("tenantId") String tenantId);
}
