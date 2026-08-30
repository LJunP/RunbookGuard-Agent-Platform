#!/bin/sh
# 把 API 地址注入 index.html。
#
# 为什么运行时注入而不是构建期：同一份镜像要能指向不同的控制面。
# 构建期烧进去会让「换环境」等于「重新构建」，而 M7 Gate 要求
# 干净机器照 README 就能起来。
set -eu

API_BASE="${RUNBOOKGUARD_API_BASE:-http://127.0.0.1:8080}"
TARGET=/usr/share/nginx/html/index.html

if [ ! -f "$TARGET" ]; then
  echo "index.html not found at $TARGET" >&2
  exit 1
fi

# 用 | 作分隔符，因为 URL 里有 /。
sed -i "s|window.__RUNBOOKGUARD_API_BASE__ = \".*\"|window.__RUNBOOKGUARD_API_BASE__ = \"${API_BASE}\"|" "$TARGET"
echo "console API base set to ${API_BASE}"
