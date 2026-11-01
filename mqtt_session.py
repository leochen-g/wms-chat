"""paho-mqtt 会话：建连、订阅、发布，断线后指数退避重连（每次重连重新拉取配置）。

paho 在自己的线程里回调；这里把消息通过 ``run_coroutine_threadsafe`` 送回 asyncio 循环。
"""

import asyncio
import contextlib
import logging
import errno
import random
import secrets
import socket
import threading
from typing import Any, Awaitable, Callable, Optional

from .api import strip_scheme

logger = logging.getLogger(__name__)

MAX_BACKOFF_S = 60.0
CONNECT_TIMEOUT_S = 15.0


def paho_importable() -> bool:
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        return False
    return True


def new_paho_client(client_id: str):
    import paho.mqtt.client as mqtt

    # paho-mqtt 2.x 需要显式 callback_api_version；1.x 没有该枚举
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id, clean_session=True)
    return mqtt.Client(client_id=client_id, clean_session=True)


def make_client_id(base: str) -> str:
    """后端给的 clientId + 随机后缀（只含 [A-Za-z0-9-]）。

    broker 对同一 clientId 只保留一条连接：另一个进程、上次没断干净的会话、或同账号的其他客户端
    都会和固定 clientId 互相冲突。每次建连（含重连）换一个新的，就不会再互相踢。
    """
    return f"{base}-{secrets.token_hex(3)}"


def supports_socket_callback() -> bool:
    import paho.mqtt.client as mqtt
    return hasattr(mqtt.Client, "on_socket_open")


# --------------------------------------------------------------------------
# 宿主 socket 层兼容
# --------------------------------------------------------------------------
# paho 建连时固定传 ``source_address=("", 0)``。原生 ``socket.create_connection`` 把它当“不绑定”；
# 但有的 Hermes 版本给整个进程换了一个 IPv6/IPv4 竞速连接层，它会把 ("", 0) 当真实网卡去绑
# （getaddrinfo("", 0) 失败，或绑到 IPv6 通配地址上导致 IPv4 连接报 10051）。
# 下面这个 shim 只在 paho 建连的这一小段时间里，把“空的源地址”丢掉再交给当前的 create_connection。
_SHIM_LOCK = threading.Lock()
_HOST_SOCKET_ERRNOS = frozenset({
    10051,                        # WSAENETUNREACH (Windows)：IPv4 连接被绑到了 IPv6 通配地址
    10060,                        # WSAETIMEDOUT (Windows)
    errno.ENETUNREACH, errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, -5, -2,  # -5/-2: getaddrinfo 的 EAI_NODATA/EAI_NONAME
})


_EMPTY_SOURCES = (("", 0), ("", None))


