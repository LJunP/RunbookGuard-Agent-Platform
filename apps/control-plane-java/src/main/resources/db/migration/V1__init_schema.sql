-- RunbookGuard Control Plane 初始 schema（M1）
--
-- 两处刻意使用数据库层强约束而非应用层判断：
--   1. audit_event 的 UPDATE/DELETE 触发器 —— 应用层"我不会改审计"不构成证明。
--   2. run_terminal_state 的主键 —— 唯一终态（INV-5）必须在并发/重投下成立，
--      应用层的 "先查再写" 在两个 Worker 同时接管时会双写。

CREATE TABLE tenant (
    tenant_id    VARCHAR(64)  NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    created_at   DATETIME(3)  NOT NULL,
    PRIMARY KEY (tenant_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE principal (
    principal_id  VARCHAR(64)  NOT NULL,
    tenant_id     VARCHAR(64)  NOT NULL,
    display_name  VARCHAR(128) NOT NULL,
    -- 逗号分隔的角色集合。第一版角色数量固定且极少，独立关联表带来的 join 成本
    -- 换不到任何查询灵活性。
    roles         VARCHAR(255) NOT NULL,
    api_token_hash CHAR(64)    NOT NULL,
    enabled       TINYINT(1)   NOT NULL DEFAULT 1,
    created_at    DATETIME(3)  NOT NULL,
    PRIMARY KEY (principal_id),
    UNIQUE KEY uk_principal_token (api_token_hash),
    KEY idx_principal_tenant (tenant_id),
    CONSTRAINT fk_principal_tenant FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE incident (
    incident_id    VARCHAR(64)  NOT NULL,
    tenant_id      VARCHAR(64)  NOT NULL,
    source         VARCHAR(64)  NOT NULL,
    severity       VARCHAR(16)  NOT NULL,
    title          VARCHAR(512) NOT NULL,
    started_at     DATETIME(3)  NOT NULL,
    current_status VARCHAR(32)  NOT NULL,
    version        BIGINT       NOT NULL DEFAULT 1,
    created_at     DATETIME(3)  NOT NULL,
    updated_at     DATETIME(3)  NOT NULL,
    PRIMARY KEY (incident_id),
    KEY idx_incident_tenant_status (tenant_id, current_status, started_at),
    CONSTRAINT fk_incident_tenant FOREIGN KEY (tenant_id) REFERENCES tenant (tenant_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE agent_run (
    run_id               VARCHAR(64) NOT NULL,
    incident_id          VARCHAR(64) NOT NULL,
    tenant_id            VARCHAR(64) NOT NULL,
    principal_id         VARCHAR(64) NOT NULL,
    graph_version        VARCHAR(32) NOT NULL,
    prompt_version       VARCHAR(32) NOT NULL,
    model_id             VARCHAR(128) NOT NULL,
    dataset_version      VARCHAR(32) NOT NULL,
    status               VARCHAR(32) NOT NULL,
    current_step         VARCHAR(64)  DEFAULT NULL,
    max_steps            INT         NOT NULL,
    deadline             DATETIME(3) NOT NULL,
    -- 成本以整数微单位存储（1 cost_unit = 1e-6 计价单位）。浮点货币在跨语言链路里
    -- 与 ADR-0002 的浮点拒绝规则冲突。
    cost_budget_micros   BIGINT      NOT NULL,
    cost_spent_micros    BIGINT      NOT NULL DEFAULT 0,
    token_budget         BIGINT      NOT NULL,
    token_spent          BIGINT      NOT NULL DEFAULT 0,
    tool_call_budget     INT         NOT NULL,
    tool_call_count      INT         NOT NULL DEFAULT 0,
    steps_used           INT         NOT NULL DEFAULT 0,
    failure_class        VARCHAR(64)  DEFAULT NULL,
    version              BIGINT      NOT NULL DEFAULT 1,
    created_at           DATETIME(3) NOT NULL,
    updated_at           DATETIME(3) NOT NULL,
    PRIMARY KEY (run_id),
    KEY idx_run_incident (incident_id),
    KEY idx_run_tenant_status (tenant_id, status),
    CONSTRAINT fk_run_incident FOREIGN KEY (incident_id) REFERENCES incident (incident_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- 唯一终态的强制点。写入即宣告终态；主键冲突就是"这个 Run 已经有终态了"。
CREATE TABLE run_terminal_state (
    run_id          VARCHAR(64) NOT NULL,
    terminal_status VARCHAR(32) NOT NULL,
    failure_class   VARCHAR(64)  DEFAULT NULL,
    decided_by      VARCHAR(128) NOT NULL,
    created_at      DATETIME(3) NOT NULL,
    PRIMARY KEY (run_id),
    CONSTRAINT fk_terminal_run FOREIGN KEY (run_id) REFERENCES agent_run (run_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE run_step (
    step_id         VARCHAR(64) NOT NULL,
    run_id          VARCHAR(64) NOT NULL,
    sequence        INT         NOT NULL,
    node_name       VARCHAR(64) NOT NULL,
    input_artifact  VARCHAR(512) DEFAULT NULL,
    output_artifact VARCHAR(512) DEFAULT NULL,
    tool_call_id    VARCHAR(64)  DEFAULT NULL,
    status          VARCHAR(32) NOT NULL,
    failure_class   VARCHAR(64)  DEFAULT NULL,
    started_at      DATETIME(3) NOT NULL,
    finished_at     DATETIME(3)  DEFAULT NULL,
    PRIMARY KEY (step_id),
    UNIQUE KEY uk_step_run_sequence (run_id, sequence),
    CONSTRAINT fk_step_run FOREIGN KEY (run_id) REFERENCES agent_run (run_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE approval (
    approval_id      VARCHAR(64) NOT NULL,
    run_id           VARCHAR(64) NOT NULL,
    tenant_id        VARCHAR(64) NOT NULL,
    -- 发起方（Agent Runtime 代表的 principal），与 decided_by 不是同一个人。
    principal_id     VARCHAR(64) NOT NULL,
    tool_name        VARCHAR(128) NOT NULL,
    resource_ref     VARCHAR(256) NOT NULL,
    arguments_digest CHAR(64)    NOT NULL,
    digest_alg       VARCHAR(32) NOT NULL,
    -- 规范化后的参数原文，供审批人核对。ADR-0002 §3 限制其 <= 8 KiB。
    arguments_canonical TEXT     NOT NULL,
    decision         VARCHAR(16) NOT NULL,
    decided_by       VARCHAR(64)  DEFAULT NULL,
    expires_at       DATETIME(3) NOT NULL,
    decided_at       DATETIME(3)  DEFAULT NULL,
    consumed_at      DATETIME(3)  DEFAULT NULL,
    version          BIGINT      NOT NULL DEFAULT 1,
    created_at       DATETIME(3) NOT NULL,
    PRIMARY KEY (approval_id),
    KEY idx_approval_run (run_id),
    KEY idx_approval_pending (tenant_id, decision, expires_at),
    CONSTRAINT fk_approval_run FOREIGN KEY (run_id) REFERENCES agent_run (run_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE evidence_reference (
    evidence_id     VARCHAR(64)  NOT NULL,
    run_id          VARCHAR(64)  NOT NULL,
    tenant_id       VARCHAR(64)  NOT NULL,
    source_type     VARCHAR(32)  NOT NULL,
    source_identity VARCHAR(256) NOT NULL,
    version         VARCHAR(64)  NOT NULL,
    location        VARCHAR(512) NOT NULL,
    content_hash    CHAR(64)     NOT NULL,
    captured_at     DATETIME(3)  NOT NULL,
    PRIMARY KEY (evidence_id),
    KEY idx_evidence_run (run_id),
    CONSTRAINT fk_evidence_run FOREIGN KEY (run_id) REFERENCES agent_run (run_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE checkpoint (
    checkpoint_id        VARCHAR(64) NOT NULL,
    run_id               VARCHAR(64) NOT NULL,
    tenant_id            VARCHAR(64) NOT NULL,
    graph_version        VARCHAR(32) NOT NULL,
    state_schema_version VARCHAR(32) NOT NULL,
    sequence             INT         NOT NULL,
    state_digest         CHAR(64)    NOT NULL,
    state_location       VARCHAR(512) NOT NULL,
    created_at           DATETIME(3) NOT NULL,
    PRIMARY KEY (checkpoint_id),
    UNIQUE KEY uk_checkpoint_run_sequence (run_id, sequence),
    CONSTRAINT fk_checkpoint_run FOREIGN KEY (run_id) REFERENCES agent_run (run_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE audit_event (
    event_id      BIGINT       NOT NULL AUTO_INCREMENT,
    tenant_id     VARCHAR(64)  NOT NULL,
    principal_id  VARCHAR(64)   DEFAULT NULL,
    action        VARCHAR(64)  NOT NULL,
    resource_type VARCHAR(32)  NOT NULL,
    resource_id   VARCHAR(64)   DEFAULT NULL,
    outcome       VARCHAR(16)  NOT NULL,
    reason        VARCHAR(512)  DEFAULT NULL,
    detail_json   TEXT          DEFAULT NULL,
    trace_id      VARCHAR(64)   DEFAULT NULL,
    occurred_at   DATETIME(3)  NOT NULL,
    PRIMARY KEY (event_id),
    KEY idx_audit_tenant_time (tenant_id, occurred_at),
    KEY idx_audit_resource (resource_type, resource_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

CREATE TABLE idempotency_record (
    idempotency_key VARCHAR(128) NOT NULL,
    tenant_id       VARCHAR(64)  NOT NULL,
    operation       VARCHAR(64)  NOT NULL,
    request_digest  CHAR(64)     NOT NULL,
    response_status INT          NOT NULL,
    response_body   TEXT         NOT NULL,
    created_at      DATETIME(3)  NOT NULL,
    PRIMARY KEY (idempotency_key),
    KEY idx_idem_tenant (tenant_id, operation)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;
