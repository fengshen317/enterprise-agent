#!/bin/bash
set -euo pipefail

umask 077
service_password=$(cat /run/ea-ragflow/password)
export MYSQL_PASSWORD="$service_password" ELASTIC_PASSWORD="$service_password" MINIO_PASSWORD="$service_password"
export REDIS_PASSWORD="$service_password" CLICKHOUSE_PASSWORD="$service_password"
unset service_password

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
