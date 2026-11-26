# 企业 IM 渠道

状态：**已实现**。飞书、钉钉、官方 QQ 机器人与微信 iLink 共用同一套入站处理：指令、审批、长回答截断、进度节流。企业渠道是「公司安装一个机器人，员工再绑定自己」，不是把企业密钥塞进微信那种每人一条 `bot_token` 的行里。

不包含企业微信、Slack、Telegram，也不使用任何非官方 QQ / 微信协议、逆向客户端或假登录。QQ 邮箱仍在能力中心的插件里，和这里的 QQ 机器人不是一回事。腾讯企点不在范围内。

## 共享适配

`ChannelCapabilities` 声明每个渠道的差异，入站和主动推送只看这些标记：

|标记|微信 iLink|飞书|钉钉|QQ|
|---|---|---|---|---|
|回复窗口|有，`EA_CHANNEL_REPLY_WINDOW_HOURS`（默认 24 小时），并且需要 `context_token`|无|无|被动回复 60 分钟、每条用户消息 4 次|
|窗口提醒|剩余不足 `EA_CHANNEL_WINDOW_REMINDER_HOURS`（默认 2 小时）提醒一次|不提醒|不提醒|只在窗口后半段提醒，文案写明 60 分钟|
|没有最近单聊时主动推送|不可以|可以，用户须在应用可用范围内|不可以，否则 `robot.oto.notExist`|可以走主动消息，但要是好友且用户未关闭主动消息|
|输入状态|有|无，进度仍按原节奏发短文本|无，进度发短文本|有。进度不再另发文本，避免占满 4 次被动额度|
|审批按钮|无，回复短码|互动卡片按钮，回调还原成「同意/拒绝 短码」|无，回复短码|内嵌键盘，按钮会替用户发出同样的短码|
|Markdown|收成纯文本|能力上支持，超长回答仍先截成纯文本预览|文本模板|文本；按钮消息用 Markdown|

长回答一律在 `EA_CHANNEL_REPLY_PREVIEW_CHARACTERS`（默认 500）处截断，并附上 `EA_PUBLIC_APP_BASE_URL` 下的网页对话链接。进度仍使用 `EA_CHANNEL_PROGRESS_AFTER_SECONDS` / `EA_CHANNEL_PROGRESS_INTERVAL_SECONDS`。同一用户的发送会按平台限频拉开间隔。

删除和外发只能在网页确认。写操作可以在单聊里回复 `同意`、`1`、`拒绝`、`2`，多项时必须带确认码。决定仍进入原来的 `ApprovalService`。

`ChannelRuntime` 继续和 `ChatRuntime` 待在同一个进程里，这样审批等待能唤醒 `ApprovalBroker`。微信按账号拿 advisory lock `782341920` 做 `getUpdates`。飞书、钉钉、QQ 每个应用再拿同一把锁的另一段，保证集群模式下只有一个长连接在收事件。

密钥继续用 `EA_CHANNEL_TOKEN_KEY` / `EA_CHANNEL_TOKEN_KEY_FILE` 的 Fernet。企业应用的 App Secret 存在 `channel_apps.credentials_ciphertext`，不写进 `channel_accounts`。

## 数据

迁移 `0028`，就绪检查 `schema_version=28`。`channel_accounts.channel_type` 允许 `ilink`、`feishu`、`dingtalk`、`qq`。一个用户每种渠道同时只有一条未解绑记录；同一个平台用户也不能绑到两个工作台账号。

- `channel_apps`：整个部署每种企业渠道一个启用中的机器人。`public_config` 只放 Robot Code、是否沙箱这类非密钥字段。
- `channel_bind_codes`：8 位绑定码，15 分钟有效。员工在单聊发送 `/bind 绑定码`。
- `channel_app_events`：还没绑定的入站，只做审计，不路由到猜测出来的账号。
- `channel_peers.passive_reply_count`：QQ 对当前这条用户消息已经用掉的被动回复次数。
- `scheduled_tasks.push_channels`：`ilink`、`feishu`、`dingtalk`、`qq` 的列表。空列表表示不推送。`push_to_wechat` 仍表示列表里有没有微信，旧任务在迁移时按原开关回填。

