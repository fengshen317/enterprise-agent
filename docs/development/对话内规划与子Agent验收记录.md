# 对话内规划与子 Agent 验收

日期：2026-11-05。范围：已有问答入口的规划、受控独立子 Agent、模型动作重试及记录展示。

## 自动化

统一执行 node scripts/check.mjs --database，退出码 0。

|检查|结果|
|---|---|
|Ruff / 格式|通过，81 个 Python 文件|
|严格 mypy|通过，45 个应用源文件|
|后端 pytest|209 项通过，193.29 秒|
|总体覆盖率|89.46%，超过 80% 门禁|
|ESLint / vue-tsc|通过|
|前端 Vitest|52 项通过|
|生产构建|通过|

完整日志：[自动化日志](verification/2026-11-05-chat-agents-checks.txt)。新增用例验证主图规划、独立上下文和双子图并行、共享权限下真实统计、重试不重复委派、输入与次数限制、取消传播、同对话继续、前端计划与失败/等待展示。原问答、鉴权、管理与旧 Task 回归仍通过。

## 真实模型

执行 backend 下 uv run python -m scripts.verify_real_chat_agents，最终退出码 0。使用临时账号、真实 Cookie/CSRF 登录、独立 PostgreSQL 测试库和官方 DeepSeek API。

- 模型 deepseek-flash，问答 Run completed，无错误码。
- 两次公开计划更新；两个真实独立子 Agent，均 completed。
- 真实统计结果校验通过（临时用户只有本轮一个问答 Run）。
- 六个逻辑模型动作，最终正文 301 字符。
- 退出前按本轮精确身份清理临时消息、运行、对话和用户空间资料。

[最终真实模型日志](verification/2026-11-05-chat-agents-real-model.txt)。初次脚本遇到清理外键顺序问题，已修正并清理已知临时资料后重新验收；保留 [初次日志](verification/2026-11-05-chat-agents-initial-model.txt) 便于复盘，不作为最终通过证据。

## 产品入口及限制

侧边栏移除独立计划入口，旧 /tasks 重定向 /chat；问答消息内展示规划、子 Agent 和执行依据，不要求编辑 JSON。可在原对话停止、重试或停止后补充要求。

本轮未执行真实浏览器视觉复核。网络故障通过真实 SDK + 受控 HTTP 验证，非真实供应商故障演练。刷新恢复展示已验证，但没有对话图任意节点重启续跑能力；旧 Task 的动作恢复不能作为该能力的验收证据。业务写入、递归委派、联网与文件能力没有开放。
