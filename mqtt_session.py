"""paho-mqtt 会话：建连、订阅、发布，断线后指数退避重连（每次重连重新拉取配置）。

paho 在自己的线程里回调；这里把消息通过 ``run_coroutine_threadsafe`` 送回 asyncio 循环。
"""

import asyncio
import contextlib
import logging
import random
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

        client = new_paho_client(cfg["clientId"])
        client.username_pw_set(cfg["username"], cfg["password"])
        client.on_connect = lambda c, u, f, rc, *a: self._cb_connect(gen, c, rc)
        client.on_message = lambda c, u, m: self._cb_message(gen, m)
        client.on_disconnect = lambda c, u, *a: self._cb_lost(gen)
        self._client = client
        client.connect_async(strip_scheme(cfg["host"]), int(cfg["port"]), keepalive=60)
        client.loop_start()
        if not await self._loop.run_in_executor(None, self._ready.wait, CONNECT_TIMEOUT_S):
            self._close()
            raise ConnectionError(f"MQTT connect timeout {cfg['host']}:{cfg['port']}")

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
            client.subscribe(self._cfg["reciveTopic"], qos=1)
            self._ready.set()
        else:
            logger.error("[%s] MQTT connect refused rc=%s", self._label, rc)
            self._cb_lost(gen)

    def _cb_lost(self, gen: int) -> None:
        if gen == self._gen and self._loop and not self._stopping:
            self._loop.call_soon_threadsafe(self._lost.set)

    def _cb_message(self, gen: int, msg) -> None:
        if gen != self._gen or not self._loop:
            return
        if msg.topic != (self._cfg or {}).get("reciveTopic"):
            return
        asyncio.run_coroutine_threadsafe(self._on_message(bytes(msg.payload)), self._loop)
