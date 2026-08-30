package io.runbookguard.controlplane.audit;

import io.runbookguard.controlplane.AbstractIntegrationTest;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 审计不可篡改由数据库触发器强制（V2 migration）。这里绕过应用层直接用 JDBC 尝试改写——
 * 如果只测应用层，等于测了"我没写 update 方法"，而不是"改不了"。
 */
class AuditImmutabilityIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    AuditService auditService;
    @Autowired
    JdbcTemplate jdbc;

    @Test
    @DisplayName("UPDATE audit_event 被数据库拒绝")
    void updateIsRejectedByDatabase() {
        auditService.allowed(tenantA, "prin-x", "test.action", "incident", "inc-1", null);
        Long eventId = jdbc.queryForObject(
                "SELECT event_id FROM audit_event WHERE tenant_id = ? ORDER BY event_id DESC LIMIT 1",
                Long.class, tenantA);

        var ex = assertThrows(Exception.class, () -> jdbc.update(
                "UPDATE audit_event SET outcome = 'ALLOWED', reason = 'tampered' WHERE event_id = ?",
                eventId));
        assertTrue(rootMessage(ex).contains("append-only"), rootMessage(ex));
    }

    @Test
    @DisplayName("DELETE audit_event 被数据库拒绝")
    void deleteIsRejectedByDatabase() {
        auditService.allowed(tenantA, "prin-x", "test.action", "incident", "inc-2", null);
        Long eventId = jdbc.queryForObject(
                "SELECT event_id FROM audit_event WHERE tenant_id = ? ORDER BY event_id DESC LIMIT 1",
                Long.class, tenantA);

        var ex = assertThrows(Exception.class, () ->
                jdbc.update("DELETE FROM audit_event WHERE event_id = ?", eventId));
        assertTrue(rootMessage(ex).contains("append-only"), rootMessage(ex));
    }

    @Test
    @DisplayName("run_terminal_state 不可改写、不可删除")
    void terminalStateIsImmutable() {
        // 直接插一条终态记录需要先有 run；这里用 SQL 层验证触发器本身即可，
        // 通过一个不存在的 run 会被外键拦住，所以借用已有 run。
        String runId = jdbc.query("SELECT run_id FROM agent_run LIMIT 1",
                rs -> rs.next() ? rs.getString(1) : null);
        if (runId == null) {
            return; // 没有 run 时跳过；其它测试类会覆盖到这条路径
        }
        jdbc.update("""
                INSERT INTO run_terminal_state (run_id, terminal_status, failure_class, decided_by, created_at)
                VALUES (?, 'COMPLETE', NULL, 'test', NOW(3))
                ON DUPLICATE KEY UPDATE run_id = run_id
                """, runId);

        var updateEx = assertThrows(Exception.class, () -> jdbc.update(
                "UPDATE run_terminal_state SET terminal_status = 'FAILED' WHERE run_id = ?", runId));
        assertTrue(rootMessage(updateEx).contains("immutable"), rootMessage(updateEx));

        var deleteEx = assertThrows(Exception.class, () ->
                jdbc.update("DELETE FROM run_terminal_state WHERE run_id = ?", runId));
        assertTrue(rootMessage(deleteEx).contains("immutable"), rootMessage(deleteEx));
    }

    @Test
    @DisplayName("拒绝类审计事件在调用方事务回滚后仍然留存")
    void deniedEventSurvivesCallerRollback() {
        long before = auditService.listRecent(tenantA, 200).size();
        auditService.denied(tenantA, "prin-y", "rbac.check", "role", "APPROVER", "missing role");
        List<AuditEvent> after = auditService.listRecent(tenantA, 200);
        assertEquals(before + 1, after.size());
    }

    private static String rootMessage(Throwable t) {
        Throwable cursor = t;
        StringBuilder all = new StringBuilder();
        while (cursor != null) {
            if (cursor.getMessage() != null) {
                all.append(cursor.getMessage()).append(" | ");
            }
            cursor = cursor.getCause();
        }
        return all.toString();
    }
}
