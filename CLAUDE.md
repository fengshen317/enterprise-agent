# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

「序列 · 企业 Agent 工作台」：基于 Deep Agents + DeepSeek 的企业问答与任务平台。当前为**本地单进程运行**，复用本机 PostgreSQL 16，不使用 Docker。正式前端和后端分别位于 `frontend/`、`backend/`；`prototype/` 是独立 Git 仓库的产品原型，已被根仓库忽略，不应作为正式能力或验收证据。

- 后端：FastAPI + SQLAlchemy 异步会话 + Alembic，Python 3.13 / uv
- 前端：Vue 3 + TypeScript + Vite + Pinia + Vue Router，Node 22.12+
- 本地定时问答：`croniter` + `tzdata`，Cron 固定对话追加运行
- 知识库：RAGFlow 1.0 独立部署的外部服务，应用侧 ACL + PostgreSQL 元数据；应用本身仍不依赖 Docker
- 根仓库分支当前为 `chore/project-bootstrap`；提交信息用中文 `<type>: <描述>`

## 常用命令

所有后端命令在 `backend/` 目录执行，前端命令在 `frontend/` 目录执行。

```bash
# 首次准备
cp backend/.env.example backend/.env        # 填写 EA_DATABASE_*、EA_TEST_DATABASE_NAME、EA_DEEPSEEK_API_KEY
cd backend && uv sync --frozen && uv run alembic upgrade head
cd frontend && npm ci

# 启动（二选一）
python scripts/start.py                     # 根目录，同时启动 8000 + 8686
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
cd frontend && npm run dev                  # Vite 固定 8686，/api 代理到 8000

# 后端检查
uv run ruff check .
uv run ruff format --check .
uv run mypy app/                            # strict 模式
uv run pytest                               # 含集成测试（未配置测试库时跳过）与 80% 覆盖率门禁
uv run pytest -m "not integration" --no-cov # 快速检查，不连数据库
# 跑单个测试必须加 --no-cov，否则仍会触发 80% 总体覆盖率门禁
uv run pytest tests/test_chat_runtime.py -v --no-cov
uv run pytest tests/test_chat_runtime.py::test_name -v --no-cov

# 数据库
uv run alembic upgrade head
uv run alembic current && uv run alembic check

# 前端检查
npm run lint
npm run typecheck
npm run test
npm run test -- src/views/ChatView.test.ts   # 单个前端测试
npm run build

# 全量检查（根目录）
node scripts/check.mjs              # 快速检查，不跑集成测试，不评估覆盖率
node scripts/check.mjs --database   # 完整验收：集成测试 + >=80% 覆盖率
```

首次登录前需初始化管理员（交互输入密码，参数中不传密码）：

```bash
cd backend && uv run python -m app.cli.accounts init --username admin --claim-legacy
```

真实模型验收脚本会调用付费 API，且只使用测试库：`uv run python -m scripts.verify_real_chat`、`verify_real_chat_agents`、`verify_real_tools`、`verify_real_tasks`。

## 架构

### 应用装配

`backend/app/main.py:create_app()` 支持注入 `Settings`、`StreamProvider`、`TaskProvider`、`RagflowClientProtocol`，测试据此替换模型、外部服务与配置。lifespan 内创建 `Database`、`AuthService`、`ChatRuntime`、`TaskRuntime`、`ScheduleRuntime`、`KnowledgeService`，退出时先停调度、收敛运行再释放连接池和 RAGFlow 客户端。所有响应带 `X-Request-ID`，错误统一为 `{"error": {"code", "message"}, "request_id"}`（`app/core/errors.py` 的 `ApplicationError`）。

### 请求与身份边界

