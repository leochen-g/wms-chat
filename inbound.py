"""入站：解析 MQTT 消息、群/私聊准入判断。纯函数，便于单测。

默认范围：所有好友、所有群。群消息默认 **不需要 @机器人**（require_mention=false）。准入链：
  group_policy(open|allowlist|disabled) -> allowed_users(默认空=不限) -> group_allow_from -> require_mention(+免@群)
allowed_users 只按 senderId(wxid) 匹配：昵称可被任意修改，不能用于授权。
群里没 @ 的消息会带 ``reply_expected=False`` 交给 agent，agent 判断无需回复时可保持沉默。
"""

import json
from typing import Any, Optional

from .config import AccountSettings
from .outbound import prefix_chat_id

REQUIRED_STR_FIELDS = ("content", "robotId", "senderId", "messageId")


def parse_inbound(raw: bytes) -> Optional[dict[str, Any]]:
    try:
        obj = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or any(not isinstance(obj.get(k), str) for k in REQUIRED_STR_FIELDS):
        return None
    return obj


def is_mentioned(m: dict[str, Any]) -> bool:
    return m.get("isGroupMention") is True or f"@{m['robotId']}" in m["content"]


def match_list(entries, *candidates: str) -> bool:
    """大小写不敏感；``*`` 通配。entries 需已小写。"""
    if "*" in entries:
        return True
    return any(c and c.lower() in entries for c in candidates)


def sender_allowed(s: AccountSettings, m: dict[str, Any]) -> bool:
    """allowed_users 为空 = 不限制；否则只按 senderId 精确匹配（大小写不敏感，``*`` 通配）。"""
    return not s.allowed_users or match_list(s.allowed_users, m["senderId"])


def admit(s: AccountSettings, m: dict[str, Any]) -> Optional[str]:
    """None=放行；否则返回拒绝原因（用于 debug 日志）。DM 授权交给 Hermes 网关。"""
    if m.get("isMyself") is True or m["senderId"] == m["robotId"]:
        return "self_echo"
    if not m.get("isGroup"):
        return None if sender_allowed(s, m) else "sender_not_allowed"
    group_id = m.get("groupId") or ""
    if s.group_policy == "disabled":
        return "group_disabled"
    if s.group_policy == "allowlist" and group_id not in s.groups:
        return "group_not_allowlisted"
    if group_id in s.group_allowed_chats:
        pass  # 整群授权：跳过 allowed_users
    elif not sender_allowed(s, m):
        return "sender_not_allowed"
    if s.group_allow_from and not match_list(s.group_allow_from, m["senderId"], m.get("senderName", "")):
        return "sender_not_allowed"
    if s.require_mention and group_id not in s.free_response_groups and not is_mentioned(m):
        return "not_mentioned"
    return None


def reply_expected(m: dict[str, Any]) -> Optional[bool]:
    """True=明确对机器人说；False=群里未 @（agent 可保持沉默）；None=私聊/未知。"""
    if not m.get("isGroup"):
        return None
    return True if is_mentioned(m) else False


def chat_identity(m: dict[str, Any], account: str) -> tuple[str, str, str]:
    """(chat_id, chat_name, chat_type)。群聊 group:<roomid>，私聊 senderId；非 default 账号加 ``<account>/`` 前缀。

    前缀保证同一个好友 / 同一个群被两个账号同时看到时，会话互相独立、回复走各自的账号。"""
    if m.get("isGroup"):
        raw = f"group:{m.get('groupId') or m['senderId']}"
        return prefix_chat_id(account, raw), m.get("groupName") or raw, "group"
    name = m.get("conversionName") or m.get("senderName") or m["senderId"]
    return prefix_chat_id(account, m["senderId"]), name, "dm"
