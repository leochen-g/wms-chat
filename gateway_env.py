"""Authorization env names (``PlatformEntry`` fields), kept out of the adapter module.

平台的自动启用走 ``env_enablement_fn``（有 WMS_CHAT_API_KEY 即启用），不需要 EnvCred：
用户插件的 ``Platform("wms_chat")`` 在注册之前无法解析，模块级构造 EnvCred 会失败。
"""

AUTHZ = {
    "allowed_users_env": "WMS_CHAT_ALLOWED_USERS",
    "allow_all_env": "WMS_CHAT_ALLOW_ALL_USERS",
    # 群聊按 chat 授权：值写 group:<roomid>，该群内任何人都可触发（无需逐个加 wxid）
    "group_allowed_chats_env": "WMS_CHAT_GROUP_ALLOWED_CHATS",
    "allowlist_suppresses_pairing": True,
}
