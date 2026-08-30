package io.runbookguard.controlplane.persistence;

import io.runbookguard.controlplane.domain.Principal;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

@Mapper
public interface PrincipalMapper {

    @Insert("""
            INSERT INTO tenant (tenant_id, display_name, created_at)
            VALUES (#{tenantId}, #{displayName}, #{createdAt})
            """)
    int insertTenant(@Param("tenantId") String tenantId,
                     @Param("displayName") String displayName,
                     @Param("createdAt") java.time.Instant createdAt);

    @Insert("""
            INSERT INTO principal (principal_id, tenant_id, display_name, roles, api_token_hash,
                                   enabled, created_at)
            VALUES (#{principalId}, #{tenantId}, #{displayName}, #{roles}, #{apiTokenHash},
                    #{enabled}, #{createdAt})
            """)
    int insertPrincipal(Principal principal);

    /** 只按 token hash 查，明文 token 不进 SQL 也不进日志。 */
    @Select("""
            SELECT principal_id, tenant_id, display_name, roles, api_token_hash, enabled, created_at
            FROM principal
            WHERE api_token_hash = #{tokenHash} AND enabled = 1
            """)
    Principal findByTokenHash(@Param("tokenHash") String tokenHash);

    @Select("""
            SELECT principal_id, tenant_id, display_name, roles, api_token_hash, enabled, created_at
            FROM principal
            WHERE principal_id = #{principalId}
            """)
    Principal findById(@Param("principalId") String principalId);
}
