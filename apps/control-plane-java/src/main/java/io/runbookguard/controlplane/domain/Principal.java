package io.runbookguard.controlplane.domain;

import java.time.Instant;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.Set;

public record Principal(
        String principalId,
        String tenantId,
        String displayName,
        String roles,
        String apiTokenHash,
        boolean enabled,
        Instant createdAt) {

    public Set<Role> roleSet() {
        Set<Role> parsed = new LinkedHashSet<>();
        if (roles == null || roles.isBlank()) {
            return parsed;
        }
        Arrays.stream(roles.split(","))
                .map(String::trim)
                .filter(s -> !s.isEmpty())
                .forEach(s -> parsed.add(Role.valueOf(s)));
        return parsed;
    }

    public boolean hasRole(Role role) {
        return roleSet().contains(role);
    }

    /** toString 会进日志与异常信息，token hash 不该出现在那里。 */
    @Override
    public String toString() {
        return "Principal[principalId=%s, tenantId=%s, roles=%s]".formatted(principalId, tenantId, roles);
    }
}
