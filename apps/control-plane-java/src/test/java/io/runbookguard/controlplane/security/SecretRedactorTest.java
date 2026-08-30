package io.runbookguard.controlplane.security;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** 威胁 T-3。注意这些断言证明的是"这些模式会被遮盖"，不是"所有 Secret 都会被遮盖"。 */
class SecretRedactorTest {

    @Test
    @DisplayName("key=value 形态的凭据被遮盖，键名保留")
    void keyValueSecretsAreMasked() {
        String out = SecretRedactor.redact("db connect failed: password=Sup3rS3cret! host=mysql");
        assertFalse(out.contains("Sup3rS3cret!"), out);
        assertTrue(out.contains("password="), "键名应保留以便排查");
        assertTrue(out.contains(SecretRedactor.MASK), out);
    }

    @Test
    @DisplayName("JSON 形态的 api_key 被遮盖")
    void jsonApiKeyIsMasked() {
        String out = SecretRedactor.redact("{\"api_key\": \"abcd1234efgh5678\", \"model\": \"gpt\"}");
        assertFalse(out.contains("abcd1234efgh5678"), out);
        assertTrue(out.contains("model"), "非敏感字段不应被破坏");
    }

    @Test
    @DisplayName("OpenAI 风格 sk- 前缀被遮盖")
    void openAiStyleKeyIsMasked() {
        String out = SecretRedactor.redact("using key sk-abcdefghijklmnopqrstuvwx for provider");
        assertFalse(out.contains("sk-abcdefghijklmnopqrstuvwx"), out);
    }

    @Test
    @DisplayName("AWS access key id 被遮盖")
    void awsKeyIsMasked() {
        String out = SecretRedactor.redact("caller AKIAIOSFODNN7EXAMPLE denied");
        assertFalse(out.contains("AKIAIOSFODNN7EXAMPLE"), out);
    }

    @Test
    @DisplayName("GitHub token 被遮盖")
    void githubTokenIsMasked() {
        String out = SecretRedactor.redact("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 rejected");
        assertFalse(out.contains("ghp_abcdefghijklmnopqrstuvwxyz0123456789"), out);
    }

    @Test
    @DisplayName("JDBC URL 中的密码被遮盖，其余部分保留")
    void jdbcPasswordIsMasked() {
        String out = SecretRedactor.redact(
                "jdbc:mysql://db:3306/rg?user=rg&password=hunter2secret&useSSL=false");
        assertFalse(out.contains("hunter2secret"), out);
        assertTrue(out.contains("jdbc:mysql://db:3306/rg"), out);
    }

    @Test
    @DisplayName("Authorization 头被遮盖")
    void authorizationHeaderIsMasked() {
        String out = SecretRedactor.redact("Authorization: Bearer abcdef1234567890xyz");
        assertFalse(out.contains("abcdef1234567890xyz"), out);
    }

    @Test
    @DisplayName("JWT 被遮盖")
    void jwtIsMasked() {
        String jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r";
        String out = SecretRedactor.redact("cookie contains " + jwt);
        assertFalse(out.contains(jwt), out);
    }

    @Test
    @DisplayName("不含凭据的文本原样返回")
    void cleanTextIsUnchanged() {
        String clean = "connection pool exhausted for synthetic-orders at 2026-08-26T10:00:00Z";
        assertEquals(clean, SecretRedactor.redact(clean));
    }

    @Test
    @DisplayName("null 与空串安全处理")
    void nullAndEmptyAreSafe() {
        assertEquals(null, SecretRedactor.redact(null));
        assertEquals("", SecretRedactor.redact(""));
    }

    @Test
    @DisplayName("Principal.toString 不包含 token hash")
    void principalToStringOmitsTokenHash() {
        var principal = new io.runbookguard.controlplane.domain.Principal(
                "prin-1", "tenant-1", "alice", "OPERATOR",
                TokenHasher.hash("super-secret-token"), true, java.time.Instant.now());
        String rendered = principal.toString();
        assertFalse(rendered.contains(TokenHasher.hash("super-secret-token")), rendered);
        assertTrue(rendered.contains("prin-1"), rendered);
    }

    @Test
    @DisplayName("明文 token 不可从 hash 反推：hash 稳定且长度固定")
    void tokenHashIsStableAndFixedLength() {
        String h1 = TokenHasher.hash("token-abc");
        String h2 = TokenHasher.hash("token-abc");
        assertEquals(h1, h2);
        assertEquals(64, h1.length());
        assertFalse(h1.contains("token-abc"));
    }
}
