"""微秘书 (WMS Chat) platform adapter for Hermes.

Layout (mirrors the Feishu plugin):
  adapter.py       adapter class + register()           <- this file
  config.py        settings from config.yaml extra + .env (multi-account)
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
import os
import uuid
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.helpers import MessageDeduplicator

from . import compat, gateway_env, inbound, media, outbound
from .compat import MessageType
from .compat import get_scoped_secret as _get_scoped_secret
from .compat import seed_extra_from_env as _seed_extra_from_env
from .compat import send_error
from .api import fetch_mqtt_config, strip_scheme
from .config import (
    DEFAULT_ACCOUNT, ENV_API_BASE_URL, ENV_API_KEY, ENV_HOME_CHANNEL, PLATFORM_NAME,
    AccountSettings, is_configured, load_settings,
)
from .mqtt_session import MqttSession, new_paho_client, paho_importable

logger = logging.getLogger(__name__)

_MESSAGE_TYPES = {
    "PHOTO": MessageType.PHOTO, "VIDEO": MessageType.VIDEO,
    "VOICE": MessageType.VOICE, "DOCUMENT": MessageType.DOCUMENT,
}


class WmsChatAdapter(BasePlatformAdapter):
    """One adapter, N 微秘书 accounts, one shared agent.

    每个账号一条独立的 MQTT 连接（各自重连、互不影响）。非 default 账号的 chat_id 带
    ``<account>/`` 前缀：会话按账号隔离，回复按前缀路由回对应账号的 sendTopic。
    """

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        self._s = load_settings(getattr(config, "extra", {}) or {})
        self._dedup = MessageDeduplicator(max_size=2000, ttl_seconds=300)
        self._sessions: dict[str, MqttSession] = {}
        self._lock_ids: list[str] = []
        self._dead: set[str] = set()

    @property
    def name(self) -> str:
        return "WMS Chat"

    # ---- lifecycle ------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        for problem in self._s.problems:
            logger.warning("[wms_chat] config: %s", problem)
        if not self._s.accounts:
            self._set_fatal_error(
                "config_missing", f"{ENV_API_KEY} or extra.accounts must be set", retryable=False)
            return False
        started = 0
        skipped: list[str] = []
        for acct in self._s.accounts:
            # 同一 apiKey 同一时刻只允许一个 profile 连接（同 clientId 会互相踢下线）
            ident = hashlib.sha256(acct.api_key.encode()).hexdigest()[:16]
            if not self._acquire_platform_lock(PLATFORM_NAME, ident, f"WMS Chat account {acct.name!r}"):
                logger.error("[wms_chat:%s] apiKey already in use by another gateway; account skipped", acct.name)
                skipped.append(acct.name)
                continue
            self._lock_ids.append(ident)
            session = MqttSession(
                fetch_config=lambda a=acct: fetch_mqtt_config(a.api_base_url, a.api_key),
                on_message=lambda raw, n=acct.name: self._on_raw_message(n, raw),
                on_exhausted=lambda n=acct.name: self._on_account_exhausted(n),
                max_attempts=acct.max_reconnect_attempts,
                base_delay=acct.reconnect_base_delay,
                label=f"wms_chat:{acct.name}",
            )
            self._sessions[acct.name] = session
            if await session.start(keep_trying=True):
                started += 1
                if not acct.allowed_users:
                    logger.warning(
                        "[wms_chat:%s] open access: any WeChat friend and any member of any group can talk "
                        "to the agent. Set WMS_CHAT_ALLOWED_USERS (wxid list) to restrict.", acct.name)
        if started == 0:
            # 一个账号都没连上：交给 gateway 的 reconnect watcher 重建
            await self._teardown()
            self._set_fatal_error(
                "connect_failed", "no 微秘书 account could connect (check apiKey / network)", retryable=True)
            return False
        self._delegate_authorization()
        self._mark_connected()
        compat.wire_plugin_handlers(self)
        retrying = len(self._sessions) - started
        if skipped:
            logger.warning("[wms_chat] skipped (apiKey held by another gateway, restart to retry): %s",
                           ", ".join(skipped))
        if retrying:
            logger.warning("[wms_chat] %d account(s) not connected yet; retrying in the background", retrying)
        return True

    def _delegate_authorization(self) -> None:
        """准入由 inbound.admit 负责。旧版 Hermes 没有 ``trusted_inbound``，网关会按自己的白名单再拦一次，
        陌生人只会收到配对码；这里把网关层设为“放行”（用户显式设置的值优先），真正的限制仍由本插件执行。"""
        if compat.supports_trusted_inbound():
            return
        var = gateway_env.AUTHZ["allow_all_env"]
        if var not in os.environ:
            os.environ[var] = "true"
            logger.info("[wms_chat] this Hermes has no trusted_inbound; set %s=true so the gateway defers "
                        "to the plugin's own access rules (WMS_CHAT_ALLOWED_USERS etc.)", var)

    async def disconnect(self) -> None:
        await self._teardown()
        self._mark_disconnected()

    async def _teardown(self) -> None:
        sessions, self._sessions = self._sessions, {}
        for session in sessions.values():
            with contextlib.suppress(Exception):
                await session.stop()
        self._dead.clear()
        self._release_all_locks()

    def _release_all_locks(self) -> None:
        """基类只记住最后一个锁；这里逐个账号还原 identity 后释放。"""
        for ident in self._lock_ids:
            with contextlib.suppress(Exception):
                self._platform_lock_scope, self._platform_lock_identity = PLATFORM_NAME, ident
                self._release_platform_lock()
        self._lock_ids.clear()

    async def _on_account_exhausted(self, account: str) -> None:
        """单个账号重连用尽：只标记该账号失效；全部失效才交给 gateway 重建 adapter。"""
        self._dead.add(account)
        logger.error("[wms_chat:%s] reconnect attempts exhausted; account is offline until gateway restart", account)
        if self._sessions and self._dead >= set(self._sessions):
            self._set_fatal_error(
                "mqtt_reconnect_exhausted", "MQTT reconnect attempts exhausted for all accounts", retryable=True)
            await self._notify_fatal_error()

    # ---- inbound --------------------------------------------------------

    async def _on_raw_message(self, account: str, raw: bytes) -> None:
        m = inbound.parse_inbound(raw)
        if m is None:
            logger.warning("[wms_chat:%s] unparseable inbound message", account)
            return
        await self._handle_inbound(account, m)

    async def _handle_inbound(self, account: str, m: dict[str, Any]) -> None:
        try:
            acct = self._s.account(account)
            if acct is None:
                return
            reason = inbound.admit(acct, m)
            if reason:
                logger.debug("[wms_chat:%s] drop %s: %s", account, m.get("messageId"), reason)
                return
            # 去重键带账号：两个账号可能同时收到同一条群消息，各自都要处理
            if self._dedup.is_duplicate(f"{account}:{m['messageId']}"):
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

            chat_id, chat_name, chat_type = inbound.chat_identity(m, account)
            source = compat.build_source(
                self,
                chat_id=chat_id, chat_name=chat_name, chat_type=chat_type,
                user_id=m["senderId"], user_name=m.get("senderName") or m["senderId"],
                message_id=m["messageId"],
            )
            ts = m.get("timestamp")
            event = compat.make_event(
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
            logger.exception("[wms_chat:%s] inbound handling failed", account)

    # ---- outbound -------------------------------------------------------

    def _route(self, chat_id: str) -> tuple[Optional[str], str]:
        """chat_id -> (账号名, 去前缀后的目标)。无前缀：default 账号；没有 default 则仅当只有一个账号时用它。"""
        account, raw = outbound.split_account(chat_id, self._sessions.keys())
        if account is None:
            if DEFAULT_ACCOUNT in self._sessions:
                account = DEFAULT_ACCOUNT
            elif len(self._sessions) == 1:
                account = next(iter(self._sessions))
        return account, raw

    async def _publish(self, chat_id: str, messages: list[dict[str, Any]]) -> SendResult:
        account, raw = self._route(chat_id)
        if account is None:
            names = ", ".join(sorted(self._sessions)) or "none"
            return SendResult(
                success=False, retryable=False,
                error=f"cannot pick a 微秘书 account for {chat_id!r}; prefix it with <account>/ (accounts: {names})")
        try:
            await self._sessions[account].publish(
                json.dumps(outbound.build_payload(raw, messages), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"[{account}] {exc}", retryable=True)
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
        account, raw = self._route(chat_id)
        info = {"name": chat_id, "type": "group" if raw.startswith("group:") else "dm"}
        if account:
            info["account"] = account
        return info


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
    """gateway 之外（hermes cron run）发送：按 chat_id 前缀选账号，临时建连、发布、断开。"""
    if not ensure_deps():
        return send_error("wms_chat standalone send: paho-mqtt not installed")
    s = load_settings(getattr(pconfig, "extra", {}) or {})
    if not s.accounts:
        return send_error(f"wms_chat standalone send: {ENV_API_KEY} or extra.accounts not set")
    if not chat_id:
        return send_error("wms_chat standalone send: chat_id (wxid or group:<roomid>) required")
    names = [a.name for a in s.accounts]
    account, raw = outbound.split_account(chat_id, names)
    if account is None:
        account = DEFAULT_ACCOUNT if DEFAULT_ACCOUNT in names else (names[0] if len(names) == 1 else None)
    if account is None:
        return send_error(
            f"wms_chat standalone send: prefix chat_id with <account>/ (accounts: {', '.join(sorted(names))})")
    acct: AccountSettings = s.account(account)

    def _run() -> dict:
        cfg = fetch_mqtt_config(acct.api_base_url, acct.api_key)
        # 复用 clientId 会把在线 gateway 挤下线，加后缀避免冲突
        client = new_paho_client(f"{cfg['clientId']}-cron-{uuid.uuid4().hex[:6]}")
        client.username_pw_set(cfg["username"], cfg["password"])
        client.connect(strip_scheme(cfg["host"]), int(cfg["port"]), keepalive=30)
        client.loop_start()
        try:
            payload = outbound.build_payload(raw, [outbound.text_message(message)])
            info = client.publish(cfg["sendTopic"], json.dumps(payload, ensure_ascii=False), qos=1)
            info.wait_for_publish(timeout=10)
            if not info.is_published():
                return send_error("publish not acknowledged")
            return {"success": True, "platform": PLATFORM_NAME, "chat_id": chat_id, "account": account,
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

    check_fn = check_requirements
    if "ensure_deps_fn" not in compat.platform_entry_fields():
        # 旧版 Hermes 没有 ensure_deps_fn：check_fn 只能兼任安装器，否则缺依赖时平台永远不启用
        def check_fn() -> bool:  # noqa: F811
            return paho_importable() or ensure_deps()

    # 只传当前 Hermes 认识的 PlatformEntry 字段；不认识的（如旧版没有 trusted_inbound）会被忽略，
    # 而不是让整个注册失败。
    compat.register_platform(
        ctx,
        **SEND_ENTRY_KWARGS, **DASHBOARD_CARD,
        name=PLATFORM_NAME, label="微秘书 (WMS Chat)", adapter_factory=WmsChatAdapter,
        check_fn=check_fn, ensure_deps_fn=ensure_deps,
        validate_config=is_configured, is_connected=is_configured,
        required_env=[ENV_API_KEY], install_hint="pip install paho-mqtt",
        env_enablement_fn=_env_enablement, cron_deliver_env_var=ENV_HOME_CHANNEL,
        standalone_sender_fn=_standalone_send,
        max_message_length=4000, emoji="💬", allow_update_command=True,
        platform_hint=PLATFORM_HINT,
        # 准入由本插件的 inbound.admit 决定（默认所有好友/所有群），网关不再要求白名单或配对码
        trusted_inbound=True,
        **gateway_env.AUTHZ,
    )
