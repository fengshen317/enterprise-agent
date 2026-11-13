# 序列 · 企业 Agent 工作台

基于 Deep Agents 技术方向的企业问答与任务平台，已完成阶段一和阶段二本地问答功能，阶段三本地核心已实现，阶段四进行中。**当前仅本地运行，不需要 Docker。**

## 已实现

- Vue 3 / TypeScript 正式前端与系统状态页面。
- FastAPI 应用、环境配置、JSON 日志、统一错误响应与请求关联标识。
- SQLAlchemy 异步会话、已有 PostgreSQL 服务、Alembic 初始化迁移。
- 存活和就绪检查、前端 API 代理、后端与前端自动检查。
- Conversation、Message、Run 数据模型与 0002 迁移，以及真实 PostgreSQL 约束验证。
- Deep Agents + DeepSeek 真实问答、连续追问、历史记录、SSE、取消与失败状态。
- 自动持久化上下文摘要与增量压缩，保留全部原始聊天记录；摘要始于迁移 0003，当前 head 为 0016。
- Vue 正式问答工作台，刷新恢复、对话切换与重试。
- 问答正文支持安全 Markdown 渲染：表格、代码块、列表与链接；HTML 经 DOMPurify 白名单清洗。
- Cron 定时问答：按计划向固定对话追加真实运行，支持创建、编辑、启停、立即执行和跳过记录。
- 知识库 MVP：RAGFlow 1.0 解析与检索、空间/私有权限、文档上传/解析/删除、主/子 Agent 检索与来源引用；支持原文件预览下载、分块浏览、检索测试和解析配置模板。
- Agent 工作台：每个对话可选择 Agent；支持配置职责指令、工具、技能和知识库范围；技能为版本化 Markdown/SOP；写工具在对话中逐次审批并记录审批链。
- 微信一对一渠道：登录用户在网页扫码绑定自己的微信，后端直接调用腾讯 iLink 协议收发消息。支持 /new、/ws、/agent、写操作确认码，以及 24 小时窗口内把定时问答结果推回微信。

现有产品原型位于 prototype/，与正式应用分离。已实现 Cookie 登录、退出、空间切换与全部问答入口的私有资源鉴权。已增加只读运行统计工具和调用审计。已增加当前空间的后台用户创建、角色与成员状态管理。已在问答内增加按需规划、独立子 Agent 并行协作、单模型动作网络重试与执行记录，并接入 RAGFlow 知识库 MVP；旧独立任务后端保留兼容。写操作逐次审批已实现；MCP、HTTP/OpenAPI 外部工具、Python 插件、文档级 ACL、独立 Worker 与管理员代审批仍未实现。

## 开始开发

环境要求：Python 3.13、uv、Node.js 22.12+（本次使用 24）、npm、本机 PostgreSQL 16+。

1. 阅读 [本地开发指南](docs/development/本地开发指南.md)，确认已有 PostgreSQL 服务运行，使用 postgres 用户配置连接。
2. 在 backend/ 执行 uv sync --frozen 和 uv run alembic upgrade head。
3. 在 backend/ 执行 uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload。
4. 双击 scripts/start-frontend.bat 启动前端。也可跳过第 3 步，在根目录执行 python scripts/start.py 同时启动前后端。

访问：问答 http://127.0.0.1:8686/chat（系统状态 /system） ，接口文档 http://127.0.0.1:8000/docs 。

## 文档入口

- [对话内规划与子 Agent：实现、配置与验收](docs/development/对话内规划与子Agent实施.md)

- [规划执行实施步骤](docs/development/规划执行实施步骤.md)
- [规划执行实现与接口](docs/development/规划执行实现与接口.md)
- [规划执行验收记录](docs/development/规划执行验收记录.md)

- [Agent 规划、执行与网络重试设计（待实施）](docs/development/Agent规划执行与网络重试设计.md)

- [后台用户与角色管理](docs/development/后台用户与角色管理.md)
- [后台管理验收记录](docs/development/后台管理验收记录.md)
- [定时问答任务](docs/development/定时问答任务.md)
- [Agent 工作台与能力扩展设计](docs/development/Agent工作台与能力扩展设计.md)
- [Agent 工作台实现与验收](docs/development/Agent工作台实现与验收.md)
- [知识库权限隔离与 RAGFlow 落地设计](docs/development/知识库权限隔离与RAGFlow落地设计.md)
- [知识库实现与验收](docs/development/知识库实现与验收.md)

- [阶段四实施计划](docs/development/阶段四实施计划.md)
- [原生工具与调用审计](docs/development/原生工具与调用审计.md)
- [阶段四验收记录](docs/development/阶段四验收记录.md)

