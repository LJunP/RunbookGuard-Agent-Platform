package io.runbookguard.controlplane.approval;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * ADR-0002：RFC 8785 JCS 的受限子集 + SHA-256。是 JCS 子集而非完整实现——浮点数被拒绝而不是按
 * ECMAScript Number::toString 序列化，因此不得对外声称实现了 RFC 8785。
 *
 * <p>所有歧义输入 fail-closed。放宽任何一条拒绝规则前先想清楚：两个语义不同的参数拿到同一个摘要，
 * 等于审批可以被绕过。
 */
public class ArgumentsCanonicalizer {

    public static final String DIGEST_ALG = "JCS-SHA256-V1";

    static final long MAX_SAFE_INTEGER = 9007199254740991L;
    static final int MAX_DEPTH = 8;
    static final int MAX_CANONICAL_BYTES = 8 * 1024;

    /** UTF-16 码元序列的字典序，即 Java String 的自然序（RFC 8785 §3.2.3）。 */
    private static final Comparator<String> KEY_ORDER = Comparator.naturalOrder();

    public String canonicalize(Map<String, Object> arguments) {
        if (arguments == null) {
            throw new NonCanonicalizableArgumentException("arguments must not be null");
        }
        StringBuilder out = new StringBuilder();
        writeObject(arguments, out, 1);
        String canonical = out.toString();
        int bytes = canonical.getBytes(StandardCharsets.UTF_8).length;
        if (bytes > MAX_CANONICAL_BYTES) {
            throw new NonCanonicalizableArgumentException(
                    "canonical form exceeds " + MAX_CANONICAL_BYTES + " bytes: " + bytes);
        }
        return canonical;
    }

    public String digest(Map<String, Object> arguments) {
        byte[] hash = sha256(canonicalize(arguments).getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder(hash.length * 2);
        for (byte b : hash) {
            hex.append(Character.forDigit((b >> 4) & 0xF, 16));
            hex.append(Character.forDigit(b & 0xF, 16));
        }
        return hex.toString();
    }

    private void writeObject(Map<?, ?> map, StringBuilder out, int depth) {
        requireDepth(depth);
        List<String> keys = new ArrayList<>(map.size());
        Set<String> seen = new HashSet<>();
        for (Object rawKey : map.keySet()) {
            if (!(rawKey instanceof String key)) {
                throw new NonCanonicalizableArgumentException(
                        "object keys must be strings, got " + typeName(rawKey));
            }
            if (!seen.add(key)) {
                throw new NonCanonicalizableArgumentException("duplicate key: " + key);
            }
            keys.add(key);
        }
        keys.sort(KEY_ORDER);

        out.append('{');
        boolean first = true;
        for (String key : keys) {
            if (!first) {
                out.append(',');
            }
            first = false;
            writeString(key, out);
            out.append(':');
            writeValue(map.get(key), out, depth + 1);
        }
        out.append('}');
    }

    private void writeValue(Object value, StringBuilder out, int depth) {
        requireDepth(depth);
        if (value == null) {
            out.append("null");
        } else if (value instanceof String s) {
            writeString(s, out);
        } else if (value instanceof Boolean b) {
            out.append(b ? "true" : "false");
        } else if (value instanceof Map<?, ?> m) {
            writeObject(m, out, depth);
        } else if (value instanceof Collection<?> c) {
            writeArray(c, out, depth);
        } else if (value instanceof Object[] a) {
            writeArray(List.of(a), out, depth);
        } else if (value instanceof Byte || value instanceof Short
                || value instanceof Integer || value instanceof Long) {
            writeInteger(((Number) value).longValue(), out);
        } else if (value instanceof java.math.BigInteger bi) {
            if (bi.bitLength() > 63) {
                throw new NonCanonicalizableArgumentException("integer out of safe range: " + bi);
            }
            writeInteger(bi.longValueExact(), out);
        } else if (value instanceof Float || value instanceof Double
                || value instanceof java.math.BigDecimal) {
            throw new NonCanonicalizableArgumentException(
                    "non-integer numbers are rejected (ADR-0002); use integers or strings: " + value);
        } else {
            throw new NonCanonicalizableArgumentException("unsupported value type: " + typeName(value));
        }
    }

    private void writeArray(Collection<?> items, StringBuilder out, int depth) {
        out.append('[');
        boolean first = true;
        for (Object item : items) {
            if (!first) {
                out.append(',');
            }
            first = false;
            writeValue(item, out, depth + 1);
        }
        out.append(']');
    }

    private void writeInteger(long value, StringBuilder out) {
        if (value > MAX_SAFE_INTEGER || value < -MAX_SAFE_INTEGER) {
            throw new NonCanonicalizableArgumentException(
                    "integer outside +/-(2^53-1), not exactly representable as double: " + value);
        }
        out.append(value);
    }

    private void writeString(String s, StringBuilder out) {
        out.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20) {
                        out.append(String.format("\\u%04x", (int) c));
                    } else if (Character.isHighSurrogate(c)) {
                        if (i + 1 >= s.length() || !Character.isLowSurrogate(s.charAt(i + 1))) {
                            throw new NonCanonicalizableArgumentException(
                                    "unpaired high surrogate at index " + i);
                        }
                        out.append(c).append(s.charAt(++i));
                    } else if (Character.isLowSurrogate(c)) {
                        throw new NonCanonicalizableArgumentException(
                                "unpaired low surrogate at index " + i);
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        out.append('"');
    }

    private void requireDepth(int depth) {
        if (depth > MAX_DEPTH) {
            throw new NonCanonicalizableArgumentException("nesting depth exceeds " + MAX_DEPTH);
        }
    }

    private static String typeName(Object o) {
        return o == null ? "null" : o.getClass().getName();
    }

    private static byte[] sha256(byte[] input) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(input);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
