#!/usr/bin/env bash
set -Eeuo pipefail
export MSYS2_ARG_CONV_EXCL='*'
export MSYS_NO_PATHCONV=1

state="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "${EA_DEPLOY_ROOT:-/d/project/enterprise-agent}"
docker_engine() {
  "${EA_DOCKER_BIN:-docker.exe}" --context desktop-linux "$@"
}
compose() {
  docker_engine compose -p enterprise-agent -f compose.yaml -f compose.funnel.yaml "$@"
}
sql() {
  compose exec -T db sh -ec 'exec psql -XAtq -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}
native_path() {
  if command -v cygpath >/dev/null; then
    cygpath -am "$1"
  else
    printf '%s\n' "$1"
  fi
}
current_main() {
  git ls-remote https://github.com/fengshen317/enterprise-agent.git refs/heads/main | cut -f1
}
health() {
  local response page
  response="$(curl --fail --silent --show-error --retry 4 --retry-connrefused --retry-delay 2 --max-time 6 "http://$origin/api/health/ready")" || return
  grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' <<< "$response" || return
  page="$(curl --fail --silent --show-error --max-time 8 "http://$origin/login")" || return
  [[ "$page" == *'<div id="app">'* ]]
}
container_image() {
  local id
  id="$(compose ps -q "$1" | tr -d '\r')" || return
  [[ "$id" =~ ^[a-f0-9]+$ ]] || return 1
  docker_engine inspect --format "$2" "$id" | tr -d '\r'
}
write_override() {
  local api="$1" web="$2"
  printf 'services:\n  api:\n    build: !reset null\n    image: "%s"\n  ragflow-init:\n    build: !reset null\n    image: "%s"\n  web:\n    build: !reset null\n    image: "%s"\n' "$api" "$api" "$web"
}
backup() {
  local output="$1"
  compose exec -T db sh -ec 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$output"
  [[ -s "$output" ]]
  compose exec -T db pg_restore --list < "$output" > "$output.contents"
  compose exec -T db pg_restore --file=/dev/null < "$output"
  sha256sum "$output" > "$output.sha256"
}
snapshot() {
  sql <<SQL
BEGIN READ ONLY;
SET LOCAL statement_timeout = '15s';
SELECT 'users=' || count(*) FROM users WHERE created_at < to_timestamp($cutoff);
SELECT 'workspaces=' || count(*) FROM workspaces WHERE created_at < to_timestamp($cutoff);
SELECT 'conversations=' || count(*) FROM conversations WHERE created_at < to_timestamp($cutoff);
SELECT 'messages=' || count(*) FROM messages WHERE created_at < to_timestamp($cutoff);
SELECT 'message_sample=' || COALESCE(md5(string_agg(md5(id::text || content), ',' ORDER BY id)), 'empty')
FROM (SELECT id, content FROM messages WHERE created_at < to_timestamp($cutoff) ORDER BY id LIMIT 30) AS sample;
COMMIT;
SQL
}

IFS= read -r protocol
if [[ "$protocol" == EA-VERIFY-1 ]]; then
  origin="$(compose port web 8080 | tr -d '\r')"
  [[ "$origin" =~ ^127\.0\.0\.1:[0-9]+$ ]]
  health
  printf 'PASS: production receiver, Docker and HTTP readiness.\n'
  exit 0
fi
[[ "$protocol" == EA-DEPLOY-1 ]] || exit 64
IFS= read -r commit
IFS= read -r base
IFS= read -r api
IFS= read -r web
[[ "$commit" =~ ^[a-f0-9]{40}$ && "$base" =~ ^[a-f0-9]{64}$ ]] || exit 64
registry='ghcr.io/fengshen317/enterprise-agent-release-test'
[[ "$api" =~ ^"$registry"-api@sha256:[a-f0-9]{64}$ && "$web" =~ ^"$registry"-web@sha256:[a-f0-9]{64}$ ]] || exit 64

head="$(current_main)"
[[ "$head" =~ ^[a-f0-9]{40}$ ]]
if [[ "$head" != "$commit" ]]; then
  printf 'SKIP: main has advanced, deploy its latest commit instead.\n'
  exit 0
fi
if [[ "$(bash "$state/base-fingerprint.sh")" != "$base" ]]; then
  printf 'STOP: installed Compose or infrastructure files differ from this release. Review the deployment configuration first.\n'
  exit 65
fi
if [[ -e "$state/recovery-required" ]]; then
  printf 'STOP: the previous operation requires recovery on the deployment host.\n'
  exit 65
fi
if ! mkdir "$state/deploy.lock"; then
  printf 'STOP: another update owns the host deployment lock.\n'
  exit 75
fi

phase=prepare
maintenance=0
work=
exec 3>&1
finish() {
  local result=$? revision
  trap - EXIT
  set +e
  if (( result != 0 )); then
    printf 'FAIL: %s. Evidence remains in the deployment host operations directory.\n' "$phase" >&3
  fi
  if (( result != 0 && maintenance )); then
    compose -f "$next_file" stop web api
    revision="$(printf 'SELECT version_num FROM alembic_version;\n' | sql | tr -d '\r')"
    if [[ "$revision" == "$old_revision" ]] \
      && compose -f "$rollback_file" up -d --no-deps --no-build --pull never --wait --wait-timeout 150 api web \
      && health; then
      printf 'ROLLBACK OK: previous API and Web are serving on the original schema.\n' >&3
    else
      printf '%s\n' "$work" > "$state/recovery-required"
      printf 'RECOVERY REQUIRED: backups and old images are preserved for recovery.\n' >&3
    fi
  fi
  if [[ -n "$work" ]]; then
    printf '%s\n' "$result" > "$work/exit-status"
  fi
  rmdir "$state/deploy.lock"
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT TERM HUP
umask 077
mkdir -p "$state/operations"
work="$(mktemp -d "$state/operations/$commit.XXXXXX")"
printf '%s\n' "$work" > "$state/latest-operation"
exec >"$work/operation.log" 2>&1
printf 'Deployment evidence: %s\n' "$(basename -- "$work")" >&3
origin="$(compose port web 8080 | tr -d '\r')"
[[ "$origin" =~ ^127\.0\.0\.1:[0-9]+$ ]]
health
old_api="$(container_image api '{{.Image}}')"
old_web="$(container_image web '{{.Image}}')"
if [[ "$(container_image api '{{.Config.Image}}')" == "$api" && "$(container_image web '{{.Config.Image}}')" == "$web" ]]; then
  printf 'PASS: the requested images are already running and healthy.\n' >&3
  exit 0
fi

phase=anonymous_registry_setup
desktop_endpoint="$(docker_engine context inspect desktop-linux --format '{{.Endpoints.docker.Host}}' | tr -d '\r')"
[[ "$desktop_endpoint" == 'npipe:////./pipe/dockerDesktopLinuxEngine' ]]
mkdir "$work/anonymous-docker"
# 空的 GHCR 条目会保留匿名拉取，并阻止 Windows CLI 自动选择凭据助手。
printf '%s\n' '{"auths":{"ghcr.io":{}}}' > "$work/anonymous-docker/config.json"
anonymous_config="$(native_path "$work/anonymous-docker")"

for image in "$api" "$web"; do
  phase=pull
  env -u DOCKER_CONTEXT -u DOCKER_HOST -u DOCKER_AUTH_CONFIG "${EA_DOCKER_BIN:-docker.exe}" \
    --config "$anonymous_config" --host "$desktop_endpoint" image pull --platform linux/amd64 "$image"
  phase=check_image_revision
  [[ "$(docker_engine image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image" | tr -d '\r')" == "$commit" ]]
done
phase=prepare_migration
old_revision="$(printf 'SELECT version_num FROM alembic_version;\n' | sql | tr -d '\r')"
[[ "$old_revision" =~ ^[a-zA-Z0-9_]+$ ]]
new_revision="$(docker_engine run --rm --pull never --network none --entrypoint alembic "$api" heads | tr -d '\r' | awk 'NF { print $1 }')"
[[ "$new_revision" =~ ^[a-zA-Z0-9_]+$ ]]
docker_engine run --rm --pull never --network none --entrypoint alembic "$api" history -r "$old_revision:head"
suffix="$(basename -- "$work" | tr '[:upper:]' '[:lower:]')"
docker_engine image tag "$old_api" "ea-prod-rollback-api:$suffix"
docker_engine image tag "$old_web" "ea-prod-rollback-web:$suffix"
write_override "$api" "$web" > "$work/compose.release-selected.yaml"
write_override "ea-prod-rollback-api:$suffix" "ea-prod-rollback-web:$suffix" > "$work/compose.release-rollback.yaml"
next_file="$(native_path "$work/compose.release-selected.yaml")"
rollback_file="$(native_path "$work/compose.release-rollback.yaml")"
compose -f "$next_file" config --quiet
compose -f "$rollback_file" config --quiet

phase=backup
backup "$work/postgres-before-switch.dump"
printf 'PASS: online PostgreSQL backup is fully readable.\n' >&3
phase=wait_for_work
for attempt in {1..12}; do
  active="$(printf "%s\n" "SELECT (SELECT count(*) FROM runs WHERE status IN ('pending','running')) + (SELECT count(*) FROM task_runs WHERE status IN ('pending','running'));" | sql | tr -d '\r')"
  [[ "$active" =~ ^[0-9]+$ ]]
  if [[ "$active" == 0 ]]; then
    break
  fi
  sleep 5
done
[[ "$active" == 0 ]]
head="$(current_main)"
[[ "$head" =~ ^[a-f0-9]{40}$ ]]
if [[ "$head" != "$commit" ]]; then
  printf 'SKIP: main changed while preparing. Current services remain available.\n' >&3
  exit 0
fi

phase=stop_ingress_and_api
maintenance=1
compose stop -t 30 web api
phase=maintenance_backup
cutoff="$(printf 'SELECT extract(epoch from clock_timestamp())::numeric(20,6);\n' | sql | tr -d '\r')"
[[ "$cutoff" =~ ^[0-9]+\.[0-9]{6}$ ]]
snapshot > "$work/data-before.txt"
backup "$work/postgres-at-cutover.dump"
printf 'PASS: maintenance-window PostgreSQL backup is fully readable.\n' >&3
phase=start_api
compose -f "$next_file" up -d --no-deps --no-build --pull never --wait --wait-timeout 150 api
[[ "$(printf 'SELECT version_num FROM alembic_version;\n' | sql | tr -d '\r')" == "$new_revision" ]]
phase=check_data
snapshot > "$work/data-after.txt"
cmp "$work/data-before.txt" "$work/data-after.txt"
phase=start_web
compose -f "$next_file" up -d --no-deps --no-build --pull never --wait --wait-timeout 150 web
health
[[ "$(container_image api '{{.Config.Image}}')" == "$api" ]]
[[ "$(container_image web '{{.Config.Image}}')" == "$web" ]]
printf '%s\n' "$next_file" > "$state/current-compose-override"
maintenance=0
printf 'PASS: production API, Web and existing data verified at %s.\n' "$commit" >&3
