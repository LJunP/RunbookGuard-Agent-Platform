package io.runbookguard.controlplane.persistence;

import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.Instant;

@Mapper
public interface IdempotencyRecordMapper {

    /**
     * 阶段一：占位。必须在业务执行**之前**，否则并发消费者会各自跑完业务再来抢插入，
     * 副作用发生 N 次。
     */
    @Insert("""
            INSERT INTO idempotency_record (idempotency_key, tenant_id, operation, request_digest,
                                            response_status, response_body, created_at, completed_at)
            VALUES (#{key}, #{tenantId}, #{operation}, #{requestDigest},
                    NULL, NULL, #{createdAt}, NULL)
            """)
    int reserve(@Param("key") String key,
                @Param("tenantId") String tenantId,
                @Param("operation") String operation,
                @Param("requestDigest") String requestDigest,
                @Param("createdAt") Instant createdAt);

    /** 阶段二：业务成功后回填结果。 */
    @Update("""
            UPDATE idempotency_record
            SET response_status = #{responseStatus}, response_body = #{responseBody},
                completed_at = #{completedAt}
            WHERE idempotency_key = #{key} AND completed_at IS NULL
            """)
    int complete(@Param("key") String key,
                 @Param("responseStatus") int responseStatus,
                 @Param("responseBody") String responseBody,
                 @Param("completedAt") Instant completedAt);

    /** 业务失败时释放占位，让该键可重试。 */
    @Delete("DELETE FROM idempotency_record WHERE idempotency_key = #{key} AND completed_at IS NULL")
    int releaseReservation(@Param("key") String key);

    @Select("""
            SELECT idempotency_key AS `key`, tenant_id AS tenantId, operation,
                   request_digest AS requestDigest, response_status AS responseStatus,
                   response_body AS responseBody, created_at AS createdAt,
                   completed_at AS completedAt
            FROM idempotency_record
            WHERE idempotency_key = #{key}
            """)
    IdempotencyRow find(@Param("key") String key);

    /**
     * 锁定读。并发同键时输家需要看到赢家刚提交的行；REPEATABLE READ 下普通 SELECT
     * 读的是事务开始时的快照，会返回 null。
     */
    @Select("""
            SELECT idempotency_key AS `key`, tenant_id AS tenantId, operation,
                   request_digest AS requestDigest, response_status AS responseStatus,
                   response_body AS responseBody, created_at AS createdAt,
                   completed_at AS completedAt
            FROM idempotency_record
            WHERE idempotency_key = #{key} FOR SHARE
            """)
    IdempotencyRow findFresh(@Param("key") String key);

    record IdempotencyRow(
            String key,
            String tenantId,
            String operation,
            String requestDigest,
            Integer responseStatus,
            String responseBody,
            Instant createdAt,
            Instant completedAt) {

        public boolean isCompleted() {
            return completedAt != null;
        }
    }
}
