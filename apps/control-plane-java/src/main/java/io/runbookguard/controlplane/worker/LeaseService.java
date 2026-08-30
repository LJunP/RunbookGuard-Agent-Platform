package io.runbookguard.controlplane.worker;

import io.runbookguard.controlplane.audit.AuditService;
import io.runbookguard.controlplane.persistence.WorkerLeaseMapper;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.util.List;
import java.util.Optional;

@Service
public class LeaseService {

    private final WorkerLeaseMapper leaseMapper;
    private final AuditService audit;

    public LeaseService(WorkerLeaseMapper leaseMapper, AuditService audit) {
        this.leaseMapper = leaseMapper;
        this.audit = audit;
    }

    /**
     * @return 空表示 Lease 由他人持有且未过期
     */
    @Transactional
    public Optional<Lease> acquire(String runId, String tenantId, String ownerId, Duration ttl) {
        if (leaseMapper.acquireOrTakeOver(runId, ownerId, Math.max(1, ttl.toMillis())) == 0) {
            return Optional.empty();
        }
        Lease lease = leaseMapper.find(runId);
        audit.allowed(tenantId, ownerId, "lease.acquire", "run", runId,
                "{\"fencingToken\":%d}".formatted(lease.fencingToken()));
        return Optional.of(lease);
    }

    @Transactional
    public boolean heartbeat(String runId, String ownerId, long fencingToken, Duration ttl) {
        return leaseMapper.heartbeat(runId, ownerId, fencingToken, Math.max(1, ttl.toMillis())) == 1;
    }

    @Transactional
    public boolean release(String runId, String ownerId, long fencingToken) {
        return leaseMapper.release(runId, ownerId, fencingToken) == 1;
    }

    /**
     * 状态写入前的守卫。TTL 本身不足以防护——Lease 过期不代表原 Worker 已死，它可能只是被
     * GC 停了。没有 token 校验，它恢复后会带着过期的认知继续写。
     */
    @Transactional(readOnly = true)
    public void requireValidToken(String runId, String ownerId, long fencingToken) {
        Long current = leaseMapper.currentToken(runId, ownerId);
        if (current == null) {
            throw new StaleFencingTokenException(
                    "lease for run " + runId + " is no longer held by " + ownerId);
        }
        if (current != fencingToken) {
            throw new StaleFencingTokenException(
                    "stale fencing token for run " + runId + ": held=" + fencingToken
                            + " current=" + current);
        }
    }

    @Transactional(readOnly = true)
    public Optional<Lease> find(String runId) {
        return Optional.ofNullable(leaseMapper.find(runId));
    }

    @Transactional(readOnly = true)
    public List<String> findReclaimable(int limit) {
        return leaseMapper.findReclaimable(Math.min(Math.max(limit, 1), 500));
    }
}
