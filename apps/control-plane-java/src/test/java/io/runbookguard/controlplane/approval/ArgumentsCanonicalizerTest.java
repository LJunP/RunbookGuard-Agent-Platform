package io.runbookguard.controlplane.approval;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.util.LinkedHashMap;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

/**
 * ADR-0002 的可执行规格。摘要一旦在两侧算出不同值，T-2 的缓解就失效并表现为"合法请求被误拒"，
 * 因此这里的断言比通常的单测更严格：拒绝规则也是契约的一部分。
 */
class ArgumentsCanonicalizerTest {

    private final ArgumentsCanonicalizer canonicalizer = new ArgumentsCanonicalizer();

    @Test
    @DisplayName("键顺序不影响摘要")
    void keyOrderDoesNotAffectDigest() {
        Map<String, Object> a = new LinkedHashMap<>();
        a.put("service", "synthetic-orders");
        a.put("target_version", "v1.4.2");

        Map<String, Object> b = new LinkedHashMap<>();
        b.put("target_version", "v1.4.2");
        b.put("service", "synthetic-orders");

        assertEquals(canonicalizer.digest(a), canonicalizer.digest(b));
    }

    @Test
    @DisplayName("规范化输出按 UTF-16 码元字典序排键，无多余空白")
    void canonicalFormIsSortedAndCompact() {
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("b", 2);
        args.put("a", 1);
        args.put("A", 0);
        assertEquals("{\"A\":0,\"a\":1,\"b\":2}", canonicalizer.canonicalize(args));
    }

    @Test
    @DisplayName("嵌套对象的键同样排序")
    void nestedKeysAreSorted() {
        Map<String, Object> inner = new LinkedHashMap<>();
        inner.put("z", "1");
        inner.put("y", "2");
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("outer", inner);
        assertEquals("{\"outer\":{\"y\":\"2\",\"z\":\"1\"}}", canonicalizer.canonicalize(args));
    }

    @Test
    @DisplayName("非 ASCII 字符不转义，直接输出 UTF-8")
    void nonAsciiIsNotEscaped() {
        Map<String, Object> args = Map.of("title", "订单服务");
        assertEquals("{\"title\":\"订单服务\"}", canonicalizer.canonicalize(args));
    }

    @Test
    @DisplayName("控制字符使用 RFC 8785 规定的短形式或 \\u00XX")
    void controlCharactersUseShortForms() {
        Map<String, Object> args = Map.of("k", "a\nb\tc\u0001d\"e\\f");
        assertEquals("{\"k\":\"a\\nb\\tc\\u0001d\\\"e\\\\f\"}", canonicalizer.canonicalize(args));
    }

    @Test
    @DisplayName("整数序列化为最短十进制形式")
    void integersUseShortestForm() {
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("zero", 0);
        args.put("neg", -42);
        args.put("big", 9007199254740991L);
        assertEquals("{\"big\":9007199254740991,\"neg\":-42,\"zero\":0}", canonicalizer.canonicalize(args));
    }

    @Test
    @DisplayName("超出双精度可精确表示范围的整数被拒绝")
    void integerBeyondSafeRangeIsRejected() {
        assertThrows(NonCanonicalizableArgumentException.class,
                () -> canonicalizer.canonicalize(Map.of("n", 9007199254740992L)));
        assertThrows(NonCanonicalizableArgumentException.class,
                () -> canonicalizer.canonicalize(Map.of("n", -9007199254740992L)));
    }

    @Test
    @DisplayName("边界整数 ±(2^53-1) 允许")
    void safeRangeBoundariesAreAccepted() {
        assertEquals("{\"n\":9007199254740991}", canonicalizer.canonicalize(Map.of("n", 9007199254740991L)));
        assertEquals("{\"n\":-9007199254740991}", canonicalizer.canonicalize(Map.of("n", -9007199254740991L)));
    }

    @Test
    @DisplayName("浮点数被拒绝（ADR-0002 收窄输入域）")
    void floatingPointIsRejected() {
        assertThrows(NonCanonicalizableArgumentException.class,
                () -> canonicalizer.canonicalize(Map.of("rate", 12.5)));
        assertThrows(NonCanonicalizableArgumentException.class,
                () -> canonicalizer.canonicalize(Map.of("rate", Double.NaN)));
        assertThrows(NonCanonicalizableArgumentException.class,
                () -> canonicalizer.canonicalize(Map.of("rate", Double.POSITIVE_INFINITY)));
    }

    @Test
    @DisplayName("null 允许，且 {\"a\":null} 与 {} 摘要不同（不做空值折叠）")
    void nullIsPreservedAndNotFolded() {
        Map<String, Object> withNull = new LinkedHashMap<>();
        withNull.put("a", null);
        assertEquals("{\"a\":null}", canonicalizer.canonicalize(withNull));
        assertNotEquals(canonicalizer.digest(withNull), canonicalizer.digest(Map.of()));
    }

    @Test
    @DisplayName("布尔与数组按 JSON 原样序列化")
    void booleansAndArrays() {
        Map<String, Object> args = new LinkedHashMap<>();
        args.put("flag", true);
        args.put("items", java.util.List.of("b", "a", 1));
        assertEquals("{\"flag\":true,\"items\":[\"b\",\"a\",1]}", canonicalizer.canonicalize(args));
    }

    @Test
    @DisplayName("嵌套深度超过 8 被拒绝")
    void excessiveNestingIsRejected() {
        Object cursor = "leaf";
        for (int i = 0; i < 9; i++) {
            cursor = Map.of("k", cursor);
        }
        final Object deep = cursor;
        @SuppressWarnings("unchecked")
        Map<String, Object> args = (Map<String, Object>) deep;
        assertThrows(NonCanonicalizableArgumentException.class, () -> canonicalizer.canonicalize(args));
    }

    @Test
    @DisplayName("规范化长度超过 8 KiB 被拒绝")
    void oversizedArgumentsAreRejected() {
        String big = "x".repeat(9 * 1024);
        assertThrows(NonCanonicalizableArgumentException.class,
                () -> canonicalizer.canonicalize(Map.of("blob", big)));
    }

    @Test
    @DisplayName("未配对的 UTF-16 代理项被拒绝")
    void unpairedSurrogateIsRejected() {
        assertThrows(NonCanonicalizableArgumentException.class,
                () -> canonicalizer.canonicalize(Map.of("k", "a\uD800b")));
    }

    @Test
    @DisplayName("摘要为 64 位小写十六进制 SHA-256")
    void digestShape() {
        String digest = canonicalizer.digest(Map.of("service", "synthetic-orders"));
        assertEquals(64, digest.length());
        assertEquals(digest.toLowerCase(), digest);
        // 固定向量：{"service":"synthetic-orders"} 的 SHA-256
        assertEquals(sha256Hex("{\"service\":\"synthetic-orders\"}"), digest);
    }

    @Test
    @DisplayName("参数任一字节变化都改变摘要")
    void anyChangeAltersDigest() {
        String original = canonicalizer.digest(Map.of("service", "synthetic-orders"));
        String tampered = canonicalizer.digest(Map.of("service", "synthetic-db"));
        assertNotEquals(original, tampered);
    }

    @Test
    @DisplayName("算法标识固定为 JCS-SHA256-V1")
    void algorithmIdentifierIsVersioned() {
        assertEquals("JCS-SHA256-V1", ArgumentsCanonicalizer.DIGEST_ALG);
    }

    private static String sha256Hex(String s) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] out = md.digest(s.getBytes(java.nio.charset.StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder(64);
            for (byte b : out) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
