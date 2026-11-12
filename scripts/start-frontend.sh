#!/usr/bin/env bash
set -euo pipefail

# 根据脚本位置定位项目，允许从任意目录启动。
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir/frontend"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "请先安装 Node.js 22.12+ 和 npm，并加入 PATH。" >&2
  exit 1
fi

# 首次启动按锁文件安装依赖，避免每次启动重复安装。
if [[ ! -d node_modules ]]; then
  npm ci
fi

echo "启动前端：http://127.0.0.1:8686（Ctrl+C 停止）"
exec npm run dev
