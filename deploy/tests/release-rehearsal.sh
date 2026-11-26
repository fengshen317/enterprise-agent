#!/usr/bin/env bash
set -euo pipefail

release_dir="${1:?请提供解包后的发布目录}"
: "${RUNNER_TEMP:?请在 GitHub 托管 Runner 上运行}"
: "${GITHUB_SHA:?缺少当前提交}"
: "${GITHUB_RUN_ID:?缺少工作流运行编号}"
: "${GITHUB_RUN_ATTEMPT:?缺少工作流运行次数}"

baseline_commit=0da2207856c0a155b865c8366f740697d807f283
baseline_revision=0026
baseline_image=enterprise-agent-release-test-api:baseline
project="ea-release-test-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
rehearsal_dir="$(mktemp -d "$RUNNER_TEMP/release-rehearsal.XXXXXX")"
test_override="$(dirname -- "$0")/compose.release-test.yaml"
backup_file="$rehearsal_dir/pre-upgrade.dump"
upgraded_db=enterprise_agent_rehearsal
restored_db=enterprise_agent_restored
fixture_message=release-rehearsal-preserve-this-message
fixture_message_id=20000000-0000-0000-0000-000000000002

export EA_DEPLOY_DOMAIN=release-test.invalid
export EA_DEPLOY_ENV_FILE="$rehearsal_dir/application.env"
export EA_DATABASE_NAME="$upgraded_db"
export EA_DATABASE_USER=enterprise_agent
export EA_FUNNEL_ORIGIN_PORT=18080
: > "$EA_DEPLOY_ENV_FILE"

compose() {
  docker compose --project-name "$project" --env-file "$release_dir/release.env" \
    -f "$release_dir/compose.yaml" -f "$release_dir/compose.funnel.yaml" \
    -f "$release_dir/compose.release.yaml" -f "$test_override" "$@"
}

sql() {
  local database="$1"
  shift
  compose exec -T db psql -X -At -v ON_ERROR_STOP=1 -U "$EA_DATABASE_USER" -d "$database" "$@"
}

check_web() {
  local url="http://127.0.0.1:$EA_FUNNEL_ORIGIN_PORT"
  local page
  curl --fail --silent --show-error --retry 8 --retry-connrefused --retry-delay 1 --max-time 5 \
    "$url/api/health/ready" | jq -e '.status == "ready"' >/dev/null
  page="$(curl --fail --silent --show-error --max-time 5 "$url/login")"
  [[ "$page" == *'<div id="app">'* ]]
}

check_message() {
  [[ "$(sql "$1" -c "SELECT content FROM messages WHERE id = '$fixture_message_id'")" == "$fixture_message" ]]
}

report_failure() {
  local status=$?
  if (( status != 0 )); then
    compose ps || true
    compose logs --tail=80 api web db || true
  fi
  exit "$status"
}
trap report_failure EXIT

[[ "$(cat "$release_dir/source-revision")" == "$GITHUB_SHA" ]]
config="$(compose config --format json)"
jq -e 'all((.services.api, .services.web, .services["ragflow-init"]);
  .build == null and (.image | test("@sha256:[0-9a-f]{64}$")))' <<< "$config" >/dev/null
published_api="$(jq -er '.services.api.image' <<< "$config")"
published_web="$(jq -er '.services.web.image' <<< "$config")"

compose pull api web
for image in "$published_api" "$published_web"; do
  [[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" == "$GITHUB_SHA" ]]
done
candidate_revision="$(docker run --rm "$published_api" alembic heads | awk 'NF {print $1}')"
[[ "$candidate_revision" =~ ^[a-zA-Z0-9_]+$ && "$candidate_revision" != "$baseline_revision" ]]

mkdir -p "$rehearsal_dir/baseline"
git archive "$baseline_commit" backend | tar -x -C "$rehearsal_dir/baseline"
docker build -t "$baseline_image" "$rehearsal_dir/baseline/backend"
EA_RELEASE_API_IMAGE="$baseline_image" compose up -d --no-build --wait --wait-timeout 180 api
[[ "$(sql "$upgraded_db" -c 'SELECT version_num FROM alembic_version')" == "$baseline_revision" ]]

# 写入迁移前的真实表，验证升级和恢复均能保留已有对话。
sql "$upgraded_db" <<SQL
INSERT INTO conversations (id, title, next_message_sequence)
VALUES ('20000000-0000-0000-0000-000000000001', 'release-rehearsal', 2);
INSERT INTO messages (id, conversation_id, sequence, role, content, status)
VALUES ('$fixture_message_id', '20000000-0000-0000-0000-000000000001', 1, 'user', '$fixture_message', 'completed');
SQL
compose stop api
compose exec -T db pg_dump -U "$EA_DATABASE_USER" -d "$upgraded_db" -Fc > "$backup_file"
[[ -s "$backup_file" ]]
compose exec -T db pg_restore --list < "$backup_file" >/dev/null
echo "数据库 $baseline_revision 已停止应用写入并生成可读取的 PostgreSQL 备份。"

compose up -d --no-build --wait --wait-timeout 180 web
check_web
[[ "$(sql "$upgraded_db" -c 'SELECT version_num FROM alembic_version')" == "$candidate_revision" ]]
[[ "$(sql "$upgraded_db" -c "SELECT to_regclass('public.chat_reports') IS NOT NULL")" == t ]]
check_message "$upgraded_db"
echo "发布镜像启动和迁移 $baseline_revision → $candidate_revision 通过，原有对话完整。"

# 恢复到空数据库，再只切换旧 API，保留升级库供独立比对。
compose exec -T db createdb -U "$EA_DATABASE_USER" "$restored_db"
compose exec -T db pg_restore --exit-on-error --single-transaction --no-owner --no-privileges \
  -U "$EA_DATABASE_USER" -d "$restored_db" < "$backup_file"
[[ "$(sql "$restored_db" -c 'SELECT version_num FROM alembic_version')" == "$baseline_revision" ]]
[[ "$(sql "$restored_db" -c "SELECT to_regclass('public.chat_reports') IS NULL")" == t ]]
check_message "$restored_db"
EA_DATABASE_NAME="$restored_db" EA_RELEASE_API_IMAGE="$baseline_image" \
  compose up -d --no-deps --no-build --wait --wait-timeout 180 api
check_web
[[ "$(sql "$upgraded_db" -c 'SELECT version_num FROM alembic_version')" == "$candidate_revision" ]]
echo "备份恢复到独立数据库通过，旧 API 就绪，升级数据库仍保留。"
