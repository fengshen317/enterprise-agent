#!/usr/bin/env bash
set -euo pipefail

# 统一换行后比对当前安装的 Compose 和基础服务文件，避免在应用发布时改变基础服务。
# 显式使用二进制模式，让 Linux 与 Git for Windows 使用相同的摘要输入和标记。
for file in compose.yaml compose.funnel.yaml deploy/init-service-passwords.py deploy/ragflow/compose.yaml deploy/ragflow/entrypoint.sh; do
  printf '%s ' "$file"
  sed 's/\r$//' "$file" | sha256sum --binary
done | sha256sum --binary | cut -d ' ' -f1
