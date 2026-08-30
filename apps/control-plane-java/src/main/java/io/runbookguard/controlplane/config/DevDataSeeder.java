package io.runbookguard.controlplane.config;

import io.runbookguard.controlplane.domain.Principal;
import io.runbookguard.controlplane.persistence.PrincipalMapper;
import io.runbookguard.controlplane.security.TokenHasher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

import java.time.Instant;

/**
 * 开发用种子数据，默认关闭，只在 RUNBOOKGUARD_SEED_DEV_DATA=true 时执行。
 *
 * <p>token 是写死的可读常量而不是随机生成：本地演示需要能照 README 复制粘贴。它们只在
 * 这个显式开关下写入，任何真实部署都不该打开这个开关。
 */
@Component
@ConditionalOnProperty(name = "runbookguard.seed-dev-data", havingValue = "true")
public class DevDataSeeder implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(DevDataSeeder.class);

    static final String TENANT = "tenant-demo";
    static final String TOKEN_OPERATOR = "dev-operator-token";
    static final String TOKEN_APPROVER = "dev-approver-token";
    static final String TOKEN_AGENT = "dev-agent-token";
    static final String TOKEN_VIEWER = "dev-viewer-token";

    private final PrincipalMapper mapper;

    public DevDataSeeder(PrincipalMapper mapper) {
        this.mapper = mapper;
    }

    @Override
    public void run(ApplicationArguments args) {
        if (mapper.findByTokenHash(TokenHasher.hash(TOKEN_OPERATOR)) != null) {
            log.info("dev seed data already present, skipping");
            return;
        }
        Instant now = Instant.now();
        mapper.insertTenant(TENANT, "Demo Tenant", now);
        seed("prin-operator", "demo-operator", "OPERATOR", TOKEN_OPERATOR, now);
        seed("prin-approver", "demo-approver", "APPROVER", TOKEN_APPROVER, now);
        seed("prin-agent", "demo-agent-runtime", "AGENT_RUNTIME", TOKEN_AGENT, now);
        seed("prin-viewer", "demo-viewer", "VIEWER", TOKEN_VIEWER, now);
        log.warn("seeded DEV principals with well-known tokens; never enable this outside local dev");
    }

    private void seed(String id, String name, String roles, String token, Instant now) {
        mapper.insertPrincipal(new Principal(id, TENANT, name, roles,
                TokenHasher.hash(token), true, now));
    }
}
