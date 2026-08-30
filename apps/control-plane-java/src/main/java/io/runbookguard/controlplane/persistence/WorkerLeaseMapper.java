package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.worker.Lease;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.util.List;

/**
 * 过期判定全部用 NOW(3)，不接受应用传入的时间戳：Worker 之间时钟不同步时，
 * 用应用时间会让两个 Worker 对"Lease 是否已过期"得出不同结论（ADR-0003 §1）。
 *
 * <p>TTL 用 MICROSECOND 而非 SECOND：秒粒度让"短 TTL 快速过期"这类测试无法表达，
 * 而 Lease 接管的正确性恰恰只在过期边界上才能被验证。
 */
@Mapper
public interface WorkerLeaseMapper {

    /**
     * 随 Run 创建一并插入的占位行。有了它，acquire 永远是纯 UPDATE——8 个 Worker 并发
     * INSERT 同一主键会在 InnoDB 的插入意图锁上死锁，而 UPDATE 只是在行锁上排队。
     */
    @Insert("""
            INSERT INTO worker_lease (run_id, tenant_id, owner_id, fencing_token,
                                      acquired_at, expires_at, heartbeat_at, released_at)
            VALUES (#{runId}, #{tenantId}, '', 0, NOW(3), NOW(3), NOW(3), NOW(3))
            """)
    int insertPlaceholder(@Param("runId") String runId, @Param("tenantId") String tenantId);

    /**
     * 判定与写入在一条语句内完成，避免"先查是否过期，再写入"之间的竞态。
     * owner_id 相等的分支允许原持有者重入（重试同一消息时不必等自己的 Lease 过期）。
     */
    @Update("""
            UPDATE worker_lease
            SET owner_id = #{ownerId},
                fencing_token = fencing_token + 1,
                acquired_at = NOW(3),
                expires_at = NOW(3) + INTERVAL #{ttlMillis} * 1000 MICROSECOND,
                heartbeat_at = NOW(3),
                released_at = NULL
            WHERE run_id = #{runId}
              AND (owner_id = #{ownerId} OR released_at IS NOT NULL OR expires_at <= NOW(3))
            """)
    int acquireOrTakeOver(@Param("runId") String runId,
                          @Param("ownerId") String ownerId,
                          @Param("ttlMillis") long ttlMillis);

    @Update("""
            UPDATE worker_lease
            SET expires_at = NOW(3) + INTERVAL #{ttlMillis} * 1000 MICROSECOND, heartbeat_at = NOW(3)
            WHERE run_id = #{runId} AND owner_id = #{ownerId}
              AND fencing_token = #{fencingToken}
              AND released_at IS NULL AND expires_at > NOW(3)
            """)
    int heartbeat(@Param("runId") String runId,
                  @Param("ownerId") String ownerId,
                  @Param("fencingToken") long fencingToken,
                  @Param("ttlMillis") long ttlMillis);

    @Update("""
            UPDATE worker_lease
            SET released_at = NOW(3), expires_at = NOW(3)
            WHERE run_id = #{runId} AND owner_id = #{ownerId} AND fencing_token = #{fencingToken}
              AND released_at IS NULL
            """)
    int release(@Param("runId") String runId,
                @Param("ownerId") String ownerId,
                @Param("fencingToken") long fencingToken);

    @Select("""
            SELECT run_id, tenant_id, owner_id, fencing_token, acquired_at, expires_at, heartbeat_at
            FROM worker_lease
            WHERE run_id = #{runId}
            """)
    Lease find(@Param("runId") String runId);

    @Select("SELECT fencing_token FROM worker_lease WHERE run_id = #{runId} AND owner_id = #{ownerId}")
    Long currentToken(@Param("runId") String runId, @Param("ownerId") String ownerId);

    /**
     * 可接管的 Run：Lease 已过期或已释放、尚未落终态，且**曾被某个 Worker 持有过**
     * （fencing_token > 0）。刚创建还没派发的 Run 的占位行不算需要接管。
     */
    @Select("""
            SELECT l.run_id
            FROM worker_lease l
            LEFT JOIN run_terminal_state t ON t.run_id = l.run_id
            WHERE t.run_id IS NULL
              AND l.fencing_token > 0
              AND (l.released_at IS NOT NULL OR l.expires_at <= NOW(3))
            ORDER BY l.expires_at ASC
            LIMIT #{limit}
            """)
    List<String> findReclaimable(@Param("limit") int limit);
}
