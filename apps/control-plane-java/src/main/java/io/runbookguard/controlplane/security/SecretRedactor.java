package io.runbookguard.controlplane.security;

import java.util.List;
import java.util.regex.Pattern;

/**
 * 出口集中脱敏（威胁 T-3）。放在写日志/审计/响应的那一层，而不是依赖每个调用点自觉——
 * 后者只要漏一处就是永久泄漏。
 *
 * <p>已知局限：基于模式匹配，无法覆盖任意格式的凭据。它是纵深防御的一层，不是唯一防线；
 * 真正的防线是 Secret 只从环境变量注入、不进入任何数据结构。
 */
public final class SecretRedactor {

    public static final String MASK = "***REDACTED***";

    /**
     * 顺序重要：具体模式先跑，通用 key=value 最后。反过来会让 "Authorization: Bearer xxx"
     * 被通用模式匹配成 key=Authorization、value=Bearer，只遮住 "Bearer" 而漏掉真正的 token。
     */
    private static final List<Pattern> PATTERNS = List.of(
            // 常见 API key 前缀
            Pattern.compile("\\bsk-[A-Za-z0-9_-]{16,}\\b"),
            Pattern.compile("\\bgh[pousr]_[A-Za-z0-9]{20,}\\b"),
            Pattern.compile("\\bAKIA[0-9A-Z]{16}\\b"),
            // JWT
            Pattern.compile("\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{4,}\\b"),
            // JDBC URL 中的密码
            Pattern.compile("(?i)(jdbc:[^\\s]*?password=)([^&\\s]+)"),
            // Basic / Bearer 认证头
            Pattern.compile("(?i)\\b(Basic|Bearer)\\s+[A-Za-z0-9._~+/=-]{8,}"),
            // key/value 形态：password=xxx、"api_key": "xxx"、token: xxx
            // 键名后允许一个引号，覆盖 JSON 的 "api_key": "..." 写法。
            Pattern.compile("(?i)\\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
                    + "private[_-]?key|credential|authorization|bearer)\\b\"?\\s*[:=]\\s*"
                    + "\"?([^\"\\s,;}]{4,})\"?")
    );

    private SecretRedactor() {
    }

    public static String redact(String input) {
        if (input == null || input.isEmpty()) {
            return input;
        }
        String out = input;
        for (Pattern p : PATTERNS) {
            java.util.regex.Matcher m = p.matcher(out);
            StringBuilder sb = new StringBuilder();
            int last = 0;
            while (m.find()) {
                sb.append(out, last, m.start());
                if (m.groupCount() >= 2) {
                    // 保留键名，只遮盖值，便于排查"哪个凭据被误传了"而不暴露值本身。
                    sb.append(out, m.start(), m.start(2)).append(MASK);
                } else {
                    sb.append(MASK);
                }
                last = m.end();
            }
            sb.append(out.substring(last));
            out = sb.toString();
        }
        return out;
    }
}
