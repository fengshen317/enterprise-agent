# 决策 0004：Agent 能力模型与写操作审批

日期：2026-11-12。状态：已采纳，第一期内置工具版本已实现。

## 背景

问答入口原先由代码硬编码工具和 system prompt，所有对话能力相同。产品需要把 Agent 作为“大脑中枢”，允许配置工具、技能和知识库范围，并为写入类能力建立用户审批。

## 决策

1. **Agent Profile 是装配参数，不是代码。** 保存职责指令、模型、工具、技能版本、知识库范围和规划/委派开关；不允许上传脚本。
2. **Tool 由代码注册，第一期只做内置工具编目。** 工具声明包含稳定标识、版本、参数模型、风险、审批、超时、配额和子 Agent 可用性；HTTP/OpenAPI、MCP、Python 插件后续扩展。
3. **Skill 是版本化 Markdown/SOP。** 可声明工具与知识库范围，但必须是 Agent 能力的子集；不执行任意代码。运行时可常驻注入或通过 `load_skill` 按需加载。
4. **每个对话选择 Agent。** 对话保存 `agent_profile_id`，`NULL` 表示内置“序列助手”；生成中不能切换，切换只影响后续运行。
5. **运行开始时固定快照。** `runs.agent_snapshot` 保存 Agent 版本、工具版本、技能版本和知识库范围；执行时按快照恢复，权限策略仍按当前状态复核。
6. **只读自动执行，写入逐次审批。** 风险级别为 read/write/delete/external；非只读工具必须 `approval=always`，审批只允许 Run owner 决策，审批链写入 `approvals` + `approval_events`。
7. **单进程审批等待。** 使用 `asyncio.Event` 在当前运行内等待，超时/取消/重启都收敛为明确终态；不承诺跨进程断点恢复。
8. **子 Agent 只读。** 子 Agent 只获得 `risk=read` 且 `child_allowed` 的工具，不加载技能，不能委派或修改计划。

## 结果

- 新增 `agent_profiles`、`agent_capabilities`、`skills`、`skill_versions`、`workspace_tool_policies`、`approvals`、`approval_events` 和运行快照字段。
- `DeepSeekProvider` 只消费装配结果，不再硬编码工具名称。
- 普通问答在 `agent_profile_id=NULL` 时保持原有工具与行为。
- 后续接入外部工具时，扩展 `ToolSpec.source` 与注册表实现，不重写执行层。
