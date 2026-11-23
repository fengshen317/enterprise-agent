# Docker Compose 服务器部署

在 Linux 服务器上运行 Caddy、FastAPI、PostgreSQL 16 和 RAGFlow。RAGFlow 使用独立的 CPU 镜像，Compose 同时管理 MySQL、Elasticsearch、S3 对象存储、Kvrocks、NATS 和 ClickHouse。Caddy 对外提供 Vue 页面和同源 `/api`。FastAPI 等 PostgreSQL 通过健康检查后执行 `alembic upgrade head`，RAGFlow 等自己的依赖就绪后启动。

## 首次部署

1. 准备 Docker Engine 28 或以上版本和 Docker Compose 2.26.1 或以上版本。资源从 4 核 CPU、16 GB 内存、50 GB 可用磁盘空间起步，按文档解析量和业务负载扩容。按 RAGFlow 部署要求检查 `sysctl vm.max_map_count`，低于 262144 时在服务器上将其调整到至少 262144 并持久化。将域名解析到服务器并开放公网 TCP 80 和 443。 [RAGFlow v1.0.0 部署要求](https://github.com/infiniflow/ragflow/blob/v1.0.0/docker/README.md) · [Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)
2. 在仓库根目录复制环境模板。

   ```bash
   cp -n .env.example .env
   chmod 600 .env
   openssl rand -hex 32
   ```

   分别生成两个随机值，填入 `EA_DATABASE_PASSWORD` 和 `EA_RAGFLOW_SERVICE_PASSWORD`。将实际域名填入 `EA_DEPLOY_DOMAIN`，例如 `agent.example.com`。域名只填主机名。

3. 构建、启动并查看服务状态。

   ```bash
   docker compose config --quiet
   docker compose up -d --build --wait --wait-timeout 420
   docker compose ps
   ```

4. 新数据库首次登录前，在自己的终端交互创建管理员并设置密码。令根目录 `.env` 的 `EA_SYSTEM_ADMIN_USERNAME` 与该账号一致。迁入已有账号的数据库可指定已有账号。

   ```bash
   docker compose exec api python -m app.cli.accounts init --username admin
   ```

5. 打开 `https://你的域名/login` 登录。访问 `/api/health/live` 和 `/api/health/ready` 应分别返回 `ok` 和 `ready`。在侧栏「API 配置」设置语言模型，再到问答页面验证流式回答。

## 在工作台配置 API

- 系统管理员可在「API 配置」选择 DeepSeek 或 OpenAI 兼容接口，设置模型 ID、接口地址、API Key、超时和输出上限。兼容供应商可指定图片模型。问答、子 Agent、摘要和定时问答使用部署级配置。
- 同页可配置 Tavily 联网搜索及进入知识库 Embedding 设置。个人连接器凭据在「能力中心」由各成员绑定。
- 页面保存的值优先于同名环境变量，后续调用直接生效。密钥加密写入 PostgreSQL，页面只显示配置状态。留空会保留已有密钥，选择清除并保存会覆盖环境变量的旧值。旧部署保留已配置的环境变量，直到在页面保存替代值。兼容 LLM 地址变更时需重新提交或清除该接口的密钥。

## 初始化 RAGFlow 知识库

FastAPI 默认使用 Compose 内网地址 `http://ragflow:80`。内置部署的 `ragflow-init` 等 RAGFlow 就绪后创建普通服务账号、签发并验证 API Key。再次部署时复用有效 Key。初始化成功后 FastAPI 从只读卷加载 Key，RAGFlow 自助注册默认关闭。镜像锁定为 `infiniflow/ragflow:v1.0.0`，`EA_RAGFLOW_IMAGE` 可指定可信镜像仓库的同版本镜像。

1. 使用 `EA_SYSTEM_ADMIN_USERNAME` 指定的工作台账号登录，从「API 配置」进入「管理知识库模型」。
2. 选择硅基流动 BGE-M3、OpenAI Embedding 或自定义 OpenAI 兼容服务，并填写供应商 API Key。自定义服务还需填写 Base URL、模型 ID、向量维度、批量上限和最大 Token 数。
3. 点击「验证并启用」。后端用 RAGFlow 发起 Embedding 调用并核对维度，成功后设置新知识库的默认模型。已创建的知识库继续使用原模型及其供应商凭据。若已有部署设置了 `EA_RAGFLOW_EMBEDDING_MODEL`，先移除该覆盖值并应用容器配置，再在工作台切换默认模型。
4. 在工作台新建知识库，上传小文档，等待解析完成，再执行检索测试。

已有独立 RAGFlow 的部署可填写原实例的 `EA_RAGFLOW_BASE_URL` 和 `EA_RAGFLOW_API_KEY`，保留原服务账号和数据集。沿用已有内置实例时也可直接填原 Key。若希望初始化任务接管已有内置实例，使用 `EA_RAGFLOW_ADMIN_PASSWORD` 提供原 RAGFlow 超级管理员密码。

### RAGFlow 运维入口

RAGFlow 界面绑定服务器的 `127.0.0.1:9388`。在自己的电脑建立 SSH 隧道，再访问 `http://127.0.0.1:9388/admin`。若改过 `EA_RAGFLOW_UI_PORT`，同步修改隧道目标端口。

```bash
ssh -N -L 9388:127.0.0.1:9388 用户名@服务器地址
```

首次使用 `admin@ragflow.io` 登录。在可信的服务器终端读取初始化密码。

```bash
docker compose exec ragflow cat /var/lib/ea-ragflow-admin/admin-password
```

在 RAGFlow 后台的 Users 页面创建日常运维账号，并按需设为 Superuser。排查知识库数据时，从同一隧道访问首页，并给运维账号授予对应 RAGFlow 租户权限。若以后修改了初始化超级管理员密码，在根目录 `.env` 同步更新 `EA_RAGFLOW_ADMIN_PASSWORD`，以便需要时恢复服务账号的 API Key。也可通过容器内网使用官方管理 CLI。[RAGFlow Admin UI](https://ragflow.io/docs/v1.0.0/administrator/management/admin_ui/before_you_begin) · [RAGFlow 管理 CLI](https://ragflow.io/docs/v1.0.0/admin_cli)

## 可选集成

- 微信 iLink 和个人连接器的部署加密密钥沿用 `backend/.env.example` 中的后端配置项，填写在根目录 `.env`。首次启用时分别生成 Fernet 密钥并持久保存。迁移数据库时须沿用原有的 `EA_CHANNEL_TOKEN_KEY` 和 `EA_CONNECTOR_CREDENTIAL_KEY`，供已有凭证解密。

  ```bash
  docker compose exec api python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
  ```

- 腾讯会议、养基宝和腾讯新闻等本机 CLI 连接器需要为 API 容器提供相应 Linux 可执行文件及依赖。可用衍生镜像或只读挂载安装，再按各连接器说明配置。API 运行账号的主目录保存在 `api_home` 卷中，供使用该目录保存授权状态的 CLI 持久化。
- 修改根目录 `.env` 后执行 `docker compose up -d --wait` 让 Compose 应用新配置。定时任务、渠道轮询和问答执行与 API 同进程运行，部署时保持一个 API 容器、一个 Uvicorn Worker。

## 迁入已有数据库

从现有 PostgreSQL 用 `pg_dump -Fc` 导出数据库。在新服务器填写根目录 `.env` 后先启动 PostgreSQL，将备份恢复到空库，再启动应用执行后续迁移。若源库已有管理员，使用原账号登录。

```bash
docker compose up -d --wait db
docker compose exec -T db sh -ec 'pg_restore --exit-on-error --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < /path/to/enterprise-agent.dump
docker compose up -d --build --wait --wait-timeout 420
```

## 更新、备份与排障

更新前记录当前版本并先备份。从仓库根目录执行以下命令，将备份保存在当前账号的私有目录。

```bash
umask 077
mkdir -p ~/.enterprise-agent-backups
backup="$HOME/.enterprise-agent-backups/enterprise-agent-$(date +%Y%m%d-%H%M%S).dump"
docker compose exec -T db sh -ec 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup"
docker compose exec -T db pg_restore --list < "$backup"
```

将数据库备份和生成的部署密钥保存在受限位置。备份 `api_home`，它保存 CLI 授权及解密工作台 API 配置所需的 `.enterprise-agent/integration-config.key`。迁移数据库时同时还原这个密钥文件。若使用知识库，应同时保存 PostgreSQL、RAGFlow 的持久化数据卷和 `ragflow_admin_state`、`ragflow_api_credentials`。只恢复工作台数据库时，其中的知识库仍会引用原 RAGFlow dataset。数据库卷初始化后，轮换密码时须同步修改服务账号和根目录 `.env`。

更新到目标应用版本后构建镜像，安排短暂停机以确保迁移期间没有旧 API 写入。

```bash
docker compose build
docker compose stop api
docker compose up -d --wait --wait-timeout 420
docker compose logs --tail=100 api web db
```

回滚前检查旧应用与当前数据库结构的兼容性。若不兼容，先在独立环境将备份恢复到空数据库并验证，再切回对应应用版本。应用容器可以重建，日常停止使用 `docker compose stop`，保留 PostgreSQL、Caddy 和 API 的命名卷。

| 现象 | 检查 |
| --- | --- |
| 域名访问失败或证书未签发 | 检查 DNS、80/443 防火墙、端口占用和 `docker compose logs web` |
| 镜像构建时 Python 依赖下载缓慢 | 在 `.env` 设置 `EA_PYPI_INDEX_URL` 为可信镜像的 simple 索引后重新构建，依赖仍按 `uv.lock` 校验版本和哈希 |
| 后端未就绪 | 检查 `docker compose ps`、`docker compose logs api db` 和迁移报错 |
| RAGFlow 未就绪 | 检查 `docker compose ps`、`docker compose logs ragflow ragflow-search ragflow-db`、内存和 `vm.max_map_count` |
| 服务账号初始化失败 | 检查 `docker compose logs ragflow-init ragflow`。已有内置实例可提供原 API Key，或核对原超级管理员密码 |
| 工作台知识库无法创建或检索 | 先在「知识库模型」查看默认 Embedding 和验证结果，再检查 RAGFlow 日志 |
| 登录报请求来源不受信任 | 检查浏览器实际地址是否为 `https://` 加 `EA_DEPLOY_DOMAIN` |
| 页面可打开但真实问答失败 | 检查「API 配置」中的供应商、模型 ID 和密钥，再检查容器出站网络和 API 日志 |

## 验证记录

2026-11-19 在 Linux amd64 容器环境完成前后端镜像构建和 173 项前端测试。用独立 PostgreSQL 16 卷从空库迁移至 `0024`，验证了本地 HTTPS、前端深链接、健康接口、管理员登录和 SSE 事件转发。数据库备份已恢复到空库，保留命名卷重建容器后仍可登录并读取对话。SSE 使用受控运行验证，公网证书签发和真实 DeepSeek 回答待目标服务器验收。

同日在独立 Compose 项目使用镜像仓库的 RAGFlow `v1.0.0` 完成全栈启动。验证了账号注册、关闭注册、生成 API Key、工作台 HTTPS 建库、内网上传和下载原件。重建 RAGFlow 容器后仍可读取原件。带真实 Embedding 模型的解析和检索待部署环境验收。

2026-11-23 在独立 Compose 项目从空卷验证内置 RAGFlow `v1.0.0` 自动初始化、重复运行复用 Key、显式 Key 跳过初始化、HTTPS 系统管理员配置模型，以及空间管理员访问限制。通过本地兼容 Embedding 测试服务验证模型配置失败时保留原默认值，切换后新库使用新模型，旧库沿用原模型并能解析和检索普通及受限 TXT。原生 `/admin` 可通过回环端口访问，超级管理员认证和用户列表请求通过。生产供应商的连接及向量质量由部署环境验收。

同日通过本地 HTTPS 验证工作台 API 配置，覆盖权限、CSRF、密钥加密保存、保存后问答，以及 API 重启后用同一套配置继续问答。语言模型使用 Compose 网络内的兼容协议测试服务。

本地开发流程见 [本地开发指南](../docs/development/本地开发指南.md)。
