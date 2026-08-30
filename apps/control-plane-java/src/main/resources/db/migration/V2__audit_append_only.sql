-- 审计事件不可篡改：只允许 INSERT。
-- 用触发器而不是"只授予 INSERT 权限"，因为应用连接通常是同一个账号，且权限配置在
-- 部署时容易被改回来；触发器和 schema 一起版本化。

CREATE TRIGGER trg_audit_event_no_update
    BEFORE UPDATE ON audit_event
    FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_event is append-only: UPDATE denied';

CREATE TRIGGER trg_audit_event_no_delete
    BEFORE DELETE ON audit_event
    FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_event is append-only: DELETE denied';

-- 终态同样不可改写：一个 Run 的终态一旦落定就是历史事实。
CREATE TRIGGER trg_terminal_state_no_update
    BEFORE UPDATE ON run_terminal_state
    FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'run_terminal_state is immutable: UPDATE denied';

CREATE TRIGGER trg_terminal_state_no_delete
    BEFORE DELETE ON run_terminal_state
    FOR EACH ROW
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'run_terminal_state is immutable: DELETE denied';
