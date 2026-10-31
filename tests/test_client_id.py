"""clientId 随机后缀 + 连接超时诊断。不依赖 Hermes（直接加载 mqtt_session）。
运行：python tests/test_client_id.py   依赖：paho-mqtt amqtt"""
import faulthandler; faulthandler.dump_traceback_later(90, exit=True)
import asyncio, importlib.util, pathlib, re, socket, sys, types

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
pkg = types.ModuleType("wp"); pkg.__path__ = [str(PLUGIN_DIR)]; sys.modules["wp"] = pkg
def load(name):
    spec = importlib.util.spec_from_file_location(f"wp.{name}", PLUGIN_DIR / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[f"wp.{name}"] = m; spec.loader.exec_module(m); return m
load("api"); ms = load("mqtt_session")

from amqtt.broker import Broker
import paho.mqtt.client as pm

PORT = 18862
CFG = dict(host="127.0.0.1", port=str(PORT), username="u", password="p", clientId="openclaw_BASE",
           reciveTopic="/t/msg", sendTopic="/t/send")

async def main():
    # 1) 后缀格式：基础 id + 6 位十六进制，且每次不同
    ids = {ms.make_client_id("openclaw_BASE") for _ in range(50)}
    assert len(ids) == 50 and all(re.fullmatch(r"openclaw_BASE-[0-9a-f]{6}", i) for i in ids), list(ids)[:3]

    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{PORT}"}},
                     "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]}, "topic-check": {"enabled": False}})
    await broker.start()

    seen = []  # broker 实际收到的 clientId
    spy = pm.Client(pm.CallbackAPIVersion.VERSION1, client_id="spy")
    spy.connect("127.0.0.1", PORT); spy.loop_start()

    async def noop(raw): pass
    def mk(): return ms.MqttSession(fetch_config=lambda: dict(CFG), on_message=noop, on_exhausted=noop,
                                    max_attempts=1, base_delay=0.1, label="t")
    # 2) 两个会话用同一个后端 clientId，同时在线且互不踢下线（固定 clientId 时 broker 会踢掉先到的那个）
    a, b = mk(), mk()
    await a.start(); await b.start()
    await asyncio.sleep(1.5)
    assert a.is_connected and b.is_connected, (a.is_connected, b.is_connected)
    def cid(sess):
        v = sess._client._client_id
        return v.decode() if isinstance(v, bytes) else v
    ca, cb = cid(a), cid(b)
    assert ca != cb and ca.startswith("openclaw_BASE-") and cb.startswith("openclaw_BASE-"), (ca, cb)

    # 3) 对照：不加后缀、同一 clientId 的两个连接确实会互相踢（证明这个问题是真实存在的）
    c1 = pm.Client(pm.CallbackAPIVersion.VERSION1, client_id="same"); c2 = pm.Client(pm.CallbackAPIVersion.VERSION1, client_id="same")
    for c in (c1, c2): c.connect("127.0.0.1", PORT); c.loop_start(); await asyncio.sleep(0.8)
    await asyncio.sleep(1.0)
    kicked = (not c1.is_connected()) or (not c2.is_connected())
    print("same-clientId control on amqtt: one connection was kicked =", kicked, "(informational; many brokers do kick)")
    for cc in (c1, c2):
        cc.loop_stop(); cc.disconnect()

    # 4) 重连也换新的 clientId
    old = cid(a)
    a._base_delay = 0.05
    a._client.disconnect()
    new = old
    for _ in range(75):
        await asyncio.sleep(0.2)
        if a._client is not None and a.is_connected:  # is_connected 是 MqttSession 的属性；重连期间 _client 会短暂为 None
            new = cid(a)
            if new != old: break
    assert new != old and new.startswith("openclaw_BASE-"), (old, new)
    await a.stop(); await b.stop()
    spy.loop_stop()
    await broker.shutdown()

    # 5) 超时诊断：端口根本不通 -> 提示 TCP 没建立
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
    ms.CONNECT_TIMEOUT_S = 2.0
    sess = ms.MqttSession(fetch_config=lambda: dict(CFG, port=str(dead)), on_message=noop, on_exhausted=noop,
                          max_attempts=1, base_delay=0.1, label="t")
    try: await sess.start(); raise SystemExit("should have timed out")
    except ConnectionError as e: assert "TCP connection never established" in str(e), e; print("closed port ->", e)

    # 6) 超时诊断：TCP 通但 broker 不回 CONNACK -> 提示的是另一种原因
    mute = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    mport = mute.sockets[0].getsockname()[1]
    sess = ms.MqttSession(fetch_config=lambda: dict(CFG, port=str(mport)), on_message=noop, on_exhausted=noop,
                          max_attempts=1, base_delay=0.1, label="t")
    try: await sess.start(); raise SystemExit("should have timed out")
    except ConnectionError as e: assert "no CONNACK" in str(e) and "TCP connected" in str(e), e; print("mute broker ->", e)
    mute.close()
    print("CLIENT-ID OK")

asyncio.run(main())
