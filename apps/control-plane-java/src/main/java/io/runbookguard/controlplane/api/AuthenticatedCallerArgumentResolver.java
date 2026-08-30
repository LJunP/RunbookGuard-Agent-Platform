package io.runbookguard.controlplane.api;

import io.runbookguard.controlplane.security.AuthenticatedCaller;
import io.runbookguard.controlplane.security.AuthenticationService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.core.MethodParameter;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.support.WebDataBinderFactory;
import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.web.method.support.ModelAndViewContainer;

/**
 * 让 Controller 方法直接声明 AuthenticatedCaller 参数。这样"忘记鉴权"会表现为编译期就
 * 拿不到 tenantId，而不是运行时静默使用了客户端自报的 tenant。
 */
@Component
public class AuthenticatedCallerArgumentResolver implements HandlerMethodArgumentResolver {

    private final AuthenticationService authenticationService;

    public AuthenticatedCallerArgumentResolver(AuthenticationService authenticationService) {
        this.authenticationService = authenticationService;
    }

    @Override
    public boolean supportsParameter(MethodParameter parameter) {
        return AuthenticatedCaller.class.equals(parameter.getParameterType());
    }

    @Override
    public Object resolveArgument(MethodParameter parameter, ModelAndViewContainer mavContainer,
                                  NativeWebRequest webRequest, WebDataBinderFactory binderFactory) {
        HttpServletRequest request = webRequest.getNativeRequest(HttpServletRequest.class);
        String header = request == null ? null : request.getHeader("Authorization");
        return authenticationService.authenticate(header)
                .orElseThrow(() -> new UnauthenticatedException("missing or invalid credentials"));
    }
}
