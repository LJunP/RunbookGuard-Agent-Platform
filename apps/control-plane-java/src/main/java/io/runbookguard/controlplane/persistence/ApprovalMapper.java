package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.domain.Approval;
import io.runbookguard.controlplane.domain.ApprovalDecision;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.Instant;
import java.util.List;

@Mapper
public interface ApprovalMapper {

    @Insert("""
            INSERT INTO approval (approval_id, run_id, tenant_id, principal_id, tool_name,
                                  resource_ref, arguments_digest, digest_alg, arguments_canonical,
                                  decision, decided_by, expires_at, decided_at, consumed_at,
                                  version, created_at)
            VALUES (#{approvalId}, #{runId}, #{tenantId}, #{principalId}, #{toolName},
                    #{resourceRef}, #{argumentsDigest}, #{digestAlg}, #{argumentsCanonical},
                    #{decision}, #{decidedBy}, #{expiresAt}, #{decidedAt}, #{consumedAt},
                    1, #{createdAt})
            """)
    int insert(Approval approval);

    @Select("""
            SELECT approval_id, run_id, tenant_id, principal_id, tool_name, resource_ref,
                   arguments_digest, digest_alg, arguments_canonical, decision, decided_by,
                   expires_at, decided_at, consumed_at, version, created_at
            FROM approval
            WHERE approval_id = #{approvalId} AND tenant_id = #{tenantId}
            """)
    Approval findByIdInTenant(@Param("approvalId") String approvalId,
                              @Param("tenantId") String tenantId);

    @Select("""
            SELECT approval_id, run_id, tenant_id, principal_id, tool_name, resource_ref,
                   arguments_digest, digest_alg, arguments_canonical, decision, decided_by,
                   expires_at, decided_at, consumed_at, version, created_at
            FROM approval
            WHERE tenant_id = #{tenantId} AND decision = 'PENDING'
            ORDER BY created_at ASC
            """)
    List<Approval> listPending(@Param("tenantId") String tenantId);

    /**
     * 一个 Run 的全部审批，含已决与已消费的。控制台的 Trace 视图要看到
     * 「请求过什么、谁批的、有没有被执行」这条完整链路，只列 PENDING 看不到历史。
     */
    @Select("""
            SELECT approval_id, run_id, tenant_id, principal_id, tool_name, resource_ref,
                   arguments_digest, digest_alg, arguments_canonical, decision, decided_by,
                   expires_at, decided_at, consumed_at, version, created_at
            FROM approval
            WHERE run_id = #{runId} AND tenant_id = #{tenantId}
            ORDER BY created_at ASC
            """)
    List<Approval> listByRun(@Param("runId") String runId, @Param("tenantId") String tenantId);

    /**
     * 决策只允许从 PENDING 出发，且必须未过期。把 expires_at 判断放进 SQL 而不是先查后判，
     * 是为了避免"查询时未过期、写入时已过期"的竞态。
     */
    @Update("""
            UPDATE approval
            SET decision = #{decision}, decided_by = #{decidedBy}, decided_at = #{decidedAt},
                version = version + 1
            WHERE approval_id = #{approvalId} AND tenant_id = #{tenantId}
              AND decision = 'PENDING' AND expires_at > #{decidedAt}
            """)
    int decide(@Param("approvalId") String approvalId,
               @Param("tenantId") String tenantId,
               @Param("decision") ApprovalDecision decision,
               @Param("decidedBy") String decidedBy,
               @Param("decidedAt") Instant decidedAt);

    /**
     * 单次使用（ADR-0002 §5）：consumed_at IS NULL 是消费的前提条件，写在 WHERE 里让数据库
     * 做并发裁决。两个并发的执行请求只有一个能拿到 1。
     */
    @Update("""
            UPDATE approval
            SET consumed_at = #{consumedAt}, version = version + 1
            WHERE approval_id = #{approvalId} AND tenant_id = #{tenantId}
              AND decision = 'APPROVED' AND consumed_at IS NULL AND expires_at > #{consumedAt}
            """)
    int consume(@Param("approvalId") String approvalId,
                @Param("tenantId") String tenantId,
                @Param("consumedAt") Instant consumedAt);

    @Update("""
            UPDATE approval
            SET decision = 'EXPIRED', version = version + 1
            WHERE tenant_id = #{tenantId} AND decision = 'PENDING' AND expires_at <= #{now}
            """)
    int markExpired(@Param("tenantId") String tenantId, @Param("now") Instant now);
}
