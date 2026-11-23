#!/bin/bash
set -euo pipefail

umask 077
state=/var/lib/ea-ragflow-admin
password_file="$state/admin-password"

# RAGFlow 仅在首次启动时采纳初始密码，后续应沿用同一部署的密钥。
if [[ -n "${EA_RAGFLOW_ADMIN_PASSWORD:-}" ]]; then
  printf '%s\n' "$EA_RAGFLOW_ADMIN_PASSWORD" > "$password_file"
elif [[ ! -s "$password_file" ]]; then
  openssl rand -hex 32 > "$password_file"
fi
cp /ragflow/conf/public.pem "$state/public.pem"
export ADMIN_DEFAULT_PASSWORD
ADMIN_DEFAULT_PASSWORD=$(cat "$password_file")
cd /ragflow
exec /ragflow/entrypoint.sh --enable-adminserver --init-superuser "$@"
