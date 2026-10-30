"""Authorization env names (``PlatformEntry`` fields), kept out of the adapter module.

平台以 ``trusted_inbound`` 注册，网关不会用这些白名单拦人；这些名字仅用于配置界面展示，
真正的限制由 ``inbound.admit`` 读取同名环境变量执行。

平台的自动启用走 ``env_enablement_fn``（有 WMS_CHAT_API_KEY 即启用），不需要 EnvCred：
用户插件的 ``Platform("wms_chat")`` 在注册之前无法解析，模块级构造 EnvCred 会失败。
"""

AUTHZ = {
    "allowed_users_env": "WMS_CHAT_ALLOWED_USERS",
    "group_allowed_chats_env": "WMS_CHAT_GROUP_ALLOWED_CHATS",
}