定时结果只在任务勾选了对应渠道、并且该用户已经绑定时发送。没勾选不是投递失败，网页里的回答保留。

## 飞书

使用企业自建应用，打开机器人能力，订阅 `im.message.receive_v1`，事件订阅方式选「使用长连接接收事件」。不要用群自定义 webhook 机器人。

管理员在 `/channels/feishu` 填写 App ID 和 App Secret。出站调用 `https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id`，凭证是 `tenant_access_token`。只处理 `chat_type=p2p`。

官方文档没有 24 小时会话窗口。用户在应用可用范围内就可以主动发消息。同一用户约 5 QPS，接口总体约 50 QPS / 1000 次每分钟。可见范围不足、接收人无效会记成 `not_in_scope`，不重试。

长连接是集群模式：多个客户端里只有一个会收到事件，而且收到后要在约 3 秒内返回。本进程把问答放到后台，回调本身立即结束。飞书 SDK 的长连接跑在独立线程里；保存新凭证或停用后，监督循环会关掉这条连接再按新配置重连，不必为了换密钥重启 API。

需要的权限至少包括：读取用户发给机器人的单聊消息、以应用身份发消息。发布版本后权限才生效。

## 钉钉

使用企业内部应用机器人，消息接收选 Stream，订阅固定 topic `/v1.0/im/bot/messages/get`。不要用群 webhook 机器人，也不要做成第三方个人应用。

只处理 `conversationType=1`。主动发送用 `POST https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend`（`robotCode`、最多 20 个 `userIds`、`msgKey` / `msgParam`）。定时推送不使用会过期的 `sessionWebhook`。

员工通常要先打开过与机器人的单聊，否则返回 `robot.oto.notExist`。`chatbotId.notAllow.sendOTO` 表示机器人未允许主动单聊。`flowControlledStaffIdList` 是被限流的员工名单。这三类都按配置或权限问题记账，不自动重试。

计费以钉钉开放平台《计费模型说明》为准：标准版（未年检 / 已年检）付费 API 为每月 10000 次、20 QPS；AI 基础版及以上不设自然月调用上限，QPS 为 60。因 QPS、全局限流或网络不可达导致的失败不计入次数。基础接口有豁免清单，批量单聊接口本身没有被列进公开的免费清单，管理员应在开发者后台「资源管理」核对当前套餐。用量耗尽时，接收消息里可能出现错误码 `20001`。

## QQ

只使用 QQ 机器人开放平台（`bot.q.qq.com` 文档，接口主机 `api.sgroup.qq.com`，沙箱为 `sandbox.api.sgroup.qq.com`）。入站事件 `C2C_MESSAGE_CREATE`，意图 `GROUP_AND_C2C_EVENT`（`1 << 25`）。发送 `POST /v2/users/{user_openid}/messages`。网关用 WebSocket。

产品限制，页面和欢迎语都会写明：

- 必须先加机器人为好友。用户可以在 QQ 里关闭主动消息，关闭后主动发送失败。
- 被动回复 60 分钟、每条消息最多 4 次。进度用 `msg_type=6` 的「正在输入」，它同样占用序号，所以一次生成只发一次输入状态。
- 主动消息：企业认证或个人认证约 10 QPS，未认证约 5 QPS 且 30 次/分钟；单个好友约 20 次/分钟、每天最多约 1000 条。
- 个人主体机器人不能公开，私聊联调最多 20 个测试号。给团队使用需要企业认证。

群事件只记账。不实现腾讯企点，也不复用 QQ 邮箱连接器。

## 员工绑定

1. 空间管理员在对应渠道页保存应用凭证。
2. 员工生成绑定码，打开与机器人的单聊，发送 `/bind 绑定码`。
3. 未绑定的消息只收到这段说明，不会被送进某个猜出来的账号。
4. 绑定后的指令与微信相同：`/new`、`/ws`、`/agent`、`/help`，以及审批短码。

微信仍然只在网页扫码，不使用绑定码。

## 页面

`/channels` 下列出微信、飞书、钉钉、QQ。企业渠道页里，管理员看到凭证表单，所有成员看到绑定码和当前绑定。定时任务表单用渠道多选替换单一的「推送到微信」。列表上的「推送到微信」按钮仍只切换微信这一项，其他已选渠道保留。
