package io.runbookguard.controlplane.security;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

public final class TokenHasher {

    private TokenHasher() {
    }

    /**
     * 明文 token 从不落库。SHA-256 而非 bcrypt：token 是高熵随机串（非用户口令），
     * 不存在字典攻击面，而认证在每个请求上都要做一次，慢哈希会直接变成延迟。
     */
    public static String hash(String plaintextToken) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(plaintextToken.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(64);
            for (byte b : digest) {
                hex.append(Character.forDigit((b >> 4) & 0xF, 16));
                hex.append(Character.forDigit(b & 0xF, 16));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