- 入口：`app/api/` 下 `auth/chat/admin/tasks/health/schedules/knowledge`；路由统一 `/api` 前缀。
- 登录：HttpOnly Cookie（path `/api`），令牌摘要存库；写请求同时校验 `Origin` 和会话绑定的 `X-CSRF-Token`。
- 空间：普通请求用 `X-Workspace-ID` 头，SSE 可用 `workspace_id` 查询参数；`get_scope()` 每次重新校验成员资格。
- 私有资源按 `Conversation.workspace_id + created_by_user_id` 过滤，管理员也不能读取他人对话（`owned_conversations`）。
- 认证状态只在 PostgreSQL；不在 `localStorage` 存令牌（前端只存工作空间选择）。
- 账号/空间/角色模型在 `app/models/identity.py`，角色属于空间成员，全局只有 `admin`/`member`。

### 对话与运行生命周期

核心链路：`app/api/chat.py` → `ChatStore`（事务）→ `ChatRuntime`（生成管理）。

- `Conversation` 有 `next_message_sequence` 和持久化摘要字段；一次提问创建 `Run` + 两条 `Message`（用户输入、助手占位），序号成对递增。
- `Run` 状态 `pending/running/completed/failed/cancelled`；数据库部分唯一索引保证每个对话只有一个活跃运行；`idempotency_key` 防重复提交。
- `ChatRuntime` 通过 PostgreSQL advisory lock `782341912` 强制**单后端进程**，启动时把遗留的活跃运行收敛为失败（`PROCESS_RESTARTED`）。
- 生成在独立 `asyncio.Task` 中执行，浏览器断开不取消；正文按 256 字符 / 0.15 秒批量落库，另起任务每秒复核账号、成员、空间是否仍有效。
- SSE 每 0.25 秒查询完整快照并只在变化时推送，避免断线重连重复拼接文本；SSE 轮询中重新认证，撤权立即终止。

### Agent 与工具

- `app/agents/provider.py` 的 `DeepSeekProvider` 用 `create_deep_agent` 构建每次运行的独立图；SDK 模块在线程中懒加载，避免阻塞事件循环；DeepSeek 关闭 thinking 和 SDK 重试。
- 中间件将模型可用工具限制为显式白名单；未注册工具、未授权调用直接抛 `ApplicationError`。
- `app/services/chat_agent.py` 的 `ChatAgentExecutor` 在对话内提供 `update_plan` / `delegate_agent`：最多 2 个并行子 Agent、6 次委派、30 次模型调用，禁止子 Agent 递归委派；子 Agent 是独立图，只共享运行身份与配额。
- 模型请求以“单个模型动作”为边界重试（`model_call`），不重放已完成的工具；错误分类与退避在 `app/services/task_retry.py`。
- 工具执行入口 `app/services/tools.py`：身份来自服务端运行归属，模型不能传身份；每次调用写 `tool_calls` 审计，配额与超时由配置控制。业务工具包括 `get_my_run_stats`、`search_knowledge_base`、`list_knowledge_bases` 和写工具 `create_scheduled_task`。
- `app/capabilities/` 是工具声明与内置实现；`AgentAssembler` 按 Agent 配置装配工具、技能和知识库范围；`DeepSeekProvider` 不再硬编码工具名称。
- 上下文压缩在 `app/services/context_builder.py`：达到 `EA_MODEL_CONTEXT_CHARACTERS * 0.7` 时按完整轮次生成摘要，保留近期轮次；摘要带版本号乐观提交，原始消息从不删除。

### 定时问答

`app/services/schedule_runtime.py` 用独立 advisory lock `782341917` 轮询 `scheduled_tasks`（迁移 `0009`）。到期任务由 `ScheduleStore.claim_due` 在事务内 `SKIP LOCKED` 领取并**先推进 `next_run_at`**，再通过 `ChatRuntime.store.create_run` 向固定对话追加新 Run；幂等键为 `schedule_{id}_{触发时间}`。同一对话有活跃运行时本次触发记为 `skipped`，不排队补跑；编辑任务会从当前时间重算下一次触发。Cron 只接受五段，时区为 IANA 名称，换算结果统一存 UTC。

