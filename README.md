# wms-chat — Hermes 微秘书渠道插件

通过 **MQTT** 把 [微秘书](https://wechat.aibotk.com/) 的微信私聊 / 群聊接入 [Hermes Agent](https://hermes-agent.nousresearch.com/)。

```
微信 ─► 微秘书 ─MQTT reciveTopic─► wms-chat ─► Hermes agent
微信 ◄─ 微秘书 ◄─MQTT sendTopic──── wms-chat ◄─ Hermes agent
```

1. 启动时调用 `GET /openapi/v1/openclaw/chat/config?apiKey=...` 获取 MQTT 连接参数
2. 建立 MQTT 连接，订阅 `reciveTopic`
3. 收到消息后生成 `MessageEvent` 交给 Hermes gateway
4. agent 的回复发布到 `sendTopic`

断线后指数退避自动重连（每次重连重新拉取配置）；重连次数用完会交给 gateway 的重连机制重建。

**支持多个微秘书账号共用同一个 agent**：每个账号一条独立的 MQTT 连接，见下文[多账号](#多账号)。

## 安装

```bash
# 推荐：从 GitHub 安装（可用 hermes plugins update wms-chat 更新）
hermes plugins install leochen-g/wms-chat
# 或完整 URL
hermes plugins install https://github.com/leochen-g/wms-chat.git

hermes plugins enable wms-chat
```

离线安装：把本目录放到 `~/.hermes/plugins/wms-chat/`（`plugin.yaml` 必须直接在该目录下），再执行 `hermes plugins enable wms-chat`。

依赖 `paho-mqtt`，首次连接时 Hermes 会自动安装；也可手动 `pip install "paho-mqtt>=1.6,<3"`。

## 配置

只要在 `~/.hermes/.env` 里设置了 `WMS_CHAT_API_KEY`，平台就会自动启用。apiKey 从微秘书[个人中心](https://wechat.aibotk.com/user/info)获取。

```bash
WMS_CHAT_API_KEY=your-api-key
WMS_CHAT_HOME_CHANNEL=wxid_aaa          # 可选：cron / 通知的默认投递目标
```

### 环境变量

环境变量优先于 `config.yaml`（`gateway.platforms.wms_chat.extra`，键名为小写下划线，如 `require_mention`）。以下除 `API_KEY` / `HOME_CHANNEL` 外都是全局默认，多账号时可在 `accounts[]` 里逐账号覆盖。

| 变量 | 默认 | 说明 |
|---|---|---|
| `WMS_CHAT_API_KEY` | — | 微秘书 apiKey，对应 `default` 账号。未使用 `extra.accounts` 时必填 |
| `WMS_CHAT_API_BASE_URL` | `https://api-bot.aibotk.com` | 后端地址 |
| `WMS_CHAT_ALLOWED_USERS` | 空 = 不限制 | 限定可触发 agent 的 wxid，逗号分隔，支持 `*`。**只按 wxid 匹配，不看昵称**（昵称可被任意修改） |
| `WMS_CHAT_GROUP_ALLOWED_CHATS` | — | 设了 `ALLOWED_USERS` 后，这些群（`group:<roomid>`，逗号分隔）内任何人仍可触发 |
| `WMS_CHAT_REQUIRE_MENTION` | `false` | 群消息是否必须 @机器人。默认 `false`：不 @ 也会收到 |
| `WMS_CHAT_FREE_RESPONSE_GROUPS` | — | `REQUIRE_MENTION=true` 时仍无需 @ 的群 ID（不带 `group:` 前缀） |
| `WMS_CHAT_GROUP_POLICY` | `open` | `open` / `allowlist` / `disabled` |
| `WMS_CHAT_GROUPS` | — | `GROUP_POLICY=allowlist` 时允许的群 ID，逗号分隔（不带 `group:` 前缀） |
| `WMS_CHAT_GROUP_ALLOW_FROM` | — | 群内允许触发的 senderId / 昵称，逗号分隔，空=不限，支持 `*`，大小写不敏感 |
| `WMS_CHAT_HOME_CHANNEL` | — | cron 投递默认目标；`WMS_CHAT_HOME_CHANNEL_NAME` 可设显示名 |
| `WMS_CHAT_MAX_RECONNECT_ATTEMPTS` | `10` | MQTT 最大重连次数 |
| `WMS_CHAT_RECONNECT_BASE_DELAY` | `2` | 重连基础延迟（秒，指数退避，上限 60） |

### 默认范围：所有好友、所有群

插件以 `trusted_inbound` 注册，Hermes 网关**不再**要求白名单或配对码。只要 `WMS_CHAT_API_KEY` 配好，机器人账号下的**所有好友**和**所有群的所有成员**都能和 agent 对话。启动时日志会提示这一点。

> ⚠️ 这意味着任何能给这个微信号发消息的人，都能让你的 agent 执行它被允许的操作（包括终端、文件等工具）。如果 agent 开了高权限工具，请务必收紧范围。

收紧范围，按需组合（全部在插件的 `inbound.admit` 里执行）：

| 目标 | 配置（多账号时可写在 `accounts[]` 里逐账号设置） |
|---|---|
| 只允许指定的人（好友 + 群成员） | `WMS_CHAT_ALLOWED_USERS=wxid_a,wxid_b` |
| 只开放指定的群 | `WMS_CHAT_GROUP_POLICY=allowlist` + `WMS_CHAT_GROUPS=roomid1,roomid2` |
| 指定人 + 某些群里任何人都行 | `WMS_CHAT_ALLOWED_USERS=...` + `WMS_CHAT_GROUP_ALLOWED_CHATS=group:roomid1` |
| 禁用所有群 | `WMS_CHAT_GROUP_POLICY=disabled` |
| 群内只允许指定发送者 | `WMS_CHAT_GROUP_ALLOW_FROM=wxid_a,Alice`（可按 wxid 或昵称，仅作群内附加过滤） |

### 群聊不 @ 也能收到

默认 `WMS_CHAT_REQUIRE_MENTION=false`，群里所有（通过过滤的）消息都会交给 agent。没被 @ 的消息带 `reply_expected=False`，agent 判断无需回复时可以保持沉默，不会每条都硬回。若希望恢复"必须 @"，设 `WMS_CHAT_REQUIRE_MENTION=true`，并用 `WMS_CHAT_FREE_RESPONSE_GROUPS` 给个别群开例外。

> 注意：媒体消息（图片、视频等）无法携带 @。若开了 `REQUIRE_MENTION=true`，需要平台侧在发布媒体消息时把 `isGroupMention` 置为 `true`，否则会被拦截。

## 多账号

一个 Hermes agent 可以同时接入多个微秘书账号（共用记忆、技能和工具）。每个账号一条独立的 MQTT 连接，各自重连，互不影响；一个账号断线或 apiKey 错误不会拖垮其他账号。

### 配置

单账号：只在 `.env` 里写 `WMS_CHAT_API_KEY`，行为和以前完全一样（账号名为 `default`，chat_id 不带前缀）。

多账号：在 `~/.hermes/config.yaml` 的 `gateway.platforms.wms_chat.extra.accounts` 里列出。**只写 `accounts` 也会自动启用平台**，不需要再设 `WMS_CHAT_API_KEY`；两者可以并存（`WMS_CHAT_API_KEY` 对应 `default` 账号）。

```yaml
gateway:
  platforms:
    wms_chat:
      extra:
        # 以下为全局默认，对所有账号生效（也可用对应的环境变量）
        require_mention: false
        accounts:
          - name: shop                # 账号名：字母/数字/_/-，最长 32，不能重复
            api_key: "key-of-shop"    # 每个账号自己的 apiKey，不能重复
            allowed_users: wxid_boss  # 以下均为可选，逐账号覆盖全局
            group_policy: allowlist
            groups: [roomid1, roomid2]
          - name: personal
            api_key: "key-of-personal"
            allowed_users: []         # 显式写空列表 = 该账号不限制（覆盖全局）
```

可逐账号覆盖的项：`api_base_url`、`allowed_users`、`group_allowed_chats`、`require_mention`、`group_policy`、`groups`、`free_response_groups`、`group_allow_from`、`max_reconnect_attempts`、`reconnect_base_delay_s`。没写的项继承全局（环境变量 > `extra` 顶层 > 默认值）。配置有误的账号（名字非法、重名、缺 `api_key`、`api_key` 重复）会被跳过，并在启动日志里给出原因。

> `api_key` 写在 `config.yaml` 里，请注意文件权限（`chmod 600`），不要提交到 Git。

### chat_id 与会话隔离

同一个好友可能同时加了两个账号，两个账号也可能在同一个群里。为了让会话互不串线、回复走对的账号，**非 `default` 账号的 chat_id 带 `<账号名>/` 前缀**：

| 账号 | 私聊 chat_id | 群聊 chat_id |
|---|---|---|
| `default` | `wxid_abc` | `group:roomid1` |
| `shop` | `shop/wxid_abc` | `shop/group:roomid1` |

- **入站**：消息进来时按账号生成 chat_id，所以同一个人在两个账号下是两条独立会话。
- **出站**：agent 回复时 Hermes 把原 chat_id 传回来，插件按前缀把消息发到对应账号的 `sendTopic`。
- **主动发送 / cron / `send_message`**：目标写成 `shop/wxid_abc`、`shop/group:roomid1|wxid_a,wxid_b`。**多账号且没有 `default` 账号时，不带前缀的目标会被拒绝**（报错并列出账号名），不会随便挑一个账号发。`WMS_CHAT_HOME_CHANNEL` 同理，多账号时请写带前缀的值。

### 注意

- **同一个 apiKey 同一时刻只能被一个 Hermes 进程使用**（MQTT clientId 相同会互相踢下线）。如果某个账号的 key 已被另一个 gateway 占用，该账号会被跳过并在日志里提示，其余账号照常运行。
- 所有账号共用同一个 agent：它们共享记忆和工具权限。如果不同账号需要隔离（不同人格 / 不同权限），请改用 Hermes 的多 profile，每个账号一个 profile。
- 按账号区分访问范围时，记得逐账号设置 `allowed_users`：默认每个账号都是"所有好友、所有群"。
- 一个账号的重连次数用完后只会让该账号离线；全部账号都离线才会触发 gateway 重建 adapter。

## 消息格式

### 入站（reciveTopic）

| 字段 | 说明 |
|---|---|
| `content` | 消息内容（文字原文，媒体为格式化描述） |
| `robotId` / `senderId` / `senderName` | 机器人 ID / 发送者 wxid / 昵称 |
| `isGroup` / `groupId` / `groupName` | 是否群消息 / 群 ID / 群名 |
| `isGroupMention` | 是否 @了机器人 |
| `messageId` / `timestamp` | 消息 ID（用于去重）/ 13 位毫秒时间戳 |
| `type` | `文字` `图片` `视频` `文件` `语音` `名片` `h5链接` `视频号` `位置` `历史记录` |
| `url` | 媒体链接（OSS URL；未配置 OSS 时是 base64，会被忽略） |
| `isMyself` | 是否机器人自己发的（丢弃） |

必填：`content` `robotId` `senderId` `messageId`（均为字符串），缺失的消息会被丢弃。

### 出站（sendTopic）

```json
{ "isGroup": false, "contactId": "wxid_user", "messages": [{ "type": 1, "content": "文字" }] }
```
```json
{ "isGroup": true, "groupId": "roomid_xxx", "mentionIds": ["wxid_a"],
  "messages": [{ "type": 1, "content": "文字" }, { "type": 2, "url": "https://example.com/a.jpg" }] }
```

`type`：`1`=文字，`2`=图片 URL。

### 发送目标（chat_id / `send_message`）

| 格式 | 含义 |
|---|---|
| `wxid_xxx` | 私聊 |
| `group:roomid_xxx` | 群聊 |
| `group:roomid_xxx\|wxid_a,wxid_b` | 群聊并 @这些人 |
| `<账号名>/wxid_xxx` 等 | 同上，指定由哪个账号发送（非 `default` 账号必须带前缀） |

## 媒体文件

入站的图片 / 视频 / 语音 / 文件会下载到 Hermes 的媒体缓存（`~/.hermes/cache/{images,videos,audio,documents}`），作为本地路径交给 agent；文件保留消息里"文件名：xxx"的原名。下载走 Hermes 的 SSRF 防护和大小上限（`gateway.max_inbound_media_bytes`），失败不会阻塞文字派发。

出站目前支持文字和图片 URL（`send_image`）；本地文件 / 视频 / 语音的出站不支持（微秘书发送协议只有 type 1/2）。

## 文件结构

```
wms-chat/
├── plugin.yaml        插件清单（环境变量、依赖）
├── after-install.md   安装后提示
├── __init__.py        导出 register
├── adapter.py         adapter 类 + register()
├── config.py          设置读取（.env > config.yaml），多账号解析
├── api.py             拉取 MQTT 配置
├── mqtt_session.py    单个账号的 MQTT 连接 / 重连 / 发布
├── inbound.py         解析 + 准入判断
├── outbound.py        chat_id → sendTopic payload
├── media.py           入站媒体本地化
├── adapter_hint.py    system prompt 平台提示
├── dashboard_card.py  Dashboard 卡片信息
├── send_targets.py    send_message 目标语法
├── gateway_env.py     授权环境变量名
└── tests/
    ├── test_e2e.py            单账号端到端测试
    └── test_multi_account.py  多账号端到端测试
```

## 开发与测试

```bash
hermes plugins validate .          # 准入检查
pip install paho-mqtt amqtt        # 测试依赖（amqtt 仅测试用）
HERMES_HOME=/tmp/h PYTHONPATH=<hermes-agent 源码目录> python tests/test_e2e.py
HERMES_HOME=/tmp/h PYTHONPATH=<hermes-agent 源码目录> python tests/test_multi_account.py
```

测试使用真实的 Hermes 基类 + 本地 HTTP 配置服务 + 本地 MQTT broker。`test_e2e.py` 覆盖入站（私聊/群聊/去重/过滤）、出站 payload、媒体落盘、断线重连、cron standalone 发送；`test_multi_account.py` 覆盖账号配置解析、会话隔离、按账号的访问控制和出站路由、单账号重连互不影响、跨进程 apiKey 锁、cron 选账号。

## 已知限制

- 入站的名片 / h5 链接 / 视频号 / 位置只以 `content` 描述文字传给 agent，`mediaInfo` 未做结构化处理。
- 因为是 `trusted_inbound`，Hermes 的配对码（pairing）和 `unauthorized_dm_behavior` 对本平台不生效。
- 同一 apiKey 同一时刻只能由一个 Hermes 进程连接（MQTT clientId 相同会互相踢下线），插件对每个 apiKey 加 scoped lock 保护。
- allowlist 群首次被拦截时不会向群里发提醒（OpenClaw 版有）。
