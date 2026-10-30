"""WMS Chat settings: ``config.yaml`` ``extra`` + ``~/.hermes/.env`` (env wins over YAML).

Env vars (all optional except the API key):
  WMS_CHAT_API_KEY              微秘书 apiKey（必填）
  WMS_CHAT_API_BASE_URL         后端地址，默认 https://api-bot.aibotk.com
  WMS_CHAT_ALLOWED_USERS        限定可触发 agent 的 wxid（逗号分隔，支持 *）。默认空 = 所有好友、所有群成员
  WMS_CHAT_GROUP_ALLOWED_CHATS  限定了 ALLOWED_USERS 时，这些群（group:<roomid>）内任何人仍可触发
  WMS_CHAT_REQUIRE_MENTION      群消息是否必须 @机器人（默认 false：不 @ 也能收到）
  WMS_CHAT_GROUP_POLICY         open | allowlist | disabled（默认 open）
  WMS_CHAT_GROUPS               group_policy=allowlist 时允许的群 ID（逗号分隔，不带 group: 前缀）
  WMS_CHAT_FREE_RESPONSE_GROUPS 即使 require_mention=true 也无需 @ 的群 ID（逗号分隔）
  WMS_CHAT_GROUP_ALLOW_FROM     群内允许触发的 senderId/昵称（逗号分隔，空=不限，支持 *）
  WMS_CHAT_MAX_RECONNECT_ATTEMPTS / WMS_CHAT_RECONNECT_BASE_DELAY
本插件以 trusted_inbound 注册：Hermes 网关不再按白名单/配对码拦人，准入全部由本插件决定。
"""

from dataclasses import dataclass
from typing import Any, Optional

from gateway.platforms._shared import extra_or_secret

PLATFORM_NAME = "wms_chat"
DEFAULT_API_BASE_URL = "https://api-bot.aibotk.com"

ENV_API_KEY = "WMS_CHAT_API_KEY"
ENV_API_BASE_URL = "WMS_CHAT_API_BASE_URL"
ENV_HOME_CHANNEL = "WMS_CHAT_HOME_CHANNEL"

GROUP_POLICIES = ("open", "allowlist", "disabled")


def to_bool(value: Any, default: bool = False) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def to_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class WmsChatSettings:
    """Snapshot taken in the adapter constructor (MQTT callbacks run on a thread with no
    profile secret scope, so nothing may read env lazily)."""

    api_key: str
    api_base_url: str
    allowed_users: frozenset[str]
    group_allowed_chats: frozenset[str]
    require_mention: bool
    group_policy: str
    groups: frozenset[str]
    free_response_groups: frozenset[str]
    group_allow_from: frozenset[str]
    max_reconnect_attempts: int
    reconnect_base_delay: float


def load_settings(extra: Optional[dict]) -> WmsChatSettings:
    extra = extra or {}

    def get(key: str, env: str, default: Any = "") -> Any:
        return extra_or_secret(extra, key, env, default)

    policy = str(get("group_policy", "WMS_CHAT_GROUP_POLICY", "open")).strip().lower()
    if policy not in GROUP_POLICIES:
        policy = "open"
    return WmsChatSettings(
        api_key=str(get("api_key", ENV_API_KEY)).strip(),
        api_base_url=str(get("api_base_url", ENV_API_BASE_URL, DEFAULT_API_BASE_URL)).strip().rstrip("/")
        or DEFAULT_API_BASE_URL,
        # 默认空 = 不限制：所有好友、所有群都可触发
        allowed_users=frozenset(s.lower() for s in to_list(get("allowed_users", "WMS_CHAT_ALLOWED_USERS"))),
        group_allowed_chats=frozenset(
            (s[len("group:"):] if s.startswith("group:") else s)
            for s in to_list(get("group_allowed_chats", "WMS_CHAT_GROUP_ALLOWED_CHATS"))
        ),
        # 默认 False：群里不 @ 也会交给 agent（agent 可用 reply_expected=False 保持沉默）
        require_mention=to_bool(get("require_mention", "WMS_CHAT_REQUIRE_MENTION", False)),
        group_policy=policy,
        groups=frozenset(to_list(get("groups", "WMS_CHAT_GROUPS"))),
        free_response_groups=frozenset(
            to_list(get("free_response_groups", "WMS_CHAT_FREE_RESPONSE_GROUPS"))
        ),
        group_allow_from=frozenset(
            s.lower() for s in to_list(get("group_allow_from", "WMS_CHAT_GROUP_ALLOW_FROM"))
        ),
        max_reconnect_attempts=_to_int(
            get("max_reconnect_attempts", "WMS_CHAT_MAX_RECONNECT_ATTEMPTS", 10), 10
        ),
        reconnect_base_delay=_to_float(
            get("reconnect_base_delay_s", "WMS_CHAT_RECONNECT_BASE_DELAY", 2.0), 2.0
        ),
    )


def is_configured(config: Any) -> bool:
    """Used for ``validate_config`` / ``is_connected``: an API key is enough."""
    return bool(load_settings(getattr(config, "extra", None)).api_key)
