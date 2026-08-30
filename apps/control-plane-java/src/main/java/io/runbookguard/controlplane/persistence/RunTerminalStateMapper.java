package io.runbookguard.controlplane.persistence;

import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.time.Instant;

@Mapper
public interface RunTerminalStateMapper {

    /**
     * 不用 INSERT IGNORE：主键冲突必须以异常形式暴露给调用方，由调用方判定"已有终态"
     * 并走幂等返回路径。静默忽略会让"重复终态"变成不可观测的事件。
     */
    @Insert("""
            INSERT INTO run_terminal_state (run_id, terminal_status, failure_class, decided_by, created_at)
            VALUES (#{runId}, #{terminalStatus}, #{failureClass}, #{decidedBy}, #{createdAt})
            """)
    int insert(@Param("runId") String runId,
               @Param("terminalStatus") String terminalStatus,
               @Param("failureClass") String failureClass,
               @Param("decidedBy") String decidedBy,
               @Param("createdAt") Instant createdAt);

    @Select("""
            SELECT terminal_status FROM run_terminal_state WHERE run_id = #{runId}
            """)
    String findTerminalStatus(@Param("runId") String runId);

    /**
     * 主键冲突后读既有终态必须用锁定读。REPEATABLE READ 下普通 SELECT 用的是事务开始时的
     * 快照，看不到并发赢家刚提交的那一行，会返回 null。
     */
    @Select("""
            SELECT terminal_status FROM run_terminal_state WHERE run_id = #{runId} FOR SHARE
            """)
    String findTerminalStatusFresh(@Param("runId") String runId);

    @Select("SELECT COUNT(*) FROM run_terminal_state WHERE run_id = #{runId}")
    int countByRun(@Param("runId") String runId);
}
