package io.runbookguard.controlplane.idempotency;

import io.runbookguard.controlplane.AbstractIntegrationTest;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** ADR-0003 §4。重点是"同键不同内容 = 冲突，不是重复"。 */
class IdempotencyIntegrationTest extends AbstractIntegrationTest {

    @Autowired
    IdempotencyService idempotency;

    private Map<String, Object> payload(String runId, int sequence) {
        Map<String, Object> p = new LinkedHashMap<>();
        p.put("runId", runId);
        p.put("sequence", sequence);
        return p;
    }

    @Test
    @DisplayName("首次执行：真正跑业务并记录结果")
    void firstCallExecutes() {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        AtomicInteger sideEffects = new AtomicInteger();

        var result = idempotency.executeOnce(key, tenantA, "run.dispatch",
                payload("run-1", 1), () -> {
                    sideEffects.incrementAndGet();
                    return new IdempotencyService.Outcome(200, "{\"ok\":true}");
                });

        assertTrue(result.firstExecution());
        assertEquals(200, result.outcome().status());
        assertEquals(1, sideEffects.get());
    }

    @Test
    @DisplayName("同键同内容投递三次：副作用只发生一次，三次都返回首次结果")
    void tripleDeliveryExecutesOnce() {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        AtomicInteger sideEffects = new AtomicInteger();
        Map<String, Object> body = payload("run-1", 1);

        for (int i = 0; i < 3; i++) {
            var result = idempotency.executeOnce(key, tenantA, "run.dispatch", body, () -> {
                sideEffects.incrementAndGet();
                return new IdempotencyService.Outcome(200, "{\"attempt\":\"first\"}");
            });
            assertEquals(200, result.outcome().status());
            assertEquals("{\"attempt\":\"first\"}", result.outcome().body());
            assertEquals(i == 0, result.firstExecution());
        }
        assertEquals(1, sideEffects.get(), "副作用必须只发生一次");
    }

    @Test
    @DisplayName("键顺序不同的等价 payload 视为同一请求（不误判为冲突）")
    void reorderedPayloadIsSameRequest() {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        Map<String, Object> first = new LinkedHashMap<>();
        first.put("runId", "run-1");
        first.put("sequence", 1);
        Map<String, Object> reordered = new LinkedHashMap<>();
        reordered.put("sequence", 1);
        reordered.put("runId", "run-1");

        idempotency.executeOnce(key, tenantA, "run.dispatch", first,
                () -> new IdempotencyService.Outcome(200, "ok"));
        var second = idempotency.executeOnce(key, tenantA, "run.dispatch", reordered,
                () -> new IdempotencyService.Outcome(500, "should not run"));

        assertFalse(second.firstExecution());
        assertEquals("ok", second.outcome().body());
    }

    @Test
    @DisplayName("同键不同内容：抛冲突，不返回首次结果，不执行业务")
    void sameKeyDifferentPayloadConflicts() {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        idempotency.executeOnce(key, tenantA, "run.dispatch", payload("run-1", 1),
                () -> new IdempotencyService.Outcome(200, "first"));

        AtomicInteger sideEffects = new AtomicInteger();
        var ex = assertThrows(IdempotencyConflictException.class, () ->
                idempotency.executeOnce(key, tenantA, "run.dispatch", payload("run-2", 1), () -> {
                    sideEffects.incrementAndGet();
                    return new IdempotencyService.Outcome(200, "second");
                }));

        assertTrue(ex.getMessage().contains("digest mismatch"), ex.getMessage());
        assertEquals(0, sideEffects.get(), "冲突时不得执行业务");
    }

    @Test
    @DisplayName("不同键各自独立执行")
    void differentKeysExecuteIndependently() {
        AtomicInteger sideEffects = new AtomicInteger();
        for (int i = 1; i <= 3; i++) {
            String key = "run.dispatch:" + UUID.randomUUID() + ":" + i;
            idempotency.executeOnce(key, tenantA, "run.dispatch", payload("run-1", i), () -> {
                sideEffects.incrementAndGet();
                return new IdempotencyService.Outcome(200, "ok");
            });
        }
        assertEquals(3, sideEffects.get());
    }

