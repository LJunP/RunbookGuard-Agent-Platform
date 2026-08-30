-- M2：Lease / 取消 / 派发序号
--
-- Lease 权威状态在 MySQL 而非 Redis（ADR-0003 §1）：Lease 判定必须与终态写入在同一事务
-- 边界内可验证，Redis 的主从延迟会让两个 Worker 对"谁持有 Lease"得出不同结论。

CREATE TABLE worker_lease (
    run_id        VARCHAR(64) NOT NULL,
    tenant_id     VARCHAR(64) NOT NULL,
    owner_id      VARCHAR(128) NOT NULL,
    -- 每次重新获取自增。Worker 上报状态时必须携带，防止 Lease 过期被接管后
    -- 原 Worker 从 GC 暂停恢复并继续写入（ADR-0003 §3）。
    fencing_token BIGINT      NOT NULL DEFAULT 1,
    acquired_at   DATETIME(3) NOT NULL,
    -- 过期判定一律用数据库时间；Worker 之间时钟不同步时用应用时间会得出不同结论。
    expires_at    DATETIME(3) NOT NULL,
    heartbeat_at  DATETIME(3) NOT NULL,
    released_at   DATETIME(3)  DEFAULT NULL,
    PRIMARY KEY (run_id),
    KEY idx_lease_expiry (expires_at),
    KEY idx_lease_owner (owner_id),
    CONSTRAINT fk_lease_run FOREIGN KEY (run_id) REFERENCES agent_run (run_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

ALTER TABLE agent_run
    -- 取消通过数据库标记传播，不走消息队列：消息可能到达已不持有 Lease 的 Worker，
    -- 或在 Worker 重启后丢失（ADR-0003 §5）。
    ADD COLUMN cancel_requested_at DATETIME(3) DEFAULT NULL AFTER failure_class,
    ADD COLUMN cancel_requested_by VARCHAR(64) DEFAULT NULL AFTER cancel_requested_at,
    -- 派发序号。与 run_id 一起构成幂等键，两者都持久化，因此生产者重启后仍能重现同一个键。
    ADD COLUMN dispatch_sequence INT NOT NULL DEFAULT 0 AFTER cancel_requested_by;
