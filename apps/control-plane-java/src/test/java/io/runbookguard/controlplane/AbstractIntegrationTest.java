package io.runbookguard.controlplane;

import com.redis.testcontainers.RedisContainer;
import io.runbookguard.controlplane.domain.Principal;
import io.runbookguard.controlplane.persistence.PrincipalMapper;
import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.security.TokenHasher;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.MySQLContainer;
import org.testcontainers.junit.jupiter.Testcontainers;
import org.testcontainers.utility.DockerImageName;

import java.time.Instant;
import java.util.UUID;

/**
 * 真实 MySQL 8 + 真实 Redis，不用 H2 也不用 mock。触发器、乐观锁行为、主键冲突这些正是
 * M1 要证明的东西，换成内存数据库就等于没测。
 *
 * <p>容器用 static 且不加 @Container 自动生命周期，让整个测试类树共用一份实例，避免每个类
 * 重启 MySQL（一次约 10 秒）。
 */
@SpringBootTest
@Testcontainers
public abstract class AbstractIntegrationTest {

    static final MySQLContainer<?> MYSQL = new MySQLContainer<>(DockerImageName.parse("mysql:8.0"))
            .withDatabaseName("runbookguard")
            .withUsername("runbookguard")
            .withPassword("test-only-password")
            // 审计不可篡改依赖触发器；binlog 开启时创建触发器需要 SUPER，否则报 1419。
            // 部署侧的等价配置在 deploy/compose/。
            .withCommand("--log-bin-trust-function-creators=1");

    static final RedisContainer REDIS = new RedisContainer(DockerImageName.parse("redis:7-alpine"));

    static final org.testcontainers.containers.RabbitMQContainer RABBIT =
            new org.testcontainers.containers.RabbitMQContainer(
                    DockerImageName.parse("rabbitmq:3.13-management-alpine"));

    static {
        MYSQL.start();
        REDIS.start();
        RABBIT.start();
    }

    @DynamicPropertySource
    static void datasourceProperties(DynamicPropertyRegistry registry) {
        registry.add("spring.datasource.url", MYSQL::getJdbcUrl);
        registry.add("spring.datasource.username", MYSQL::getUsername);
        registry.add("spring.datasource.password", MYSQL::getPassword);
        registry.add("spring.data.redis.host", REDIS::getHost);
        registry.add("spring.data.redis.port", () -> REDIS.getMappedPort(6379));
        registry.add("spring.rabbitmq.host", RABBIT::getHost);
        registry.add("spring.rabbitmq.port", RABBIT::getAmqpPort);
        registry.add("spring.rabbitmq.username", RABBIT::getAdminUsername);
        registry.add("spring.rabbitmq.password", RABBIT::getAdminPassword);
    }

    @Autowired
    protected PrincipalMapper principalMapper;

    @Autowired
    protected org.springframework.data.redis.core.StringRedisTemplate redis;

    protected String tenantA;
    protected String tenantB;

    @BeforeEach
    void seedTenants() {
        // 每个测试独立租户，避免限流计数与数据互相污染。
        tenantA = "tenant-a-" + UUID.randomUUID();
        tenantB = "tenant-b-" + UUID.randomUUID();
        principalMapper.insertTenant(tenantA, "Tenant A", Instant.now());
        principalMapper.insertTenant(tenantB, "Tenant B", Instant.now());
        redis.getConnectionFactory().getConnection().serverCommands().flushDb();
    }

    protected AuthenticatedCaller caller(String tenantId, String roles) {
        String token = "tok-" + UUID.randomUUID();
        Principal p = new Principal("prin-" + UUID.randomUUID(), tenantId,
                "test-" + roles, roles, TokenHasher.hash(token), true, Instant.now());
        principalMapper.insertPrincipal(p);
        return new AuthenticatedCaller(p);
    }

    protected String tokenFor(AuthenticatedCaller caller) {
        throw new UnsupportedOperationException(
                "plaintext tokens are not recoverable; use callerWithToken() for HTTP tests");
    }

    protected record CallerWithToken(AuthenticatedCaller caller, String token) {
    }

    protected CallerWithToken callerWithToken(String tenantId, String roles) {
        String token = "tok-" + UUID.randomUUID();
        Principal p = new Principal("prin-" + UUID.randomUUID(), tenantId,
                "test-" + roles, roles, TokenHasher.hash(token), true, Instant.now());
        principalMapper.insertPrincipal(p);
        return new CallerWithToken(new AuthenticatedCaller(p), token);
    }
}
