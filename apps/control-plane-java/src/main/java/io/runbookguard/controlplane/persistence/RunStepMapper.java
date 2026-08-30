package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.domain.RunStep;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

import java.util.List;

/**
 * 步骤是 Trace 的骨架。没有 update/delete：一个已经发生的步骤不会变成别的样子，
 * 提供修改入口就等于允许事后改写执行历史。
 *
 * <p>唯一键 (run_id, sequence) 让重复上报在数据库层被拒绝。Worker 重投同一批步骤时
 * 会撞这个键，调用方据此走幂等路径 —— 与 run_terminal_state 同一套思路。
 */
@Mapper
public interface RunStepMapper {

    String COLUMNS = """
            step_id, run_id, sequence, node_name, input_artifact, output_artifact,
            tool_call_id, status, failure_class, started_at, finished_at
            """;

    @Insert("""
            INSERT INTO run_step (step_id, run_id, sequence, node_name, input_artifact,
                                  output_artifact, tool_call_id, status, failure_class,
                                  started_at, finished_at)
            VALUES (#{stepId}, #{runId}, #{sequence}, #{nodeName}, #{inputArtifact},
                    #{outputArtifact}, #{toolCallId}, #{status}, #{failureClass},
                    #{startedAt}, #{finishedAt})
            """)
    int insert(RunStep step);

    /**
     * 重复上报时跳过已存在的 sequence。
     *
     * <p>这里用 INSERT IGNORE 而 run_terminal_state 不用，因为两者的语义不同：
     * 重复的终态是需要被审计的事件（可能是两个执行者在竞争），重复的步骤只是消息重投，
     * 每次都抛异常会让调用方被迫逐条 try/catch。
     */
    @Insert("""
            INSERT IGNORE INTO run_step (step_id, run_id, sequence, node_name, input_artifact,
                                         output_artifact, tool_call_id, status, failure_class,
                                         started_at, finished_at)
            VALUES (#{stepId}, #{runId}, #{sequence}, #{nodeName}, #{inputArtifact},
                    #{outputArtifact}, #{toolCallId}, #{status}, #{failureClass},
                    #{startedAt}, #{finishedAt})
            """)
    int insertIfAbsent(RunStep step);

    @Select("SELECT " + COLUMNS + """
            FROM run_step
            WHERE run_id = #{runId}
            ORDER BY sequence ASC
            """)
    List<RunStep> listByRun(@Param("runId") String runId);

    @Select("SELECT COUNT(*) FROM run_step WHERE run_id = #{runId}")
    int countByRun(@Param("runId") String runId);
}
