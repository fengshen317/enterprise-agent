# Agent 工作台与能力扩展设计

日期：2026-11-12。状态：第一期已实现（内置工具 + 技能 + 逐次审批）。目标：让 Agent 成为企业问答与任务的中枢，用户可以为 Agent 配置工具、技能和知识范围，架构支持后续接入 HTTP/OpenAPI、MCP 与插件。

## 一、能力模型

| 对象 | 定位 | 是否落库 | 是否执行代码 |
|---|---|---|---|
| Tool | 服务端实现的可执行能力，模型通过 function calling 调用 | 目录来自代码；空间启停策略落库 | 是，仅内置代码 |
| Skill | 版本化 Markdown/SOP：目标、步骤、示例、输出规范 | 是 | 否 |
| Knowledge | RAGFlow 数据集 + 应用侧 ACL；Agent/Skill 只声明可用范围 | 已有 | 否 |
| Subagent | 运行时 `analyst/reviewer` 子图，不可由用户创建 | 否，仅审计 | 是，继承父能力并收窄为只读 |
| Agent Profile | 面向用户的“大脑配置” | 是 | 否 |

边界：

- Agent 只是装配参数，不能上传脚本或定义新执行逻辑。
- Skill 不能新增权限；技能声明的工具必须是 Agent 工具集的子集，知识库范围只能收窄。
- 子 Agent 只保留 `risk=read` 且 `child_allowed` 的工具，不加载技能。
- 模型永远拿不到身份、SQL、RAGFlow key、handler 或内部策略。

`ToolSpec` 的关键字段：`key`、`version`、`display_name`、`description`、`args_model`、`risk`、`approval`、`timeout_seconds`、`quota_per_run`、`child_allowed`、`context_eligible`、`source`、`handler`、`audit`、`approval_preview`。工具结果统一为 `{ok, audit_id, risk, data}` 或 `{ok:false, audit_id, risk, error_code}`。

## 二、运行流程

```text
ChatView
  └─ POST /conversations/{id}/runs
       ├─ ChatRuntime.create_run
       │    └─ AgentAssembler.snapshot_for_conversation
       │         ├─ 读取 Agent、能力绑定、技能版本、知识库范围
       │         ├─ 过滤空间停用工具与不可读资源
       │         └─ 写入 runs.agent_snapshot
       └─ ChatRuntime._execute
            ├─ AgentAssembler.assemble_for_run（按快照恢复）
            ├─ ChatAgentExecutor（追加 update_plan/delegate_tasks/load_skill）
            ├─ DeepSeekProvider（只消费 assembly）
            └─ ToolExecutor.invoke
                 ├─ 权限复核 → 参数校验 → 配额
                 ├─ 写工具：ApprovalService 等待用户决策
                 ├─ handler 执行 → 超时/错误映射
                 └─ tool_calls 审计
```

装配在运行开始时固定，运行中不动态增删工具，避免权限漂移。

## 三、技能运行时

| 模式 | 行为 | 适用 |
|---|---|---|
| `always` | 技能正文注入 system prompt 的“已启用技能”段 | 短 SOP、输出规范；最多 3 个 |
| `on_demand` | 注册 `load_skill`，模型按需加载；单次 ≤ 6000 字符，总 3 次 | 长流程、领域手册 |

- 技能版本不可变；Agent 绑定固定 `skill_version_id`，发布新版本不会悄悄改变已绑定 Agent。
- 技能正文用 `<skill key="..." version="...">` 分隔，安全规则放在提示词最后。
- `load_skill` 审计只保存 key/version/length，不保存正文。
- 上下文预算在 Agent 保存和运行时双重校验。

## 四、审批运行时

状态机：

```text
awaiting_approval ──approve──> running ──> completed / failed
        ├──reject──> denied
        └──timeout / restart──> expired / failed
```

- `ApprovalService.request_and_wait`：先注册 `asyncio.Event`，再插入 `approvals` 并把 `ToolCall` 置为 `awaiting_approval`；每秒兜底查库，超时自动过期。
- `decide`：只允许 Run owner；运行必须 active、审批 pending、未过期；重复决策幂等。
- 执行完成后写 `executed/failed`；重启把 pending 审批收敛为 `expired`，不恢复半完成写操作。
- 前端通过 `RunSnapshot.approval` 和 `GET /runs/{id}/approvals` 展示审批卡片与审批链。

## 五、数据模型

迁移 `0012`：

- `agent_profiles`：名称、图标、指令、可见性、状态、版本、模型、规划/委派开关、知识库范围、欢迎页示例。
- `agent_capabilities`：Agent 与工具/技能版本的绑定；技能固定版本，工具使用当前实现。
- `skills` / `skill_versions`：技能身份与不可变版本。
- `workspace_tool_policies`：空间管理员启停内置工具。
- `conversations.agent_profile_id`、`runs.agent_profile_id`、`runs.agent_snapshot`。

迁移 `0013`：

- `approvals` / `approval_events`：审批与决策链。
- `tool_calls` 增加 `tool_version/risk_level/approval_required/arguments_digest`，状态增加 `awaiting_approval/denied/expired`。

## 六、API

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

权限沿用知识库模式：`workspace` Agent/Skill 所有有效成员可读，管理员或创建者可管理；`private` 仅创建者可见，管理员不能读取他人的私有配置。空间 Agent 只能绑定空间知识库/技能，避免私有资源被共享配置带走。

## 七、前端信息架构

```text
/chat                     问答工作台：Agent 选择、能力抽屉、审批卡片、执行轨迹
/agents                   智能体列表
/agents/:agentId          智能体编辑器
/capabilities             能力中心：工具目录 / 技能库
/capabilities/skills/:id  技能编辑器与版本历史
/knowledge                知识库
/schedules                定时任务
/system                   系统概览
/admin/members            成员与权限
```

## 八、扩展路线

1. **HTTP/OpenAPI 工具**：声明式 URL/方法/鉴权/参数映射，SSRF 白名单、密钥引用、响应裁剪。
2. **MCP**：服务允许列表、工具导入、超时与故障隔离。
3. **Python 插件**：子进程沙箱、资源限制、签名/允许列表。
4. **技能脚本**：沙箱执行，仍继承 Agent 工具边界。
5. **工作流模板**：把成熟技能组合成可复用流程；可视化编辑器后置。
6. **索引版本/文档级 ACL**：属于知识库后续阶段。

## 九、明确不做（第一期）

- HTTP/OpenAPI、MCP、Python 插件、任意脚本执行。
- 文档级 ACL、跨空间 RAGFlow 团队隔离。
- 可视化工作流编辑器、用户自定义子 Agent 角色。
- 自动审批、管理员代审批、跨进程审批恢复。
- 多模型供应商切换。