def _drop_empty_source(real):
    """create_connection 的包装：空的源地址（paho 固定传的 ("", 0)）改成 None。"""

    def _wrapped(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
        if source_address in _EMPTY_SOURCES:
            source_address = None
        return real(address, timeout, source_address, *args, **kwargs)

    _wrapped._wms_shim = True  # type: ignore[attr-defined]
    return _wrapped


class _paho_source_address_shim:
    """with 块内临时包一层 ``socket.create_connection``。

    原生 Python 里 ("", 0) 等于“不绑定”，所以这层对没打补丁的 Hermes 是无操作；
    只有 ``create_connection`` 被替换成竞速实现时才起作用。进程内用锁串行化，避免并发时
    互相恢复到对方的包装。
    """

    def __enter__(self):
        _SHIM_LOCK.acquire()
        self._real = socket.create_connection
        socket.create_connection = _drop_empty_source(self._real)
        return self

    def __exit__(self, *exc):
        socket.create_connection = self._real
        _SHIM_LOCK.release()


def connect_with_shim(client, host: str, port: int, keepalive: int = 60) -> None:
    """``connect_async``，但 paho 拨号那一步（在它自己的网络线程里）走带 shim 的 create_connection。

    paho 没有“传入现成 socket”的公开接口，所以包住它的拨号函数 ``_create_socket_connection``，
    而不是在这里先拨好。包的范围只有这一次拨号，拨完立刻恢复。"""
    original = getattr(client, "_create_socket_connection", None)
    if original is not None:  # paho 内部改名时退化为不加 shim，行为等同原来

        def _patched():
            with _paho_source_address_shim():
                return original()

        client._create_socket_connection = _patched
    client.connect_async(host, port, keepalive=keepalive)


def diagnose_connect_failure(host: str, port: int, timeout: float = 5.0) -> str:
    """TCP 没建立时的自检：用裸 socket 再拨一次，对比 ``socket.create_connection``，指出问题在哪一层。

    只在失败路径上调用，成功路径零开销。返回一句给人看的原因。"""
    raw_err: Optional[BaseException] = None
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return f"DNS resolution failed for {host}: {exc}"
    for family, socktype, proto, _c, sockaddr in infos:
        s = socket.socket(family, socktype, proto)
        s.settimeout(timeout)
        try:
            s.connect(sockaddr)
            raw_err = None
            break
        except OSError as exc:
            raw_err = exc
        finally:
            s.close()
    if raw_err is not None:
        return (f"a plain socket cannot reach {host}:{port} either ({raw_err}) — "
                "real network problem (firewall / proxy / security group / outbound port blocked)")
    # 裸 socket 通了：再用进程里当前的 create_connection（可能被宿主替换过）拨一次
    for src in ((), (("", 0),)):  # 无源地址 / paho 的空源地址
        try:
            conn = socket.create_connection((host, port), timeout, *src)
            conn.close()
        except OSError as exc:
            if src and exc.errno in _HOST_SOCKET_ERRNOS or isinstance(exc, socket.gaierror):
                return ("host socket layer problem: a plain socket reaches the broker, but "
                        f"socket.create_connection fails with an empty source address ({exc!r}). "
                        "Hermes' IPv6/IPv4 racing connect layer mishandles paho's source_address=('', 0); "
                        "this plugin works around it, so if you still see this, update the plugin.")
            return (f"a plain socket reaches {host}:{port} but socket.create_connection fails ({exc!r}) — "
                    "something in this process patches the connect path")
    return (f"TCP to {host}:{port} works from this process, so the failure is above TCP "
            "(TLS / CONNACK) — check the broker's response")


def rc_ok(rc: Any) -> bool:
    return rc == 0 or getattr(rc, "value", None) == 0 or str(rc) == "Success"


class MqttSession:
    def __init__(
        self,
        *,
        fetch_config: Callable[[], dict],
        on_message: Callable[[bytes], Awaitable[None]],
        on_exhausted: Callable[[], Awaitable[None]],
        max_attempts: int,
        base_delay: float,
        label: str = "wms_chat",
    ):
        self._label = label
        self._fetch_config = fetch_config
        self._on_message = on_message
        self._on_exhausted = on_exhausted
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None
        self._cfg: Optional[dict] = None
        self._gen = 0  # 丢弃旧 client 的迟到回调
        self._stopping = False
        self._lost = asyncio.Event()
        self._ready = threading.Event()
        self._tcp_open = threading.Event()  # TCP 已连上（还没收到 CONNACK 时用来区分故障位置）
        self._supervisor: Optional[asyncio.Task] = None

    @property
    def config(self) -> Optional[dict]:
        return self._cfg

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    async def start(self, *, keep_trying: bool = False) -> bool:
        """首次建连。返回是否已连上。

        keep_trying=False：失败直接抛异常。
        keep_trying=True：失败不抛，转入后台指数退避重试（多账号时，一个账号起不来不能拖垮其他账号）。
        """
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        ok = True
        try:
            await self._open()
        except Exception as exc:  # noqa: BLE001
            if not keep_trying:
                raise
            logger.warning("[%s] initial connect failed, will keep retrying: %s", self._label, exc)
            self._close()
            ok = False
        self._supervisor = asyncio.create_task(self._supervise())
        if not ok:
            self._lost.set()
        return ok

    async def stop(self) -> None:
        self._stopping = True
        task, self._supervisor = self._supervisor, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._close()

    async def publish(self, payload: str) -> None:
        client, cfg = self._client, self._cfg
        if client is None or cfg is None or not client.is_connected():
            raise ConnectionError("MQTT not connected")

        def _do() -> None:
            info = client.publish(cfg["sendTopic"], payload, qos=1)
            info.wait_for_publish(timeout=10)
            if not info.is_published():
                raise TimeoutError("publish not acknowledged")

        await self._loop.run_in_executor(None, _do)

    # ---- internals ------------------------------------------------------

    async def _open(self) -> None:
        cfg = await self._loop.run_in_executor(None, self._fetch_config)
        self._gen += 1
        gen = self._gen
        self._cfg = cfg
        self._lost.clear()
        self._ready.clear()
        self._tcp_open.clear()

        host, port = strip_scheme(cfg["host"]), int(cfg["port"])
        client_id = make_client_id(str(cfg["clientId"]))
        client = new_paho_client(client_id)
        client.username_pw_set(cfg["username"], cfg["password"])
        client.on_connect = lambda c, u, f, rc, *a: self._cb_connect(gen, c, rc)
        client.on_message = lambda c, u, m: self._cb_message(gen, m)
        client.on_disconnect = lambda c, u, *a: self._cb_lost(gen, a[0] if a else None)
        client.on_socket_open = lambda c, u, s: self._cb_socket(gen)
        client.on_subscribe = lambda c, u, mid, *a: self._cb_subscribed(gen, a[0] if a else None)
        client.on_connect_fail = lambda c, u: logger.warning("[%s] TCP connect to %s:%s failed", self._label, host, port)
        self._client = client
        logger.info("[%s] connecting to %s:%s as clientId=%s (broker clientId=%s)",
                    self._label, host, port, client_id, cfg["clientId"])
        connect_with_shim(client, host, port, keepalive=60)
        client.loop_start()
        if not await self._loop.run_in_executor(None, self._ready.wait, CONNECT_TIMEOUT_S):
            tcp = self._tcp_open.is_set()
            self._close()
            if not supports_socket_callback():
                where = "no CONNACK"
            elif tcp:
                where = "TCP connected but the broker sent no CONNACK (credentials / clientId rejected silently?)"
            else:
                # TCP 没建立：别直接怪网络，先用裸 socket 自检，分清是宿主 socket 层还是真的网络问题
                where = await self._loop.run_in_executor(None, diagnose_connect_failure, host, port)
            raise ConnectionError(f"MQTT connect timeout {host}:{port} after {CONNECT_TIMEOUT_S:.0f}s: {where}")

    def _close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                client.loop_stop()
                client.disconnect()

    async def _supervise(self) -> None:
        attempts = 0
        while not self._stopping:
            await self._lost.wait()
            if self._stopping:
                return
            self._close()
            while not self._stopping:
                attempts += 1
                if attempts > self._max_attempts:
                    logger.error("[%s] max reconnect attempts reached", self._label)
                    await self._on_exhausted()
                    return
                delay = min(self._base_delay * 2 ** attempts, MAX_BACKOFF_S)
                await asyncio.sleep(delay * random.uniform(0.9, 1.1))
                try:
                    await self._open()
                    attempts = 0
                    logger.info("[%s] reconnected", self._label)
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] reconnect %d failed: %s", self._label, attempts, exc)

    # ---- paho thread callbacks -------------------------------------------

    def _cb_connect(self, gen: int, client, rc) -> None:
        if gen != self._gen:
            return
        if rc_ok(rc):
            logger.info("[%s] CONNACK ok, subscribing to %s", self._label, self._cfg["reciveTopic"])
            client.subscribe(self._cfg["reciveTopic"], qos=1)
            self._ready.set()
        else:
            logger.error("[%s] MQTT connect refused rc=%s", self._label, rc)
            self._cb_lost(gen, rc)

    def _cb_socket(self, gen: int) -> None:
        if gen == self._gen:
            self._tcp_open.set()

    def _cb_subscribed(self, gen: int, granted) -> None:
        if gen == self._gen:
            logger.info("[%s] subscribed to %s (granted=%s)", self._label, (self._cfg or {}).get("reciveTopic"), granted)

    def _cb_lost(self, gen: int, rc: Any = None) -> None:
        if gen == self._gen and self._loop and not self._stopping:
            logger.warning("[%s] MQTT connection lost (rc=%s)", self._label, rc)
            self._loop.call_soon_threadsafe(self._lost.set)

    def _cb_message(self, gen: int, msg) -> None:
        if gen != self._gen or not self._loop:
            return
        if msg.topic != (self._cfg or {}).get("reciveTopic"):
            return
        asyncio.run_coroutine_threadsafe(self._on_message(bytes(msg.payload)), self._loop)
