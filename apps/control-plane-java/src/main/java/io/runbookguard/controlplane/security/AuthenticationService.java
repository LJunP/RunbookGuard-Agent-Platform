package io.runbookguard.controlplane.security;

import io.runbookguard.controlplane.domain.Principal;
import io.runbookguard.controlplane.persistence.PrincipalMapper;
import org.springframework.stereotype.Component;

import java.util.Optional;

@Component
public class AuthenticationService {

    private static final String BEARER = "Bearer ";

    private final PrincipalMapper principalMapper;

    public AuthenticationService(PrincipalMapper principalMapper) {
        this.principalMapper = principalMapper;
    }

    public Optional<AuthenticatedCaller> authenticate(String authorizationHeader) {
        if (authorizationHeader == null || !authorizationHeader.startsWith(BEARER)) {
            return Optional.empty();
        }
        String token = authorizationHeader.substring(BEARER.length()).trim();
        if (token.isEmpty()) {
            return Optional.empty();
        }
        Principal principal = principalMapper.findByTokenHash(TokenHasher.hash(token));
        return Optional.ofNullable(principal).map(AuthenticatedCaller::new);
    }
}
