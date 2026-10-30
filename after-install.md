## 微秘书 (WMS Chat) 已安装

**1. 启用插件**

```bash
hermes plugins enable wms-chat
```

**2. 配置 apiKey**（在微秘书[个人中心](https://wechat.aibotk.com/user/info)获取），写入 `~/.hermes/.env`：

```bash
WMS_CHAT_API_KEY=你的apiKey
# 谁可以和 agent 对话（二选一）
WMS_CHAT_ALLOWED_USERS=wxid_xxx,wxid_yyy      # 私聊白名单
# WMS_CHAT_ALLOW_ALL_USERS=true               # 仅开发用
# 群聊：整群授权，该群内任何人都可触发
# WMS_CHAT_GROUP_ALLOWED_CHATS=group:roomid_xxx
```

**3. 启动**

```bash
hermes gateway run
```

首次连接会自动安装 `paho-mqtt`。群消息默认**不需要 @机器人**；想改成必须 @，设置 `WMS_CHAT_REQUIRE_MENTION=true`。
完整配置见插件目录下的 `README.md`。
