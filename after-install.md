## 微秘书 (WMS Chat) 已安装

**1. 启用插件**

```bash
hermes plugins enable wms-chat
```

**2. 配置 apiKey**（在微秘书[个人中心](https://wechat.aibotk.com/user/info)获取），写入 `~/.hermes/.env`：

```bash
WMS_CHAT_API_KEY=你的apiKey
# 默认：所有好友、所有群都可以和 agent 对话（无需配对码）。
# 想限制范围时再设置：
# WMS_CHAT_ALLOWED_USERS=wxid_xxx,wxid_yyy    # 只允许这些 wxid（好友和群成员都按它过滤）
# WMS_CHAT_GROUP_ALLOWED_CHATS=group:roomid_xxx   # 限定后，这些群内任何人仍可触发
# WMS_CHAT_GROUP_POLICY=allowlist
# WMS_CHAT_GROUPS=roomid_xxx                  # 只开放这些群
```

**3. 启动**

```bash
hermes gateway run
```

**多个微秘书账号共用一个 agent**：在 `~/.hermes/config.yaml` 里配置（只写这个也会自动启用）：

```yaml
gateway:
  platforms:
    wms_chat:
      extra:
        accounts:
          - {name: shop, api_key: "key-of-shop"}
          - {name: personal, api_key: "key-of-personal"}
```

非 `default` 账号的 chat_id 带 `<账号名>/` 前缀（如 `shop/wxid_abc`），主动发送时也要带。详见 README 的「多账号」。

首次连接会自动安装 `paho-mqtt`。

**4. 先发一条消息确认"入站"能进来**

日志里出现 `CONNACK ok` 和 `subscribed to ...` 只说明**出站/订阅**通了。请再用微信给机器人发一条消息，确认 gateway 日志里出现这条消息、agent 有回复。入站要过 Hermes 自己的准入检查，连上 MQTT 并不代表它能过。

**已知宿主问题**（Hermes 主干的 bug，不是本插件的；本插件已绕过第一条）

| 症状（日志原文） | 原因 | 处理 |
|---|---|---|
| `MQTT connect timeout ... host socket layer problem: ... socket.create_connection fails with an empty source address` 或 Windows 上 `10051` | Hermes 给整个进程装了 IPv6/IPv4 竞速连接层，paho 建连时传的空本地绑定地址 `('', 0)` 被它当成真实网卡去绑，IPv4 连接失败 | 本插件在 paho 建连那一刻临时丢掉空的源地址，**无需操作**。看到这条说明插件版本太旧：`hermes plugins update wms-chat` |
| `invalid_params`（入站消息被拒） | 入站信封里的元数据是封闭白名单，只登记了 WhatsApp 的键，插件带自定义键会被拦 | 本插件已不再往 `metadata` 写任何自定义键，**无需操作**；同样症状出现在别的插件上，是同一个原因 |

这两条在 Hermes `main` 上能复现；我核对过 0.13.0、0.14.0、0.15.2 的发布包，**没有**这两处代码。如果你的版本也出现了，请把 `hermes --version` 和日志原文一起反馈。群消息默认**不需要 @机器人**；想改成必须 @，设置 `WMS_CHAT_REQUIRE_MENTION=true`。
完整配置见 [README](https://github.com/leochen-g/wms-chat#readme)。
