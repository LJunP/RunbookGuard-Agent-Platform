package io.runbookguard.controlplane.approval;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.DynamicTest;

import java.io.IOException;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * 跨语言摘要一致性（M1 Gate 报告 §5.2 的遗留项）。
 *
 * <p>向量由 Python 侧生成，Java 侧在这里校验。两侧算出不同摘要时，审批时绑定的 digest
 * 与执行时重算的不一致，威胁 T-2 的缓解直接失效——表现为合法请求被误拒，或更糟，
 * 两个语义不同的参数拿到同一摘要。
 *
 * <p>刻意不共享实现：跨语言共享一段规范化代码会引入必须同步的隐式依赖，
 * 而两套独立实现 + 同一份向量能真正印证规则一致。
 */
class DigestCrossLanguageVectorTest {

    private static final Path VECTORS = Path.of("../../contracts/tools/digest-test-vectors.json");

    private final ArgumentsCanonicalizer canonicalizer = new ArgumentsCanonicalizer();
    private final ObjectMapper mapper = new ObjectMapper();

    private JsonNode loadVectors() throws IOException {
        assertTrue(Files.exists(VECTORS),
                "缺少跨语言测试向量：" + VECTORS.toAbsolutePath().normalize());
        return mapper.readTree(Files.readString(VECTORS));
    }

    @Test
    @DisplayName("向量文件声明的算法标识与 Java 实现一致")
    void algorithmIdentifierMatches() throws IOException {
        assertEquals(ArgumentsCanonicalizer.DIGEST_ALG,
                loadVectors().get("digest_alg").asText(),
                "算法标识不一致意味着两侧在算不同的东西");
    }

    @TestFactory
    @DisplayName("accept 向量：规范化输出与摘要必须逐字节一致")
    List<DynamicTest> acceptVectorsMatch() throws IOException {
        JsonNode accept = loadVectors().get("accept");
        List<DynamicTest> tests = new ArrayList<>();
        for (JsonNode vector : accept) {
            String name = vector.get("name").asText();
            tests.add(DynamicTest.dynamicTest("accept/" + name, () -> {
                Map<String, Object> args = toJavaMap(vector.get("arguments"));
                assertEquals(vector.get("canonical").asText(), canonicalizer.canonicalize(args),
                        "canonical form mismatch for vector " + name);
                assertEquals(vector.get("digest").asText(), canonicalizer.digest(args),
                        "digest mismatch for vector " + name);
            }));
        }
        assertTrue(tests.size() >= 15, "accept 向量过少：" + tests.size());
        return tests;
    }

    @TestFactory
    @DisplayName("reject 向量：Java 侧必须同样拒绝")
    List<DynamicTest> rejectVectorsAreRejected() throws IOException {
        JsonNode reject = loadVectors().get("reject");
        List<DynamicTest> tests = new ArrayList<>();
        for (JsonNode vector : reject) {
            String name = vector.get("name").asText();
            tests.add(DynamicTest.dynamicTest("reject/" + name, () -> {
                JsonNode argsNode = vector.get("arguments");
                if (!argsNode.isObject()) {
                    // 顶层非对象：Java 的类型系统在调用点就挡住了，这里断言语义等价。
                    assertTrue(argsNode.isArray() || argsNode.isValueNode(),
                            "non-object top level vector " + name);
                    return;
                }
                Map<String, Object> args = toJavaMap(argsNode);
                assertThrows(NonCanonicalizableArgumentException.class,
                        () -> canonicalizer.canonicalize(args),
                        "vector " + name + " should have been rejected");
            }));
        }
        assertTrue(tests.size() >= 6, "reject 向量过少：" + tests.size());
        return tests;
    }

    @Test
    @DisplayName("键序不同的两个向量摘要相同（不误拒的正面证据）")
    void keyOrderVectorsAgree() throws IOException {
        JsonNode accept = loadVectors().get("accept");
        String basic = null;
        String reordered = null;
        for (JsonNode v : accept) {
            if ("basic".equals(v.get("name").asText())) {
                basic = canonicalizer.digest(toJavaMap(v.get("arguments")));
            }
            if ("key-order-differs".equals(v.get("name").asText())) {
                reordered = canonicalizer.digest(toJavaMap(v.get("arguments")));
            }
        }
        assertEquals(basic, reordered);
    }

    @Test
    @DisplayName("显式 null 与空对象摘要不同（不做空值折叠）")
    void nullIsNotFolded() throws IOException {
        JsonNode accept = loadVectors().get("accept");
        String withNull = null;
        String empty = null;
        for (JsonNode v : accept) {
            if ("explicit-null".equals(v.get("name").asText())) {
                withNull = canonicalizer.digest(toJavaMap(v.get("arguments")));
            }
            if ("empty-object".equals(v.get("name").asText())) {
                empty = canonicalizer.digest(toJavaMap(v.get("arguments")));
            }
        }
        assertTrue(withNull != null && empty != null, "向量缺失");
        assertTrue(!withNull.equals(empty),
                "折叠空值会让两个不同的工具调用共享一个摘要");
    }

    /**
     * Jackson 的数值节点默认给出 Integer/Long/Double。这里显式区分整数与浮点：
     * 若把 12.5 读成 BigDecimal 再交给 canonicalizer，它会走浮点拒绝分支——
     * 这正是我们要断言的行为，因此不能在这一层就转成字符串。
     */
    private Map<String, Object> toJavaMap(JsonNode node) {
        Map<String, Object> out = new LinkedHashMap<>();
        node.fields().forEachRemaining(entry -> out.put(entry.getKey(), toJavaValue(entry.getValue())));
        return out;
    }

    private Object toJavaValue(JsonNode node) {
        if (node.isNull()) {
            return null;
        }
        if (node.isTextual()) {
            return node.asText();
        }
        if (node.isBoolean()) {
            return node.asBoolean();
        }
        if (node.isIntegralNumber()) {
            return node.canConvertToLong() ? (Object) node.asLong() : new BigInteger(node.asText());
        }
        if (node.isFloatingPointNumber()) {
            return node.asDouble();
        }
        if (node.isObject()) {
            return toJavaMap(node);
        }
        if (node.isArray()) {
            List<Object> items = new ArrayList<>();
            node.forEach(child -> items.add(toJavaValue(child)));
            return items;
        }
        throw new IllegalStateException("unhandled node type: " + node.getNodeType());
    }
}
