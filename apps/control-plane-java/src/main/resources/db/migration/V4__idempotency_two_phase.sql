-- M2：幂等记录改为「先占位、后填结果」两阶段。
--
-- 原实现在业务执行之后才 INSERT 幂等键，8 个并发消费者会全部执行完业务再去抢插入，
-- 副作用发生 8 次。占位必须发生在业务之前，因此结果字段在占位时还是未知的。

ALTER TABLE idempotency_record
    MODIFY COLUMN response_status INT DEFAULT NULL,
    MODIFY COLUMN response_body TEXT DEFAULT NULL,
    ADD COLUMN completed_at DATETIME(3) DEFAULT NULL AFTER created_at;
