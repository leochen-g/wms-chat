"""微秘书 (WMS Chat) platform adapter for Hermes.

Layout (mirrors the Feishu plugin):
  adapter.py       adapter class + register()           <- this file
  config.py        settings from config.yaml extra + .env
  api.py           GET /openapi/v1/openclaw/chat/config -> MQTT params
  mqtt_session.py  paho connect/subscribe/publish + backoff reconnect
  inbound.py       parse + group/DM admission (pure)
  outbound.py      chat_id -> sendTopic payload (pure)
  media.py         inbound media -> Hermes media cache
  adapter_hint.py / dashboard_card.py / send_targets.py / gateway_env.py   registry metadata
"""

import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import uuid
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms._shared import seed_extra_from_env as _seed_extra_from_env
from gateway.platforms._shared import send_error
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator

from . import inbound, media, outbound
from .api import fetch_mqtt_config, strip_scheme
from .config import (
    DEFAULT_API_BASE_URL, ENV_API_BASE_URL, ENV_API_KEY, ENV_HOME_CHANNEL, PLATFORM_NAME,
    is_configured, load_settings,
)
from .mqtt_session import MqttSession, new_paho_client, paho_importable

logger = logging.getLogger(__name__)

_MESSAGE_TYPES = {
    "PHOTO": MessageType.PHOTO, "VIDEO": MessageType.VIDEO,
    "VOICE": MessageType.VOICE, "DOCUMENT": MessageType.DOCUMENT,
}


class WmsChatAdapter(BasePlatformAdapter):
    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        self._s = load_settings(getattr(config, "extra", {}) or {})
        self._dedup = MessageDeduplicator(max_size=1000, ttl_seconds=300)
        self._session: Optional[MqttSession] = None

    @property
    def name(self) -> str:
        return "WMS Chat"

    # ---- lifecycle ------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._s.api_key:
            self._set_fatal_error("config_missing", f"{ENV_API_KEY} must be set", retryable=False)
            return False
        # 同一 apiKey 同一时刻只允许一个 profile 连接（同 clientId 会互相踢下线）
        digest = hashlib.sha256(self._s.api_key.encode()).hexdigest()[:16]
        if not self._acquire_platform_lock(PLATFORM_NAME, digest, "WMS Chat apiKey"):
            return False
        self._session = MqttSession(
            fetch_config=lambda: fetch_mqtt_config(self._s.api_base_url, self._s.api_key),
            on_message=self._on_raw_message,
            on_exhausted=self._on_reconnect_exhausted,
            max_attempts=self._s.max_reconnect_attempts,
            base_delay=self._s.reconnect_base_delay,
        )
        try:
            await self._session.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("[wms_chat] connect failed: %s", exc)
            self._session = None
            with contextlib.suppress(Exception):
                self._release_platform_lock()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False
        self._mark_connected()
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        session, self._session = self._session, None
        if session is not None:
            await session.stop()
        self._mark_disconnected()

    async def _on_reconnect_exhausted(self) -> None:
        # 交给 gateway 的 reconnect watcher 重建 adapter
        self._set_fatal_error("mqtt_reconnect_exhausted", "MQTT reconnect attempts exhausted", retryable=True)
        await self._notify_fatal_error()

    # ---- inbound --------------------------------------------------------

    async def _on_raw_message(self, raw: bytes) -> None:
        m = inbound.parse_inbound(raw)
        if m is None:
            logger.warning("[wms_chat] unparseable inbound message")
            return
        await self._handle_inbound(m)

    async def _handle_inbound(self, m: dict[str, Any]) -> None:
        try:
            reason = inbound.admit(self._s, m)
            if reason:
                logger.debug("[wms_chat] drop %s: %s", m.get("messageId"), reason)
                return
            if self._dedup.is_duplicate(m["messageId"]):
                return
            text = m["content"]
            url = m.get("url") if isinstance(m.get("url"), str) else None
            if url and not url.lower().startswith(("http://", "https://")):
                url = None  # base64 / data: —— 平台未配置 OSS 时的形态，忽略
            msg_type_name = m.get("type") or "文字"

            media_urls: list[str] = []
            media_types: list[str] = []
            media_names: list[str] = []
            msg_type = MessageType.TEXT
            if url and msg_type_name in media.KIND_BY_TYPE:
                local = await media.localize(url, msg_type_name, text)
                if local is not None:
                    media_urls.append(local.path)
                    media_types.append(local.mime)
                    media_names.append(local.filename)
                    msg_type = _MESSAGE_TYPES[media.KIND_BY_TYPE[msg_type_name][1]]
            if not text.strip():
                if media_urls:
                    text = ""
                elif url:
                    text = f"<media:{msg_type_name}> {url}"
                else:
                    return

            chat_id, chat_name, chat_type = inbound.chat_identity(m)
            source = self.build_source(
                chat_id=chat_id, chat_name=chat_name, chat_type=chat_type,
                user_id=m["senderId"], user_name=m.get("senderName") or m["senderId"],
                message_id=m["messageId"],
            )
            ts = m.get("timestamp")
            event = MessageEvent(
                text=text, message_type=msg_type, source=source,
                user_id=m["senderId"], user_name=m.get("senderName") or m["senderId"],
                message_id=m["messageId"], raw_message=m,
                media_urls=media_urls, media_types=media_types, media_filenames=media_names,
                reply_expected=inbound.reply_expected(m),
                timestamp=(datetime.datetime.fromtimestamp(ts / 1000)
                           if isinstance(ts, (int, float)) else datetime.datetime.now()),
            )
            await self.handle_message(event)
        except Exception:  # noqa: BLE001
            logger.exception("[wms_chat] inbound handling failed")

    # ---- outbound -------------------------------------------------------

    async def _publish(self, chat_id: str, messages: list[dict[str, Any]]) -> SendResult:
        session = self._session
        if session is None:
            return SendResult(success=False, error="Not connected", retryable=True)
        try:
            await session.publish(json.dumps(outbound.build_payload(chat_id, messages), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=str(exc), retryable=True)
        return SendResult(success=True, message_id=f"wms-{uuid.uuid4().hex[:12]}")

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict] = None) -> SendResult:
        return await self._publish(chat_id, [outbound.text_message(content)])

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[dict] = None) -> SendResult:
        msgs = []
        if caption and caption.strip():
            msgs.append(outbound.text_message(caption))
        msgs.append(outbound.image_message(image_url))
        return await self._publish(chat_id, msgs)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """微秘书没有输入中状态。"""

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "group" if chat_id.startswith("group:") else "dm"}


