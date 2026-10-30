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

## 安装

```bash
# 推荐：从 Git 仓库安装（可用 hermes plugins update 更新）
hermes plugins install <owner>/wms-chat
# 或完整 URL（私有仓库 / 自建 Git）
hermes plugins install https://git.example.com/team/wms-chat.git

hermes plugins enable wms-chat
```

离线安装：把本目录放到 `~/.hermes/plugins/wms-chat/`（`plugin.yaml` 必须直接在该目录下），再执行 `hermes plugins enable wms-chat`。

依赖 `paho-mqtt`，首次连接时 Hermes 会自动安装；也可手动 `pip install "paho-mqtt>=1.6,<3"`。

## 配置

只要在 `~/.hermes/.env` 里设置了 `WMS_CHAT_API_KEY`，平台就会自动启用。apiKey 从微秘书[个人中心](https://wechat.aibotk.com/user/info)获取。

```bash
WMS_CHAT_API_KEY=your-api-key
WMS_CHAT_ALLOWED_USERS=wxid_aaa,wxid_bbb
WMS_CHAT_HOME_CHANNEL=wxid_aaa          # cron / 通知的默认投递目标
```

### 环境变量

环境变量优先于 `config.yaml`（`gateway.platforms.wms_chat.extra`，键名为小写下划线，如 `require_mention`）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `WMS_CHAT_API_KEY` | — | **必填**。微秘书 apiKey |
| `WMS_CHAT_API_BASE_URL` | `https://api-bot.aibotk.com` | 后端地址 |
| `WMS_CHAT_ALLOWED_USERS` | — | 允许和 agent 对话的 wxid，逗号分隔（Hermes 授权层读取） |
| `WMS_CHAT_ALLOW_ALL_USERS` | `false` | 允许任何人（仅开发用） |
| `WMS_CHAT_GROUP_ALLOWED_CHATS` | — | 按群授权，值为 `group:<roomid>`，逗号分隔；该群内任何人都可触发 |
| `WMS_CHAT_REQUIRE_MENTION` | `false` | 群消息是否必须 @机器人。默认 `false`：不 @ 也会收到 |
| `WMS_CHAT_FREE_RESPONSE_GROUPS` | — | `REQUIRE_MENTION=true` 时仍无需 @ 的群 ID（不带 `group:` 前缀） |
| `WMS_CHAT_GROUP_POLICY` | `open` | `open` / `allowlist` / `disabled` |
| `WMS_CHAT_GROUPS` | — | `GROUP_POLICY=allowlist` 时允许的群 ID，逗号分隔（不带 `group:` 前缀） |
| `WMS_CHAT_GROUP_ALLOW_FROM` | — | 群内允许触发的 senderId / 昵称，逗号分隔，空=不限，支持 `*`，大小写不敏感 |
| `WMS_CHAT_HOME_CHANNEL` | — | cron 投递默认目标；`WMS_CHAT_HOME_CHANNEL_NAME` 可设显示名 |
| `WMS_CHAT_MAX_RECONNECT_ATTEMPTS` | `10` | MQTT 最大重连次数 |
| `WMS_CHAT_RECONNECT_BASE_DELAY` | `2` | 重连基础延迟（秒，指数退避，上限 60） |

### 授权：两层，别搞混

1. **插件层（本插件）**：`GROUP_POLICY` / `GROUPS` / `GROUP_ALLOW_FROM` / `REQUIRE_MENTION` 决定**哪些群消息会被送进 agent**。
2. **Hermes 网关层**：`ALLOWED_USERS` / `ALLOW_ALL_USERS` / `GROUP_ALLOWED_CHATS` 决定**谁有权限和 agent 对话**。

群里的人要触发 agent，两层都要通过：要么发送者在 `WMS_CHAT_ALLOWED_USERS` 里，要么该群在 `WMS_CHAT_GROUP_ALLOWED_CHATS` 里。私聊陌生人默认会收到 Hermes 的配对码流程；设了 `ALLOWED_USERS` 后陌生人不再收到配对码。

### 群聊不 @ 也能收到

默认 `WMS_CHAT_REQUIRE_MENTION=false`，群里所有（通过过滤的）消息都会交给 agent。没被 @ 的消息带 `reply_expected=False`，agent 判断无需回复时可以保持沉默，不会每条都硬回。若希望恢复"必须 @"，设 `WMS_CHAT_REQUIRE_MENTION=true`，并用 `WMS_CHAT_FREE_RESPONSE_GROUPS` 给个别群开例外。

> 注意：媒体消息（图片、视频等）无法携带 @。若开了 `REQUIRE_MENTION=true`，需要平台侧在发布媒体消息时把 `isGroupMention` 置为 `true`，否则会被拦截。

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
├── config.py          设置读取（.env > config.yaml）
├── api.py             拉取 MQTT 配置
├── mqtt_session.py    MQTT 连接 / 重连 / 发布
├── inbound.py         解析 + 准入判断
├── outbound.py        chat_id → sendTopic payload
├── media.py           入站媒体本地化
├── adapter_hint.py    system prompt 平台提示
├── dashboard_card.py  Dashboard 卡片信息
├── send_targets.py    send_message 目标语法
├── gateway_env.py     授权环境变量名
└── tests/test_e2e.py  端到端测试
```

## 开发与测试

```bash
hermes plugins validate .          # 准入检查
pip install paho-mqtt amqtt        # 测试依赖（amqtt 仅测试用）
HERMES_HOME=/tmp/h PYTHONPATH=<hermes-agent 源码目录> python tests/test_e2e.py
```

测试使用真实的 Hermes 基类 + 本地 HTTP 配置服务 + 本地 MQTT broker，覆盖：入站（私聊/群聊/去重/过滤）、出站 payload、媒体落盘、断线重连、cron standalone 发送。

## 已知限制

- 入站的名片 / h5 链接 / 视频号 / 位置只以 `content` 描述文字传给 agent，`mediaInfo` 未做结构化处理。
- 同一 apiKey 同一时刻只能由一个 Hermes profile 连接（MQTT clientId 相同会互相踢下线），插件用 scoped lock 保护。
- allowlist 群首次被拦截时不会向群里发提醒（OpenClaw 版有）。
