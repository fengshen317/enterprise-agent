FROM python:3.13-slim-bookworm AS dependencies
ARG EA_PYPI_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --no-cache-dir --index-url "$EA_PYPI_INDEX_URL" uv==0.11.8
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
# 导出锁定版本及哈希，允许从镜像下载同一份依赖。
RUN uv export --locked --no-dev --no-emit-project --output-file /tmp/requirements.txt --quiet \
    && uv venv \
    && uv pip sync --default-index "$EA_PYPI_INDEX_URL" --require-hashes /tmp/requirements.txt

FROM python:3.13-slim-bookworm
# 微信密钥保存在 API 主目录的持久卷中，镜像升级可沿用已安装的 Compose。
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EA_CHANNEL_TOKEN_KEY_FILE=/home/enterprise-agent/.enterprise-agent/wechat-token.key
RUN groupadd --gid 10001 enterprise-agent \
    && useradd --uid 10001 --gid enterprise-agent --create-home enterprise-agent
WORKDIR /app
COPY --from=dependencies /app/.venv ./.venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
USER enterprise-agent

# 迁移失败时拒绝启动 API。同一进程承载调度和渠道轮询，保持单 Worker。
# API 端口仅供 Compose 内部访问，Caddy 会覆盖外部的转发地址头。
CMD ["sh", "-ec", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --forwarded-allow-ips='*' --timeout-graceful-shutdown 20"]
