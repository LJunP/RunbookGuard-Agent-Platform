package io.runbookguard.controlplane.idempotency;

import io.runbookguard.controlplane.persistence.IdempotencyRecordMapper;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import java.time.Clock;

/**
 * 幂等记录的独立事务边界。必须是独立 Bean：如果这些方法留在 IdempotencyService 内，
 * 从 executeOnce 的自调用走不到 Spring 代理，REQUIRES_NEW 不生效，占位就无法在业务
 * 执行前被其他消费者看到。
 */
@Component
public class IdempotencyStore {

    private final IdempotencyRecordMapper mapper;
    private final Clock clock;

    public IdempotencyStore(IdempotencyRecordMapper mapper, Clock clock) {
        this.mapper = mapper;
        this.clock = clock;
    }

    /** @return false 表示该键已被占用（重复投递或并发） */
    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public boolean reserve(String key, String tenantId, String operation, String digest) {
        try {
            mapper.reserve(key, tenantId, operation, digest, clock.instant());
            return true;
        } catch (DuplicateKeyException alreadyReserved) {
            return false;
        }
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void complete(String key, int status, String body) {
        mapper.complete(key, status, body, clock.instant());
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW)
    public void releaseReservation(String key) {
        mapper.releaseReservation(key);
    }

    @Transactional(propagation = Propagation.REQUIRES_NEW, readOnly = true)
    public IdempotencyRecordMapper.IdempotencyRow findFresh(String key) {
        return mapper.findFresh(key);
    }
}