### 知识库

- `app/services/knowledge.py` 是权限与文档状态主库；`app/services/ragflow.py` 是 RAGFlow 1.0 唯一出口。一个应用知识库对应一个 RAGFlow dataset，`ragflow_dataset_id` 不返回前端。
- `workspace` 知识库由空间管理员创建和管理，所有有效成员可检索；`private` 知识库仅创建者可读写，管理员也不能读取。当前不做文档级 ACL。
- 上传流程固定为：上传文件 → `PATCH /datasets/{ds}/documents/{doc}` 写解析配置 → `POST /datasets/{ds}/chunks` 解析。RAGFlow 1.0 上传接口会忽略 `parser_config`。
- 解析状态为 `UNSTART/RUNNING/COMPLETED/FAIL/CANCEL`；列表请求时同步 RAGFlow 状态，前端解析中每 5 秒轮询，没有后台 Worker。
- 原文件下载走 `GET /datasets/{ds}/documents/{doc}`；分块列表走 `GET .../chunks`，参数为 `page/page_size`。前端通过后端代理 `/api/knowledge-documents/{id}/file` 预览/下载，`/chunks` 只读浏览。
- 解析配置支持预设模板、`parser_id`、分块参数、PDF 解析方式和自动关键词/问题；组件名按 `Parser:`/`Extractor:`/`GeneralChunker:` 前缀回退定位。`/reparse-documents` 先 apply 配置再批量解析。
- 详情页“检索测试”走独立服务方法，不写 `tool_calls`、不做 Agent 上下文预算裁剪，阈值由用户指定。
- `search_knowledge_base` 身份来自当前 Run；主 Agent 与子 Agent 均可使用，服务端把模型给的 `kb_ids` 与可访问知识库求交集，只把允许的 `dataset_ids` 传给 RAGFlow。检索审计复用 `tool_calls`，不保存完整正文。
- **自动知识库检索注入**：`chat_runtime.generate()` 在每次提问时自动用用户问题检索可访问知识库（按 Agent `knowledge_scope` 裁剪），把 chunk 编号 [1][2]… 改写进 `history[-1]` 注入模型上下文；审计写 `tool_calls(tool_name="auto_knowledge_retrieval")`。检索失败降级为无资料回答，不阻塞生成。前端展示来源卡片（`ChunkSourceCard`），正文 `[n]` 渲染为可点击角标。
- RAGFlow API Key 只在后端 `.env`；未配置密钥时不创建知识库服务、不注册工具。RAGFlow UI 不向终端用户开放。

### Agent 工作台与能力扩展

- `agent_profiles` + `agent_capabilities` 保存 Agent 指令、工具/技能绑定和知识库范围；`skills` + `skill_versions` 保存不可变 Markdown/SOP 版本；`workspace_tool_policies` 允许空间管理员停用内置工具。
- 每个对话通过 `conversations.agent_profile_id` 选择 Agent；`NULL` 表示内置“序列助手”。新建对话或切换 Agent 走 `/api/conversations/{id}/agent`，生成中拒绝切换。
- 运行创建时把 Agent 配置固化为 `runs.agent_snapshot`，执行时由 `AgentAssembler` 恢复；历史运行不会因 Agent 后续编辑而改变。
- 技能支持 `always`（注入 system prompt）和 `on_demand`（模型调用 `load_skill`）；技能声明的工具必须是 Agent 工具集的子集，知识库范围只能收窄。
- 工具风险分级：只读工具自动执行；写入/删除/外发必须逐次审批。`approvals` + `approval_events` 保存审批链，`tool_calls` 状态包含 `awaiting_approval/denied/expired`。
- 审批等待在单进程内用 `asyncio.Event`，只允许 Run owner 决策；重启时 pending 审批收敛为过期，不做断点恢复。
- 第一期内置工具编目；HTTP/OpenAPI、MCP、Python 插件和技能脚本执行不在本期范围。

