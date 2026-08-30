#!/usr/bin/env bash
# M0 交付物结构校验。非产品代码：只检查 docs/ 下 M0 文档是否具备说明书要求的强制结构，
# 不校验内容质量。M0 阶段没有产品代码，这是 Gate 唯一可机器复核的证据来源。
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

red()   { printf '\033[31m%s\033[0m\n' "$1"; }
green() { printf '\033[32m%s\033[0m\n' "$1"; }

# require <file> <grep-pattern> <label>
require() {
  local file="$1" pattern="$2" label="$3"
  if [ ! -f "$ROOT/$file" ]; then
    red "FAIL  $label  (缺少文件 $file)"; FAIL=$((FAIL + 1)); return
  fi
  if grep -qE "$pattern" "$ROOT/$file"; then
    green "ok    $label"; PASS=$((PASS + 1))
  else
    red "FAIL  $label  ($file 缺少 /$pattern/)"; FAIL=$((FAIL + 1))
  fi
}

# require_count <file> <pattern> <min> <label>
require_count() {
  local file="$1" pattern="$2" min="$3" label="$4"
  if [ ! -f "$ROOT/$file" ]; then
    red "FAIL  $label  (缺少文件 $file)"; FAIL=$((FAIL + 1)); return
  fi
  local n
  n="$(grep -cE "$pattern" "$ROOT/$file" || true)"
  if [ "$n" -ge "$min" ]; then
    green "ok    $label  ($n >= $min)"; PASS=$((PASS + 1))
  else
    red "FAIL  $label  (命中 $n，要求 >= $min)"; FAIL=$((FAIL + 1))
  fi
}

echo "== M0-1 Product Brief =="
BRIEF="docs/architecture/M0-product-brief.md"
require "$BRIEF" '一句话定义'        "Brief: 一句话定义"
require "$BRIEF" '要解决的问题'      "Brief: 问题陈述"
require "$BRIEF" '差异化'            "Brief: 差异化（对标 HolmesGPT/kagent）"
require "$BRIEF" '成功标准'          "Brief: 成功标准"
require "$BRIEF" '三分钟'            "Brief: 三分钟答辩（M0 Gate 条件）"
require "$BRIEF" 'UNKNOWN|未验证'    "Brief: 事实与推测分离标注"

echo
echo "== M0-2 用户与场景 =="
USERS="docs/architecture/M0-users-and-scenarios.md"
require_count "$USERS" '^### P[0-9]+ ' 4 "用户画像 >= 4 个（P1..Pn）"
require_count "$USERS" '^### UJ[0-9]+ ' 3 "用户旅程 >= 3 条（UJn）"
require "$USERS" '非目标用户'         "用户: 明确非目标用户"

echo
echo "== M0-3 不做清单 =="
NG="docs/architecture/M0-non-goals.md"
require_count "$NG" '^### NG-[0-9]+ ' 17 "不做项 >= 17 条（说明书 9 条产品级 + 提示词 6 条过程级 + 技术栈排除）"
require_count "$NG" '^\*\*理由' 17    "每条不做项都有独立理由段"
require "$NG" '重新评估条件|解禁条件'  "不做清单: 有解禁/重新评估条件"

echo
echo "== M0-4 威胁模型 =="
TM="docs/architecture/M0-threat-model.md"
require_count "$TM" '^### T-[0-9]+ ' 6 "威胁条目 >= 6 条"
for t in \
  '注入日志诱导越权' \
  '审批后篡改参数' \
  'Secret 泄漏进 Trace' \
  '工具越 tenant 访问' \
  '无界循环烧预算' \
  '崩溃后重复副作用'
do
  require "$TM" "$t" "威胁必含: $t"
done
require_count "$TM" '里程碑落点|落点里程碑' 6 "每个威胁绑定里程碑落点"
require "$TM" '信任分级|信任等级'     "威胁模型: 含信任分级表"

echo
echo "== M0-5 初始故障场景 =="
SC="docs/architecture/M0-initial-incident-scenarios.md"
require_count "$SC" '^## S[0-9]+ ' 8  "初始故障场景 = 8 个"
require_count "$SC" '输入快照' 8       "每场景: 输入快照"
require_count "$SC" '允许工具集合' 8   "每场景: 允许工具集合"
require_count "$SC" '必需证据' 8       "每场景: 必需证据"
require_count "$SC" '可接受结论' 8     "每场景: 可接受结论"
require_count "$SC" '禁止动作' 8       "每场景: 禁止动作"
require_count "$SC" '期望终态' 8       "每场景: 期望终态"
require_count "$SC" 'grader' 8         "每场景: grader"

echo
echo "== M0-6 ADR: 上游冲突裁决 =="
ADR="docs/adr/ADR-0001-scope-freeze-and-spec-conflicts.md"
require "$ADR" 'Status'               "ADR: 有 Status 字段"
require_count "$ADR" '^### C-[0-9]+ ' 3 "ADR: 记录 >= 3 处说明书/提示词冲突"
require "$ADR" '以说明书为准' "ADR: 声明冲突裁决原则"

echo
echo "== M0-7 Gate 自检报告 =="
GATE="docs/architecture/M0-gate-report.md"
require "$GATE" '交付物清单'          "Gate: 交付物清单"
require "$GATE" '真实输出'            "Gate: 真实运行输出"
require "$GATE" '已知限制'            "Gate: 已知限制与未验证项"
require "$GATE" '前置依赖'            "Gate: 下一里程碑前置依赖"

echo
echo "==============================="
echo "PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
