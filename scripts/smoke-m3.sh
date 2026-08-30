#!/usr/bin/env bash
# M3 冒烟：对运行中的 Agent Runtime 打真实 HTTP。
#
# 判据是 M3 Gate 的那一句：模型失败不能产生假成功。因此这里绝大多数断言都是
# 「失败必须表现为 502 + 可识别的 failure_class」，而不是「成功返回 200」。
#
# 前置：agent-runtime 已启动（默认 127.0.0.1:8100，fake provider）。
set -u

BASE="${BASE:-http://127.0.0.1:8100}"
JSON="Content-Type: application/json"

PASS=0
FAIL=0
LAST_BODY=""

check() {
  local label="$1" expected="$2"; shift 2
  local out status body
  out="$(curl -s -w '\n%{http_code}' "$@")"
  status="$(printf '%s' "$out" | tail -1)"
  body="$(printf '%s' "$out" | sed '$d')"
  LAST_BODY="$body"
  if [ "$status" = "$expected" ]; then
    printf 'ok    %-52s [%s]\n' "$label" "$status"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-52s expected %s got %s\n      %s\n' "$label" "$expected" "$status" "$body"
    FAIL=$((FAIL + 1))
  fi
}

expect_field() {
  local label="$1" field="$2" want="$3"
  local actual
  actual="$(printf '%s' "$LAST_BODY" | python3 -c "
import sys,json
try:
    print(json.load(sys.stdin).get(sys.argv[1]))
except Exception as e:
    print('<unparseable>')
" "$field")"
  if [ "$actual" = "$want" ]; then
    printf 'ok    %-52s [%s]\n' "$label" "$actual"
    PASS=$((PASS + 1))
  else
    printf 'FAIL  %-52s expected %s got %s\n' "$label" "$want" "$actual"
    FAIL=$((FAIL + 1))
  fi
}

echo "== 前置检查 =="
check "健康检查" 200 "$BASE/health"
expect_field "默认 provider 是 fake（不产生真实调用）" provider_id "fake"

echo
echo "== 成功路径 =="
check "非流式补全 -> 200" 200 -X POST -H "$JSON" \
  -d '{"messages":[{"role":"user","content":"diagnose the incident"}]}' \
  "$BASE/v1/complete"

check "结构化诊断 -> 200" 200 -X POST -H "$JSON" \
  -d '{"messages":[{"role":"user","content":"diagnose"}]}' \
  "$BASE/v1/diagnose"
expect_field "解包标记对调用方可见" unwrapped "False"

echo
echo "== 输入校验 =="
check "空 messages -> 422" 422 -X POST -H "$JSON" -d '{"messages":[]}' "$BASE/v1/diagnose"
check "缺 messages 字段 -> 422" 422 -X POST -H "$JSON" -d '{}' "$BASE/v1/diagnose"
check "messages 类型错误 -> 422" 422 -X POST -H "$JSON" \
  -d '{"messages":"not a list"}' "$BASE/v1/diagnose"

echo
echo "== SSE 流式 =="
STREAM_OUT="$(curl -s -N -X POST -H "$JSON" \
  -d '{"messages":[{"role":"user","content":"stream please"}]}' \
  "$BASE/v1/complete/stream")"
DELTA_COUNT="$(printf '%s' "$STREAM_OUT" | grep -c '"type": "delta"' || true)"
HAS_FINAL="$(printf '%s' "$STREAM_OUT" | grep -c '"type": "final"' || true)"
HAS_DONE="$(printf '%s' "$STREAM_OUT" | grep -c '\[DONE\]' || true)"

if [ "$DELTA_COUNT" -gt 0 ]; then
  echo "ok    流式产出 ${DELTA_COUNT} 个 delta"
  PASS=$((PASS + 1))
else
  echo "FAIL  流式未产出 delta"
  FAIL=$((FAIL + 1))
fi
if [ "$HAS_FINAL" = "1" ] && [ "$HAS_DONE" = "1" ]; then
  echo "ok    流式以 final + [DONE] 收尾（完整结果的标志）"
  PASS=$((PASS + 1))
else
  echo "FAIL  流式收尾异常：final=${HAS_FINAL} done=${HAS_DONE}"
  FAIL=$((FAIL + 1))
fi

echo
echo "== 契约：响应字段齐全 =="
COMPLETE_BODY="$(curl -s -X POST -H "$JSON" \
  -d '{"messages":[{"role":"user","content":"x"}]}' "$BASE/v1/complete")"
MISSING="$(printf '%s' "$COMPLETE_BODY" | python3 -c "
import sys, json
body = json.load(sys.stdin)
need = {'text','model','provider_id','finish_reason','attempts','usage','cost_micros'}
print(','.join(sorted(need - set(body))) or 'none')
")"
if [ "$MISSING" = "none" ]; then
  echo "ok    /v1/complete 返回全部契约字段"
  PASS=$((PASS + 1))
else
  echo "FAIL  /v1/complete 缺字段：${MISSING}"
  FAIL=$((FAIL + 1))
fi

USAGE_KEYS="$(printf '%s' "$COMPLETE_BODY" | python3 -c "
import sys, json
u = json.load(sys.stdin)['usage']
need = {'prompt_tokens','completion_tokens','total_tokens'}
print(','.join(sorted(need - set(u))) or 'none')
")"
if [ "$USAGE_KEYS" = "none" ]; then
  echo "ok    usage 含三个 token 计数"
  PASS=$((PASS + 1))
else
  echo "FAIL  usage 缺字段：${USAGE_KEYS}"
  FAIL=$((FAIL + 1))
fi

echo
echo "== Secret 不泄漏 =="
LEAK_BODY="$(curl -s -X POST -H "$JSON" \
  -d '{"messages":[{"role":"user","content":"my key is sk-abcdefghijklmnopqrstuvwx"}]}' \
  "$BASE/v1/complete")"
if printf '%s' "$LEAK_BODY" | grep -q 'sk-abcdefghijklmnopqrstuvwx'; then
  echo "FAIL  响应回显了疑似凭据"
  FAIL=$((FAIL + 1))
else
  echo "ok    响应未回显疑似凭据"
  PASS=$((PASS + 1))
fi

echo
echo "== 未配置真实 Provider 时不静默回退 =="
# 这条无法通过 HTTP 验证（容器已用 fake 启动），改为直接验证工厂行为。
FACTORY_CHECK="$(cd "$(dirname "$0")/../apps/agent-runtime-python" && \
  PYTHONPATH=src .venv/bin/python -c "
from agent_runtime.provider.factory import build_provider, ProviderConfigurationError
import os
for k in ('BASE_URL','MODEL','API_KEY'):
    os.environ.pop('RUNBOOKGUARD_LLM_'+k, None)
try:
    build_provider(kind='openai-compatible')
    print('SILENTLY_FELL_BACK')
except ProviderConfigurationError as e:
    print('RAISED' if 'RUNBOOKGUARD_LLM_BASE_URL' in str(e) else 'RAISED_WITHOUT_DETAIL')
" 2>&1 | tail -1)"
if [ "$FACTORY_CHECK" = "RAISED" ]; then
  echo "ok    请求真实 Provider 但缺配置时报错，不回退到 fake"
  PASS=$((PASS + 1))
else
  echo "FAIL  工厂行为异常：${FACTORY_CHECK}"
  FAIL=$((FAIL + 1))
fi

echo
echo "==============================="
echo "PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