### 旧独立任务后端

`TaskRuntime` + `TaskStore` + `models/task.py` 是保留兼容的持久化任务账本（计划确认、fencing token、动作检查点、重试与恢复），调度锁 `782341915`。前端 `/tasks` 已重定向到 `/chat`，新功能优先落在对话内规划/子 Agent，不要误把旧任务链路当成默认产品入口。

### 前端

- `src/main.ts` 定义路由：`/login`、`/chat/:conversationId?`、`/agents`、`/agents/:agentId`、`/capabilities`、`/capabilities/skills/:skillId`、`/knowledge`、`/knowledge/:kbId`、`/schedules`、`/system`、`/admin/members`；全局守卫调用 `auth.restore()`，管理员路由检查当前空间角色。
- `src/stores/auth.ts` 管理会话与工作空间；`src/api/client.ts` 统一附加 Cookie、CSRF、`X-Workspace-ID`，超时 5 秒，401 派发 `auth-expired` 事件。
- API 封装按领域放在 `src/api/`；样式使用 SCSS、变量在 `src/styles/variables.scss`、类名 BEM；Element Plus 尚未引入。
- 问答页 `ChatView.vue` 负责消息、SSE 订阅、取消/重试、工具记录与规划执行卡片；`SchedulesView.vue` 管理定时任务的创建、编辑、启停、立即执行与删除，并可新建固定对话。
- `MarkdownBody.vue` + `utils/markdown.ts` 用 marked + DOMPurify 渲染消息正文；禁止直接 `v-html` 输出未清洗的模型内容。
- `CronBuilder.vue` 是定时任务的可视化时间组件：每天/每周/每月/固定间隔生成五段 Cron，无法归类时回退到高级 Cron 原文。
- `KnowledgeView.vue` 已拆为 `views/knowledge/`：`KnowledgeListView`（列表+新建）、`KnowledgeDetailView`（Tab：文档管理/解析配置/检索测试）、`DocumentsTab`、`ParserConfigTab`、`RetrievalTestTab`、`ParserFields`、分块与重解析弹层；`api/client.ts` 的 `requestForm` 负责 multipart 上传（不手写 Content-Type）。
- 工作台页面：`views/agents/`（列表/编辑器）、`views/capabilities/`（工具目录/技能库/版本编辑器）；`ChatView` 顶部有 `AgentSelector`、能力抽屉和全局 `ApprovalCard`；`ToolCallList` 展示计划、工具、重试与子 Agent。

### 数据与迁移

- 迁移 head 为 `0013`（`backend/alembic/versions/`）。应用启动**不会**自动迁移；新增模型后必须在 `backend/alembic/env.py` 显式导入，否则 `alembic check` 会漏表。
- 配置统一前缀 `EA_`，从 `backend/.env` 读取（`app/core/config.py`），密码用 `SecretStr`；测试库必须与开发库不同且以 `_test` 结尾。

## 关键约束

- 使用中文沟通与注释；注释说明“为什么”，Python 函数要有类型注解和 Google 风格 docstring。
- 修改代码必须同步更新受影响的技术文档（`docs/development/`），区分“规划能力”和“已实现能力”。
- 按 `docs/项目阶段实施步骤.md` 推进，验收证据写回 `docs/development/` 后再标记完成。
- 后端：FastAPI 路由用 `async def`，每个并发任务使用独立数据库会话，事务不跨越模型调用或 SSE 等待。
- 不要同时启动多个指向同一数据库的后端；单进程由 advisory lock 强制。
- 集成测试会执行迁移的降级/升级，只能对专用可丢弃的 `*_test` 库运行；不要与真实模型验收脚本并行执行。
- 不删除 `.env`、不提交密钥、不修改任务无关文件；保留 `prototype/` 原样。
- 不用原型模拟数据或测试替身作为真实数据库/模型验收证据。
