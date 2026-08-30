package io.runbookguard.controlplane.ratelimit;

import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.data.redis.core.script.RedisScript;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.List;

/**
 * 固定窗口计数器，用 Lua 保证 INCR 与 EXPIRE 的原子性。分两条命令时，进程在 INCR 之后崩溃
 * 会留下无 TTL 的键，该 principal 从此永久被限流。
 *
 * <p>选固定窗口而非滑动窗口：本项目的限流目的是防失控循环与误用，不是精确整形。窗口边界处
 * 可能放过接近 2 倍配额，这个偏差可以接受。
 */
@Component
public class RedisRateLimiter {

    private static final RedisScript<Long> INCR_WITH_TTL = new DefaultRedisScript<>(
            """
            local current = redis.call('INCR', KEYS[1])
            if current == 1 then
              redis.call('PEXPIRE', KEYS[1], ARGV[1])
            end
            return current
            """, Long.class);

    private final StringRedisTemplate redis;

    public RedisRateLimiter(StringRedisTemplate redis) {
        this.redis = redis;
    }

    /** @return true 表示放行 */
    public boolean tryAcquire(String bucket, int limitPerWindow, Duration window) {
        Long count = redis.execute(INCR_WITH_TTL, List.of("rl:" + bucket),
                String.valueOf(window.toMillis()));
        return count != null && count <= limitPerWindow;
    }
}
