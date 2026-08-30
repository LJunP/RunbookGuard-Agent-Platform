package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.domain.AgentRun;
import io.runbookguard.controlplane.domain.RunStatus;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.Instant;
import java.util.List;

@Mapper
public interface AgentRunMapper {

    String COLUMNS = """
            run_id, incident_id, tenant_id, principal_id, graph_version, prompt_version,
            model_id, dataset_version, status, current_step, max_steps, deadline,
            cost_budget_micros, cost_spent_micros, token_budget, token_spent,
            tool_call_budget, tool_call_count, steps_used, failure_class,
            cancel_requested_at, dispatch_sequence, version, created_at, updated_at
            """;

    @Insert("""
            INSERT INTO agent_run (run_id, incident_id, tenant_id, principal_id, graph_version,
                                   prompt_version, model_id, dataset_version, status, current_step,
                                   max_steps, deadline, cost_budget_micros, cost_spent_micros,
                                   token_budget, token_spent, tool_call_budget, tool_call_count,
                                   steps_used, failure_class, cancel_requested_at,
                                   dispatch_sequence, version, created_at, updated_at)
            VALUES (#{runId}, #{incidentId}, #{tenantId}, #{principalId}, #{graphVersion},
                    #{promptVersion}, #{modelId}, #{datasetVersion}, #{status}, #{currentStep},
                    #{maxSteps}, #{deadline}, #{costBudgetMicros}, #{costSpentMicros},
                    #{tokenBudget}, #{tokenSpent}, #{toolCallBudget}, #{toolCallCount},
                    #{stepsUsed}, #{failureClass}, #{cancelRequestedAt},
                    #{dispatchSequence}, 1, #{createdAt}, #{updatedAt})
            """)
    int insert(AgentRun run);

    @Select("SELECT " + COLUMNS + " FROM agent_run WHERE run_id = #{runId} AND tenant_id = #{tenantId}")
    AgentRun findByIdInTenant(@Param("runId") String runId, @Param("tenantId") String tenantId);

    /**
     * 仅供后台任务（Lease 回收扫描）使用。它没有调用方身份，因此也没有 tenant 可比对；
     * 所有对外 API 必须走 findByIdInTenant。
     */
    @Select("SELECT " + COLUMNS + " FROM agent_run WHERE run_id = #{runId}")
    AgentRun findByIdAnyTenant(@Param("runId") String runId);

    @Select("""
            SELECT """ + COLUMNS + """
            FROM agent_run
            WHERE incident_id = #{incidentId} AND tenant_id = #{tenantId}
            ORDER BY created_at DESC
            """)
    List<AgentRun> listByIncident(@Param("incidentId") String incidentId,
                                  @Param("tenantId") String tenantId);

    @Update("""
            UPDATE agent_run
            SET status = #{newStatus}, current_step = #{currentStep},
                version = version + 1, updated_at = #{updatedAt}
            WHERE run_id = #{runId} AND tenant_id = #{tenantId} AND version = #{expectedVersion}
            """)
    int updateStatusWithVersion(@Param("runId") String runId,
                                @Param("tenantId") String tenantId,
                                @Param("newStatus") RunStatus newStatus,
                                @Param("currentStep") String currentStep,
                                @Param("expectedVersion") long expectedVersion,
                                @Param("updatedAt") Instant updatedAt);

    /**
     * 终态结算前先取父行的排他锁。不加这一步时并发结算会死锁：run_terminal_state 到 agent_run
     * 的外键让 INSERT 先拿到父行共享锁，随后的 applyTerminalStatus 又要升级成排他锁，两个事务
     * 各持共享锁互相等待。先拿排他锁把结算串行化，重复者随后正常撞主键。
     */
    @Select("SELECT run_id FROM agent_run WHERE run_id = #{runId} FOR UPDATE")
    String lockForSettlement(@Param("runId") String runId);

    @Update("""
            UPDATE agent_run
            SET status = #{terminalStatus}, failure_class = #{failureClass},
                version = version + 1, updated_at = #{updatedAt}
            WHERE run_id = #{runId}
            """)
    int applyTerminalStatus(@Param("runId") String runId,
                            @Param("terminalStatus") RunStatus terminalStatus,
                            @Param("failureClass") String failureClass,
                            @Param("updatedAt") Instant updatedAt);

    /** 自增后用 currentDispatchSequence 读回，两者在同一事务内保证不会拿到重复序号。 */
    @Update("UPDATE agent_run SET dispatch_sequence = dispatch_sequence + 1 WHERE run_id = #{runId}")
    int bumpDispatchSequence(@Param("runId") String runId);

    @Select("SELECT dispatch_sequence FROM agent_run WHERE run_id = #{runId} FOR UPDATE")
    int currentDispatchSequence(@Param("runId") String runId);

    @Update("""
            UPDATE agent_run
            SET cancel_requested_at = #{requestedAt}, cancel_requested_by = #{requestedBy},
                version = version + 1, updated_at = #{requestedAt}
            WHERE run_id = #{runId} AND tenant_id = #{tenantId} AND cancel_requested_at IS NULL
            """)
    int requestCancel(@Param("runId") String runId,
                      @Param("tenantId") String tenantId,
                      @Param("requestedBy") String requestedBy,
                      @Param("requestedAt") Instant requestedAt);

    @Select("""
            SELECT cancel_requested_at FROM agent_run
            WHERE run_id = #{runId} AND tenant_id = #{tenantId}
            """)
    Instant findCancelRequestedAt(@Param("runId") String runId, @Param("tenantId") String tenantId);

    /** Worker 上报进度时累加。用 SQL 累加而非读改写，避免并发上报互相覆盖。 */
    @Update("""
            UPDATE agent_run
            SET cost_spent_micros = #{costSpentMicros}, token_spent = #{tokenSpent},
                tool_call_count = #{toolCallCount}, steps_used = #{stepsUsed},
                status = #{status}, current_step = #{currentStep},
                version = version + 1, updated_at = #{updatedAt}
            WHERE run_id = #{runId} AND tenant_id = #{tenantId}
            """)
    int applyProgress(@Param("runId") String runId,
                      @Param("tenantId") String tenantId,
                      @Param("status") RunStatus status,
                      @Param("currentStep") String currentStep,
                      @Param("costSpentMicros") long costSpentMicros,
                      @Param("tokenSpent") long tokenSpent,
                      @Param("toolCallCount") int toolCallCount,
                      @Param("stepsUsed") int stepsUsed,
                      @Param("updatedAt") Instant updatedAt);
}