- [登录鉴权验收记录](docs/development/登录鉴权验收记录.md)
- [登录、鉴权与管理员初始化](docs/development/登录与接口鉴权.md)
- [阶段三实施计划](docs/development/阶段三实施计划.md)
- [阶段三数据模型与权限方案](docs/development/阶段三数据模型与权限方案.md)
- [阶段三数据层验收](docs/development/阶段三数据层验收记录.md)
- [项目阶段实施步骤](docs/项目阶段实施步骤.md)
- [产品功能与原型说明](docs/产品功能与原型说明.md)
- [本地开发指南](docs/development/本地开发指南.md)
- [阶段二实施计划](docs/development/阶段二实施计划.md)
- [对话上下文压缩与验收](docs/development/对话上下文压缩.md)
- [阶段二架构与接口](docs/development/阶段二架构与接口.md)
- [阶段二验收记录](docs/development/阶段二验收记录.md)
- [阶段二数据模型](docs/development/阶段二数据模型.md)
- [阶段一架构与接口](docs/development/阶段一架构与接口.md)
- [技术决策 0001：本地运行与工程基础](docs/decisions/0001-本地运行与工程基础.md)
- [阶段一最终验收记录](docs/development/阶段一最终验收记录.md)
- [阶段一历史记录](docs/development/阶段一验收记录.md)

本项目代码改动必须同步更新受影响的技术文档，该要求仅适用于本项目。

数据库配置已切换为复用现有 postgresql-x64-16 服务（示例端口 5432）并使用 postgres 用户；请在 backend/.env 分别填写 EA_DATABASE_HOST、EA_DATABASE_PORT、EA_DATABASE_USER、EA_DATABASE_PASSWORD、EA_DATABASE_NAME，密码使用原文。现有服务已连接，开发库与测试库已创建；两库表结构初始化和完整数据库验收均已通过。

真实问答请在 backend/.env 填写 EA_DEEPSEEK_API_KEY，默认 EA_DEEPSEEK_MODEL=deepseek-flash。知识库需要在 backend/.env 填写 EA_RAGFLOW_API_KEY 和 EA_RAGFLOW_BASE_URL；未配置时知识库工具不会注册。更新代码后请重启旧后端，使用单进程本机服务。完整检查：node scripts/check.mjs --database。

首次登录前，在 backend 执行 uv run python -m app.cli.accounts init --username admin --claim-legacy 并按提示设置密码，再重启后端；前端登录入口为 http://127.0.0.1:8686/login。没有默认密码，已有账号无需重复初始化。

工具体验：登录后询问“请查询我在当前空间最近 7 天的运行统计”，展开回答下方的工具调用记录。模型仅能读取本人当前空间的聚合结果。

后台入口：http://127.0.0.1:8686/admin/members，仅当前空间管理员可访问。可以创建用户、设置管理员/普通成员、停用或恢复当前空间访问，并查看管理操作记录。

定时任务入口：http://127.0.0.1:8686/schedules。可直接新建对话或选择已有对话，用可视化执行时间构建器选择每天、每周、每月、固定间隔，也可切换高级 Cron；到期后自动向固定对话追加一次真实问答。任务只属于当前用户和空间。若该用户已绑定微信，且 24 小时内在微信里发过消息，完成结果会同时推送到微信；否则只留在网页并记录未推送原因。

微信绑定入口：http://127.0.0.1:8686/channels/wechat。需要在 backend/.env 配置 EA_CHANNEL_TOKEN_KEY。每位员工绑定自己的微信，只支持一对一，不支持群聊。页面会显示推送窗口还开到什么时候，以及因为超过 24 小时而没有推送的记录。详见 [微信 iLink 渠道](docs/development/微信iLink渠道.md)。

Agent 入口：http://127.0.0.1:8686/agents，可创建私有或空间 Agent，绑定工具、技能和知识库范围；能力中心 http://127.0.0.1:8686/capabilities 提供内置工具目录、技能编辑与版本发布。新建对话时选择 Agent，生成中可查看“能力”抽屉；写工具会在输入框上方弹出审批卡片。

知识库入口：http://127.0.0.1:8686/knowledge。空间管理员可创建空间知识库，普通成员可创建私有知识库；上传文档后由 RAGFlow 解析。详情页支持原文件预览/下载、分块浏览、解析配置模板、检索测试和批量重新解析。问答中主 Agent 和子 Agent 会自动检索有权限的知识库并引用来源。管理员不能读取他人的私有知识库。

统一入口：http://127.0.0.1:8686/chat。简单问题直接回答，复杂目标按需规划并委派子 Agent；在消息下方查看“规划与执行”。例如：“先规划，再让分析员和复核员分别分析企业 Agent 的本地开发方案，最后汇总”。无需创建独立任务或编辑 JSON。旧 /tasks 地址重定向问答。
