# 微信 iLink 渠道

状态：**已实现第一期**。后端直接调用腾讯公开的 iLink Bot HTTP 协议，不经过 OpenClaw gateway。只做员工自己的一对一会话，不做群聊、媒体和他人代绑。

协议对照的是 `openclaw-weixin` 主线文档 `docs/protocol_zh_CN.md`、`src/api/api.ts`、`src/monitor/monitor.ts`、`src/api/session-guard.ts`，以及 `package.json` 里的插件版本 `3.0.0-beta-0`（2026-11 读取）。下面凡是没有在真实 iLink 上跑过的行为，都单独标出。

## 已实现

- 登录用户在 `/channels/wechat` 取码、轮询、查看和解绑。扫码确认后，iLink 账号绑定到该系统用户，初始空间是当时选中的工作空间。`bot_token` 用 Fernet 加密，密钥为 `EA_CHANNEL_TOKEN_KEY`。
- `ChannelRuntime` 在拥有问答 advisory lock 的同一进程里，按账号再拿一把锁（`782341920` + 账号散列）做 `getUpdates` 长轮询。这样审批等待能唤醒本进程的 `ApprovalBroker`。
- 入站文本进入该用户当前空间的固定对话，并调用现有 `ChatRuntime.create_run`。生成过程中发 typing，结束后按约 1800 字分段回复。
- 指令：`/new` 新对话，`/ws <slug>` 切换空间（必须是有效成员），`/agent` 列出或切换 Agent。
- 写操作审批会在 24 小时窗口内推送工具名、参数预览和四位确认码。回复 `同意 CODE` 或 `拒绝 CODE 原因` 时调用现有 `ApprovalService.decide`。
- 不是这条微信消息触发的运行（含 Cron `schedule_` / 手动 `manual_`）结束后，若该用户 24 小时内有过入站且仍有 `context_token`，把结果推到微信。否则不发送，在 `channel_messages` 记下 `outside_window`、`no_inbound` 或 `no_context_token`。网页里的回答保留。
- `getUpdates` 的 `ret` 或 `errcode` 为 `-14` 时，该账号暂停 `EA_ILINK_SESSION_PAUSE_SECONDS`（默认 3600 秒）后再轮询。这是当前官方 `monitor.ts` + `session-guard.ts` 的行为。

## 配置

见 `backend/.env.example` 的 `EA_CHANNEL_*` 和 `EA_ILINK_*`。未配置密钥时，绑定接口返回 `CHANNEL_NOT_CONFIGURED`，也不会启动轮询。

`EA_ILINK_CHANNEL_VERSION` 默认 `3.0.0/enterprise-agent-0.1.0`。协议要求前三段是 openclaw-weixin 插件版本，本系统版本放在 `/` 后面。`iLink-App-ClientVersion` 只编码前三段，`3.0.0` 为十进制 `196608`。插件版本升级后要一起改这个配置。

## 数据

迁移 `0016`，就绪检查 `schema_version=16`。

- `channel_accounts`：用户、当前空间、bot id、user id、baseurl、token 密文、游标、状态、暂停到何时。同一用户同时只能有一条未解绑记录。
- `channel_bind_sessions`：扫码完成前的二维码。
- `channel_peers`：每个空间一条对端记录，保存当前对话、最近 `context_token` 和最近入站时间。
- `channel_messages`：入站按 `message_id` 去重，出站记录发送、失败或跳过原因。只存 500 字摘录。
- `channel_approval_codes`：微信短确认码。

## 尚未在真实 iLink 上验证

- 超过 24 小时后发送是否真的被静默丢弃、且不返回错误。这是 weilink 文档和官方说明里的平台限制，本仓库用本地入站时间提前拦住，没有对生产环境发过包。
- `message_state=1` 的流式增量之后，服务端会不会用同一个 `message_id` 再推一条完成态。实现上忽略状态 1，完成态才去重；若服务端只推一次且状态就是 1，这条消息会被跳过。
- 没有 `context_token` 时 `sendMessage` 是否被接受。官方客户端会警告后继续发送，但不能证明服务端接受。主动推送在缺少 token 时直接不发。
- `-14` 暂停一小时后是否必须先 `notifyStart` 才能恢复。当前官方主线仍是暂停一小时；曾有 PR 建议先 `notifyStart` 再短退避，那次修改没有体现在本次读到的 `monitor.ts` 里。本实现在开始和停止轮询时调用 `notifyStart` / `notifyStop`，暂停到期后直接重新 `getUpdates`。
- 发送成功（`ret=0`）但手机收不到的情况。协议和社区问题都提到过，本渠道只根据返回码记账。
- 实名代绑、PIN、`scaned_but_redirect` 的真实跳转主机。代码按文档处理了重定向和数字验证码，但默认走本人绑定，没有 `bot_spam_ticket`。
- 群消息、图片、文件和语音文件本身。带 `group_id` 的消息只记账跳过。语音转写文本会当成文字。

## 不做

- OpenClaw gateway、群聊、媒体上传下载、一人绑定多个微信、管理员代他人绑定。