# --------------------------------------------------------------------------
# 注册钩子
# --------------------------------------------------------------------------

def check_requirements() -> bool:
    """被动探测：只检查依赖，不安装、不查凭据。"""
    return paho_importable()


def ensure_deps() -> bool:
    """主动安装 paho-mqtt（仅在平台已启用且即将连接时被调用）。"""
    if paho_importable():
        return True
    try:
        import subprocess
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "paho-mqtt>=1.6,<3"],
                       check=True, timeout=300)
    except Exception:  # noqa: BLE001
        return False
    return paho_importable()


def _env_enablement() -> Optional[dict]:
    """有 WMS_CHAT_API_KEY 就把平台标记为已配置并播种 extra（含 home channel）。"""
    key = str(_get_scoped_secret(ENV_API_KEY, "") or "").strip()
    if not key:
        return None
    seed = _seed_extra_from_env(
        ((ENV_API_BASE_URL, "api_base_url", lambda v: v.rstrip("/")),),
        home_env=ENV_HOME_CHANNEL,
    )
    return {"api_key": key, **seed}


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None,
                           force_document=False):
    """gateway 之外（hermes cron run）发送：临时建连、发布、断开。"""
    if not ensure_deps():
        return send_error("wms_chat standalone send: paho-mqtt not installed")
    s = load_settings(getattr(pconfig, "extra", {}) or {})
    if not s.api_key:
        return send_error(f"wms_chat standalone send: {ENV_API_KEY} not set")
    if not chat_id:
        return send_error("wms_chat standalone send: chat_id (wxid or group:<roomid>) required")

    def _run() -> dict:
        cfg = fetch_mqtt_config(s.api_base_url, s.api_key)
        # 复用 clientId 会把在线 gateway 挤下线，加后缀避免冲突
        client = new_paho_client(f"{cfg['clientId']}-cron-{uuid.uuid4().hex[:6]}")
        client.username_pw_set(cfg["username"], cfg["password"])
        client.connect(strip_scheme(cfg["host"]), int(cfg["port"]), keepalive=30)
        client.loop_start()
        try:
            payload = outbound.build_payload(chat_id, [outbound.text_message(message)])
            info = client.publish(cfg["sendTopic"], json.dumps(payload, ensure_ascii=False), qos=1)
            info.wait_for_publish(timeout=10)
            if not info.is_published():
                return send_error("publish not acknowledged")
            return {"success": True, "platform": PLATFORM_NAME, "chat_id": chat_id,
                    "message_id": f"wms-{uuid.uuid4().hex[:12]}"}
        finally:
            client.loop_stop()
            client.disconnect()

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _run)
    except Exception as exc:  # noqa: BLE001
        return send_error(f"wms_chat standalone send failed: {exc}")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    from . import gateway_env
    from .adapter_hint import PLATFORM_HINT
    from .dashboard_card import DASHBOARD_CARD
    from .send_targets import SEND_ENTRY_KWARGS

    ctx.register_platform(
        **SEND_ENTRY_KWARGS, **DASHBOARD_CARD,
        name=PLATFORM_NAME, label="微秘书 (WMS Chat)", adapter_factory=WmsChatAdapter,
        check_fn=check_requirements, ensure_deps_fn=ensure_deps,
        validate_config=is_configured, is_connected=is_configured,
        required_env=[ENV_API_KEY], install_hint="pip install paho-mqtt",
        env_enablement_fn=_env_enablement, cron_deliver_env_var=ENV_HOME_CHANNEL,
        standalone_sender_fn=_standalone_send,
        max_message_length=4000, emoji="💬", allow_update_command=True,
        platform_hint=PLATFORM_HINT,
        **gateway_env.AUTHZ,
    )
