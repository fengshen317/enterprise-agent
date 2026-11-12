# Agent 工作台实现与验收

日期：2026-11-12。状态：第一期内置工具 + 技能 + 逐次审批已实现，迁移 `0012` / `0013`。

## 一、已实现范围

- Agent Profile：创建、列表、详情、完整更新、归档；职责指令、模型、可见性、规划/委派开关、欢迎页示例。
- 每个对话选择 Agent；生成中不能切换；切换只影响后续运行。
- 能力绑定：内置工具、已发布技能版本、知识库范围。
- 技能：Markdown/SOP 创建、草稿版本、发布、版本历史、归档；常驻与按需加载。
- 工具目录：`get_my_run_stats`、`search_knowledge_base`、`list_knowledge_bases`、`create_scheduled_task`；空间管理员可启停。
- 运行快照：`runs.agent_snapshot` 固定 Agent 版本、工具版本、技能版本和知识库范围。
- 写操作审批：`approvals` + `approval_events`；只允许 Run owner 决策；超时、取消、重启均收敛。
- 前端：`/agents`、`/agents/:agentId`、`/capabilities`、`/capabilities/skills/:skillId`；ChatView 增加 Agent 选择、能力抽屉、全局审批卡；工具记录支持审批/拒绝/过期状态。
- 审计：`tool_calls` 增加工具版本、风险、审批标记和参数摘要；运行轨迹接口返回脱敏快照、工具调用和审批链。

## 二、未实现范围

- HTTP/OpenAPI 工具、MCP、Python 插件、技能脚本执行。
- 文档级 ACL、跨空间 RAGFlow 团队隔离。
- 可视化工作流编辑器、用户自定义子 Agent 角色。
- 自动审批、管理员代审批、跨进程审批恢复。
- 多模型供应商切换；第一期沿用当前 DeepSeek 模型。

## 三、代码结构

| 文件 | 职责 |
|---|---|
| `app/capabilities/spec.py` | ToolSpec / ToolContext / AgentAssembly / SkillSnapshot |
| `app/capabilities/registry.py` | 内置工具注册与目录 |
| `app/capabilities/builtins/*` | 运行统计、知识库、定时任务、技能加载 |
| `app/services/agent_assembler.py` | 按 Agent 配置和运行快照装配能力 |
| `app/services/agents.py` | Agent CRUD、权限、绑定校验、工具策略 |
| `app/services/skills.py` | 技能与不可变版本、发布/归档 |
| `app/services/approvals.py` | 审批创建、等待、决策、执行标记 |
| `app/models/agent.py` / `skill.py` / `approval.py` | 新增数据模型 |
| `alembic/versions/0012_agent_workbench.py` | Agent/技能/工具策略/快照 |
| `alembic/versions/0013_approvals.py` | 审批与 ToolCall 扩展 |
| `frontend/src/views/agents/*` | Agent 列表与编辑器 |
| `frontend/src/views/capabilities/*` | 工具目录、技能库、技能编辑器 |
| `frontend/src/components/chat/*` | Agent 选择、能力抽屉、审批卡 |
| `frontend/src/api/agents.ts` / `skills.ts` / `approvals.ts` | 前端 API 与运行时校验 |

## 四、权限规则

| 操作 | 规则 |
|---|---|
| 列表/读取 | 空间 Agent/Skill + 自己的私有配置 |
| 创建空间 Agent/Skill | 仅空间 admin |
| 创建私有 Agent/Skill | 任意有效成员 |
| 管理空间 Agent/Skill | 空间 admin 或创建者 |
| 管理私有 Agent/Skill | 仅创建者；管理员不能读取他人私有配置 |
| 绑定知识库/技能 | 只允许当前用户可读；空间 Agent 只能绑定空间资源 |
| 对话使用 Agent | 仅当前对话所有者；切换要求无活跃 Run |
| 审批决策 | 仅 Run owner；管理员不能代审批 |
| 子 Agent | 只读工具；不加载技能；不能委派 |

## 五、内置工具

| key | 风险 | 审批 | 超时 | 配额 | 说明 |
|---|---|---|---|---|---|
| `get_my_run_stats` | read | never | 5s | 4 | 运行统计 |
| `search_knowledge_base` | read | never | RAGFlow+5s | 3 | 知识库检索 |
| `list_knowledge_bases` | read | never | 5s | 2 | 知识库目录 |
| `create_scheduled_task` | write | always | 5s | 1 | 创建定时任务，固定当前对话 |
| `update_plan` / `delegate_tasks` | system | never | 2s / Run 超时 | 12 / 6 | 仅主 Agent |
| `load_skill` | system | never | 2s | 3 | 按需加载已绑定技能 |

