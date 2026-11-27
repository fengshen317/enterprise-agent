FROM golang:1.25-bookworm@sha256:3b4a11519ad929d1e1d261a12cff056f0c85b735253d7d861346b9c6f8b36437 AS fundpeek
# 固定上游提交，升级时同时验证持仓协议和本地凭据目录格式。
RUN CGO_ENABLED=0 go install github.com/icpd/fundpeek/cmd/fundpeek@55ad57ec1346fe676ee6520afca042b0c373f8e9

FROM node:22-bookworm-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb AS meeting
ARG TARGETARCH
RUN npm install --prefix /opt/tmeet --ignore-scripts @tencentcloud/tmeet@1.1.0 \
    && case "$TARGETARCH" in arm64) binary=tmeet-Linux-ARM64 ;; *) binary=tmeet-Linux-x86_64 ;; esac \
    && cp "/opt/tmeet/node_modules/@tencentcloud/tmeet/dist/$binary" /usr/local/bin/tmeet \
    && chmod 755 /usr/local/bin/tmeet

FROM python:3.13-slim-bookworm AS news
ARG TARGETARCH
# 使用官方固定版本并校验发布哈希，成员无需自行安装后端 CLI。
RUN python - <<'PY'
import hashlib
import os
from pathlib import Path
from urllib.request import urlopen

architecture = os.environ.get("TARGETARCH") or "amd64"
expected = {
    "amd64": "3b2a1cc815e3f55483eeff98518f4bbc16e790ac7ed2271accaeeda7dc8091e9",
    "arm64": "744e65fcc00c93eab2ba6733bbcef6f90b852c46ea5f700ce4e21830790b4977",
}[architecture]
url = f"https://mat1.gtimg.com/qqcdn/qqnews/cli/hub/v1.0.23/linux-{architecture}/tencent-news-cli"
with urlopen(url, timeout=60) as response:
    binary = response.read()
if hashlib.sha256(binary).hexdigest() != expected:
    raise SystemExit("腾讯新闻 CLI 发布校验失败")
path = Path("/usr/local/bin/tencent-news-cli")
path.write_bytes(binary)
path.chmod(0o755)
PY

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
    EA_CHANNEL_TOKEN_KEY_FILE=/home/enterprise-agent/.enterprise-agent/wechat-token.key \
    EA_YANGJIBAO_CLI_PATH=/usr/local/bin/fundpeek
RUN groupadd --gid 10001 enterprise-agent \
    && useradd --uid 10001 --gid enterprise-agent --create-home enterprise-agent
WORKDIR /app
COPY --from=dependencies /app/.venv ./.venv
COPY --from=fundpeek /go/bin/fundpeek /usr/local/bin/fundpeek
COPY --from=meeting /usr/local/bin/tmeet /usr/local/bin/tmeet
COPY --from=meeting /opt/tmeet/node_modules/@tencentcloud/tmeet/LICENSE /usr/share/doc/tencentmeeting-cli/LICENSE
COPY --from=news /usr/local/bin/tencent-news-cli /usr/local/bin/tencent-news-cli
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
USER enterprise-agent

# 迁移失败时拒绝启动 API。同一进程承载调度和渠道轮询，保持单 Worker。
# API 端口仅供 Compose 内部访问，Caddy 会覆盖外部的转发地址头。
CMD ["sh", "-ec", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --forwarded-allow-ips='*' --timeout-graceful-shutdown 20"]
