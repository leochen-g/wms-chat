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

首次连接会自动安装 `paho-mqtt`。群消息默认**不需要 @机器人**；想改成必须 @，设置 `WMS_CHAT_REQUIRE_MENTION=true`。
完整配置见 [README](https://github.com/leochen-g/wms-chat#readme)。