## 六、审批状态机

```text
awaiting_approval ──approve──> running ──> completed / failed
        ├──reject──> denied
        └──timeout / restart──> expired / failed
```

- 审批超时默认 300 秒；`EA_APPROVAL_TIMEOUT_SECONDS` 可调。
- 重启时 pending 审批置 `expired`，`ToolCall` 置 failed `PROCESS_RESTARTED`，Run 失败，不恢复半完成写操作。
- 审批参数只保存脱敏预览；`load_skill` 审计不保存正文。

## 七、API

```text
GET/POST /api/agents
GET/POST /api/agents/{id}
POST     /api/agents/{id}/archive
GET      /api/tools
GET      /api/tools/{key}
POST     /api/admin/tools/{key}
GET/POST /api/skills
GET      /api/skills/{id}
POST     /api/skills/{id}
GET      /api/skills/{id}/versions
POST     /api/skills/{id}/publish
POST     /api/skills/{id}/archive
POST     /api/conversations/{id}/agent
GET      /api/runs/{id}/approvals
POST     /api/runs/{id}/approvals/{approval_id}/decide
GET      /api/runs/{id}/trace
```

## 八、验收记录

2026-11-12 本机执行：

| 检查 | 结果 |
|---|---|
| `uv run ruff check .` / `ruff format --check .` | 通过 |
| `uv run mypy app/` | 通过，78 个应用源文件 |
| `uv run pytest` | 270 项通过，后端总体覆盖率 82.92%（门禁 80%） |
| `npm run lint` / `typecheck` / `test` / `build` | 通过，前端 107 项测试 |
| `uv run alembic current` / `alembic check` | 开发库 `0013 (head)`，无模型差异 |
| `node scripts/check.mjs --database` | 全部本地检查通过，含真实测试数据库集成测试 |

新增/扩展测试：

- `test_agent_workbench_integration.py`：私有/空间 Agent 权限、版本冲突、技能版本固定、工具策略、知识库范围、对话绑定、运行快照、技能工具子集、常驻/按需装配。
- `test_approvals_integration.py`：批准执行、拒绝无副作用、非 owner 拒绝、超时、重启收敛、运行轨迹。
- `test_skill_runtime.py`：常驻/按需提示词、子 Agent 收窄、`load_skill` 正文与审计脱敏。
- 前端新增 Agent/Skill/Approval API 解析、Agent 列表/编辑器、能力中心、审批卡测试。

## 九、未执行的验收

- **未执行真实浏览器视觉验收**：前端由组件测试与生产构建覆盖；建议实际打开 `/agents`、`/capabilities`、`/chat` 检查布局和审批交互。
- **未执行真实模型冒烟**：审批集成测试使用真实 PostgreSQL 和受控工具调用，不调用付费模型；模型是否主动选择写工具取决于提示词。可执行 `backend/scripts/verify_real_agent_workbench.py`（如已添加）或手工在对话中明确要求“创建每天 9 点的定时任务”。
- **未验证真实环境中的审批超时人工等待**：自动化使用较短超时；默认 300 秒需手工观察。
- **未验证多标签页同时审批**：决策幂等由数据库行锁保证，但未做浏览器并发验收。

## 十、手工验收步骤

1. 升级数据库：`cd backend && uv run alembic upgrade head`，重启后端和前端。
2. 打开 `/capabilities`：确认工具目录包含 4 个业务工具；创建并发布一个技能。
3. 打开 `/agents`：新建私有 Agent，绑定只读工具和技能，保存。
4. 打开 `/chat`：新建对话选择该 Agent，发送问题；点“能力”查看工具/技能/知识库范围。
5. 让 Agent 创建定时任务（需绑定 `create_scheduled_task`）：输入“请创建一个每天 9 点执行的定时任务，总结本周运行情况”。在输入框上方批准或拒绝；批准后到 `/schedules` 确认任务存在。
6. 生成中尝试切换 Agent，应提示不能切换；刷新页面后执行轨迹和审批链仍可读。
7. 重启后端：如果存在 pending 审批，确认审批过期、Run 失败码 `PROCESS_RESTARTED`，且没有产生定时任务。