    @Test
    @DisplayName("并发同键：只有一个执行业务，其余拿到结果或被判定为处理中")
    void concurrentSameKeyExecutesOnce() throws Exception {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        Map<String, Object> body = payload("run-1", 1);
        int threads = 8;
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        AtomicInteger sideEffects = new AtomicInteger();
        AtomicInteger firsts = new AtomicInteger();
        AtomicInteger inProgress = new AtomicInteger();
        AtomicInteger replayed = new AtomicInteger();

        var tasks = new java.util.ArrayList<Callable<Void>>();
        for (int i = 0; i < threads; i++) {
            tasks.add(() -> {
                try {
                    var r = idempotency.executeOnce(key, tenantA, "run.dispatch", body, () -> {
                        sideEffects.incrementAndGet();
                        return new IdempotencyService.Outcome(200, "ok");
                    });
                    if (r.firstExecution()) {
                        firsts.incrementAndGet();
                    } else {
                        replayed.incrementAndGet();
                    }
                } catch (IdempotencyInProgressException stillRunning) {
                    // 赢家尚未回填结果。抛异常而非返回空结果，让消息重投——
                    // 返回空会让调用方误以为业务已完成。
                    inProgress.incrementAndGet();
                }
                return null;
            });
        }
        for (Future<Void> f : pool.invokeAll(tasks)) {
            f.get();
        }
        pool.shutdown();

        assertEquals(1, sideEffects.get(), "并发下副作用仍只能发生一次");
        assertEquals(1, firsts.get());
        assertEquals(threads, firsts.get() + inProgress.get() + replayed.get(),
                "每个线程都必须有明确结果：执行、处理中被拒、或拿到首次结果");
    }

    @Test
    @DisplayName("赢家完成后，后续投递拿到首次结果")
    void replayAfterCompletionReturnsFirstResult() {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        Map<String, Object> body = payload("run-1", 1);
        idempotency.executeOnce(key, tenantA, "run.dispatch", body,
                () -> new IdempotencyService.Outcome(201, "created"));

        var replay = idempotency.executeOnce(key, tenantA, "run.dispatch", body,
                () -> new IdempotencyService.Outcome(500, "must not run"));
        assertFalse(replay.firstExecution());
        assertEquals(201, replay.outcome().status());
        assertEquals("created", replay.outcome().body());
    }

    @Test
    @DisplayName("业务抛异常时不写幂等记录，允许重试")
    void failedExecutionIsRetryable() {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        Map<String, Object> body = payload("run-1", 1);

        assertThrows(IllegalStateException.class, () ->
                idempotency.executeOnce(key, tenantA, "run.dispatch", body, () -> {
                    throw new IllegalStateException("transient failure");
                }));

        AtomicInteger sideEffects = new AtomicInteger();
        var retry = idempotency.executeOnce(key, tenantA, "run.dispatch", body, () -> {
            sideEffects.incrementAndGet();
            return new IdempotencyService.Outcome(200, "ok on retry");
        });
        assertTrue(retry.firstExecution(), "失败的执行不应占用幂等键");
        assertEquals(1, sideEffects.get());
    }

    @Test
    @DisplayName("payload 含浮点数被拒绝（沿用 ADR-0002 规则）")
    void floatPayloadIsRejected() {
        String key = "run.dispatch:" + UUID.randomUUID() + ":1";
        assertThrows(io.runbookguard.controlplane.approval.NonCanonicalizableArgumentException.class,
                () -> idempotency.executeOnce(key, tenantA, "run.dispatch",
                        Map.of("rate", 1.5), () -> new IdempotencyService.Outcome(200, "ok")));
    }
}
