package io.runbookguard.controlplane.config;

import io.runbookguard.controlplane.api.AuthenticatedCallerArgumentResolver;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.time.Clock;
import java.util.List;

@Configuration
@EnableConfigurationProperties(RunbookGuardProperties.class)
public class ControlPlaneConfig implements WebMvcConfigurer {

    private final AuthenticatedCallerArgumentResolver callerResolver;
    private final RunbookGuardProperties props;

    public ControlPlaneConfig(AuthenticatedCallerArgumentResolver callerResolver,
                              RunbookGuardProperties props) {
        this.callerResolver = callerResolver;
        this.props = props;
    }

    /** 注入 Clock 而不是直接调 Instant.now()，否则审批过期这类时间相关逻辑无法测试。 */
    @Bean
    public Clock clock() {
        return Clock.systemUTC();
    }

    /**
     * 无状态纯函数组件，用 @Bean 注册而不在类上加 @Component：它必须能在没有 Spring 的
     * 单元测试里直接 new 出来（ArgumentsCanonicalizerTest 就是这么用的）。
     */
    @Bean
    public io.runbookguard.controlplane.approval.ArgumentsCanonicalizer argumentsCanonicalizer() {
        return new io.runbookguard.controlplane.approval.ArgumentsCanonicalizer();
    }

    @Override
    public void addArgumentResolvers(List<HandlerMethodArgumentResolver> resolvers) {
        resolvers.add(callerResolver);
    }

    /**
     * 控制台是独立源（Vite dev server 或 nginx），必须显式放行。
     *
     * <p>三点是刻意的：来源是白名单而非通配；不允许携带 cookie（allowCredentials 默认 false）
     * ——本项目的凭据走 Authorization 头，开 cookie 会引入 CSRF 面而没有任何收益；
     * 只放行读写 API 需要的方法，不放行 PUT/DELETE，因为 API 里根本没有它们。
     */
    @Override
    public void addCorsMappings(org.springframework.web.servlet.config.annotation.CorsRegistry registry) {
        registry.addMapping("/api/**")
                .allowedOrigins(props.console().allowedOrigins().toArray(String[]::new))
                .allowedMethods("GET", "POST", "PATCH", "OPTIONS")
                .allowedHeaders("Authorization", "Content-Type")
                .maxAge(600);
    }
}
