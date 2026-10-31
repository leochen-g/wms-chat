"""出站：chat_id -> sendTopic payload。

chat_id 约定：私聊 = wxid；群聊 = ``group:<roomid>``，可加 ``|wxid_a,wxid_b`` 在发送时 @ 这些人。
消息体 type：1=文字，2=图片 URL。
"""

from typing import Any, Collection, Optional

DEFAULT_ACCOUNT = "default"


def prefix_chat_id(account: str, chat_id: str) -> str:
    """default 账号不加前缀（单账号用户无感）；其余为 ``<account>/<chat_id>``。"""
    return chat_id if account == DEFAULT_ACCOUNT else f"{account}/{chat_id}"


def split_account(chat_id: str, known: Collection[str]) -> tuple[Optional[str], str]:
    """``shop/wxid_a`` -> (``shop``, ``wxid_a``)；无（已知）前缀 -> (None, chat_id)。"""
    target = chat_id.strip()
    if target.lower().startswith("wms_chat:"):
        target = target[len("wms_chat:"):].strip()
    head, sep, rest = target.partition("/")
    if sep and head in known:
        return head, rest
    return None, target


def parse_target(chat_id: str) -> dict[str, Any]:
    target = chat_id.strip()
    if target.lower().startswith("wms_chat:"):
        target = target[len("wms_chat:"):].strip()
    if target.startswith("group:"):
        group_id, sep, mentions = target[len("group:"):].partition("|")
        out: dict[str, Any] = {"isGroup": True, "groupId": group_id}
        ids = [m.strip() for m in mentions.split(",") if m.strip()] if sep else []
        if ids:
            out["mentionIds"] = ids
        return out
    return {"isGroup": False, "contactId": target}


def build_payload(chat_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {**parse_target(chat_id), "messages": messages}


def text_message(content: str) -> dict[str, Any]:
    return {"type": 1, "content": content}


def image_message(url: str) -> dict[str, Any]:
    return {"type": 2, "url": url}
