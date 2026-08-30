package io.runbookguard.controlplane.idempotency;

import io.runbookguard.controlplane.approval.ArgumentsCanonicalizer;
import io.runbookguard.controlplane.persistence.IdempotencyRecordMapper;
import org.springframework.stereotype.Service;

import java.util.Map;
import java.util.function.Supplier;

/**
 * ADR-0003 §4。请求摘要沿用 ADR-0002 的规范化算法，因此消息体的键顺序与空白不影响判定。
 *
 * <p>两阶段：先占位（独立事务立即提交，让并发者看得见），再执行业务，最后回填结果。
 * 占位若放在业务之后，并发消费者会各自跑完业务才发现重复，副作用已经发生 N 次。
 */
@Service
public class IdempotencyService {

    private final IdempotencyStore store;
    private final ArgumentsCanonicalizer canonicalizer;

    public IdempotencyService(IdempotencyStore store, ArgumentsCanonicalizer canonicalizer) {
        this.store = store;
        this.canonicalizer = canonicalizer;
    }

    public record Outcome(int status, String body) {
    }

    public record Result(Outcome outcome, boolean firstExecution) {
    }

    /**
     * 业务抛异常时释放占位，键仍可重试——把失败的尝试永久记下来会让一次瞬时故障永久占用
     * 该幂等键，任务再也无法推进。
     */
    public Result executeOnce(String idempotencyKey, String tenantId, String operation,
                              Map<String, Object> requestPayload, Supplier<Outcome> business) {
        String digest = canonicalizer.digest(requestPayload);

        if (!store.reserve(idempotencyKey, tenantId, operation, digest)) {
            return replay(idempotencyKey, digest);
        }

        Outcome outcome;
        try {
            outcome = business.get();
        } catch (RuntimeException businessFailure) {
            store.releaseReservation(idempotencyKey);
            throw businessFailure;
        }
        store.complete(idempotencyKey, outcome.status(), outcome.body());
        return new Result(outcome, true);
    }

    private Result replay(String key, String digest) {
        IdempotencyRecordMapper.IdempotencyRow row = store.findFresh(key);
        if (row == null) {
            // 占位者失败后释放了占位，本次可以重试。
            throw new IdempotencyInProgressException(
                    "idempotency key " + key + " was released; retry the delivery");
        }
        if (!row.requestDigest().equals(digest)) {
            throw new IdempotencyConflictException(
                    "idempotency key " + key + " reused with different payload: digest mismatch"
                            + " (recorded=" + row.requestDigest() + " incoming=" + digest + ")");
        }
        if (!row.isCompleted()) {
            throw new IdempotencyInProgressException(
                    "idempotency key " + key + " is being processed by another worker");
        }
        return new Result(new Outcome(row.responseStatus(), row.responseBody()), false);
    }
}
