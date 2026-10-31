"""多账号：一个 adapter、多个微秘书账号、共用一个 agent。

两个账号各有独立的 MQTT topic（模拟微秘书给每个账号分配不同的 reciveTopic/sendTopic）。
运行：HERMES_HOME=/tmp/h PYTHONPATH=<hermes-agent> python tests/test_multi_account.py"""
import asyncio, importlib.util, json, pathlib, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "wms_chat_plugin", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
pkg = importlib.util.module_from_spec(spec); sys.modules["wms_chat_plugin"] = pkg; spec.loader.exec_module(pkg)
ad = sys.modules["wms_chat_plugin.adapter"]
cfgmod = sys.modules["wms_chat_plugin.config"]
outbound = sys.modules["wms_chat_plugin.outbound"]
inbound = sys.modules["wms_chat_plugin.inbound"]

import paho.mqtt.client as pm
from amqtt.broker import Broker
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry, PlatformEntry

class _Ctx:
    def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None,
                          required_env=None, install_hint="", **kw):
        platform_registry.register(PlatformEntry(name=name, label=label, adapter_factory=adapter_factory,
            check_fn=check_fn, validate_config=validate_config, required_env=required_env or [],
            install_hint=install_hint, source="plugin", **kw))
pkg.register(_Ctx())
entry = platform_registry.get("wms_chat")

PORT_MQTT, PORT_HTTP = 18851, 18852
# apiKey -> 该账号的 MQTT 参数（clientId / topic 各不相同）
CFGS = {
    "KEY_A": dict(host="127.0.0.1", port=str(PORT_MQTT), username="u", password="p", clientId="bot-a",
                  reciveTopic="/im/a/msg", sendTopic="/im/a/send", robotId="wxid_bot_a", userId="1"),
    "KEY_B": dict(host="127.0.0.1", port=str(PORT_MQTT), username="u", password="p", clientId="bot-b",
                  reciveTopic="/im/b/msg", sendTopic="/im/b/send", robotId="wxid_bot_b", userId="2"),
}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        key = self.path.split("apiKey=")[-1].split("&")[0] if "apiKey=" in self.path else ""
        cfg = CFGS.get(key)
        self.send_response(200 if cfg else 401); self.end_headers()
        self.wfile.write(json.dumps(cfg or {}).encode())
    def log_message(self, *a): pass

BASE = f"http://127.0.0.1:{PORT_HTTP}"

def msg(robot, **kw):
    m = dict(content="hi", robotId=robot, senderId="wxid_u", senderName="Al", messageId="m", isGroup=False,
             timestamp=1700000000000)
    m.update(kw); return m

def unit_tests():
    # ---- 配置解析 ----
    s = cfgmod.load_settings({"api_key": "KEY_A", "accounts": [
        {"name": "shop", "api_key": "KEY_B", "allowed_users": "wxid_boss", "require_mention": True},
        {"name": "bad name!", "api_key": "K3"},                   # 非法名
        {"name": "dup", "api_key": "KEY_A"},                      # 复用 key
        {"name": "shop", "api_key": "K5"},                        # 重名
        {"name": "nokey"},                                        # 缺 key
        "oops",                                                   # 不是映射
    ]})
    assert [a.name for a in s.accounts] == ["default", "shop"], [a.name for a in s.accounts]
    assert len(s.problems) == 5, s.problems
    d, sh = s.accounts
    assert d.allowed_users == frozenset() and sh.allowed_users == frozenset({"wxid_boss"})
    assert d.require_mention is False and sh.require_mention is True  # 逐账号覆盖，互不影响
    # 没有 default：只有 accounts 列表
    s2 = cfgmod.load_settings({"accounts": [{"name": "a", "api_key": "x"}, {"name": "b", "api_key": "y"}]})
    assert [a.name for a in s2.accounts] == ["a", "b"]
    assert cfgmod.is_configured(PlatformConfig(enabled=True, extra={"accounts": [{"name": "a", "api_key": "x"}]}))
    assert not cfgmod.is_configured(PlatformConfig(enabled=True, extra={"accounts": [{"name": "a"}]}))
    assert not cfgmod.is_configured(PlatformConfig(enabled=True, extra={}))
    # 账号继承全局；账号里显式写空列表 = 该账号不限制（覆盖全局）
    s3 = cfgmod.load_settings({"allowed_users": "wxid_g", "accounts": [
        {"name": "a", "api_key": "x"}, {"name": "b", "api_key": "y", "allowed_users": []}]})
    a3, b3 = s3.accounts
    assert a3.allowed_users == frozenset({"wxid_g"}), a3.allowed_users
    assert b3.allowed_users == frozenset(), b3.allowed_users
    # ---- chat_id 前缀 ----
    assert outbound.prefix_chat_id("default", "wxid_a") == "wxid_a"
    assert outbound.prefix_chat_id("shop", "group:r1") == "shop/group:r1"
    names = {"default", "shop"}
    assert outbound.split_account("shop/wxid_a", names) == ("shop", "wxid_a")
    assert outbound.split_account("shop/group:r1|wxid_a,wxid_b", names) == ("shop", "group:r1|wxid_a,wxid_b")
    assert outbound.split_account("wxid_a", names) == (None, "wxid_a")
    assert outbound.split_account("nope/wxid_a", names) == (None, "nope/wxid_a")  # 未知前缀不当账号
    assert outbound.split_account("wms_chat:shop/wxid_a", names) == ("shop", "wxid_a")
    assert inbound.chat_identity(msg("r", isGroup=True, groupId="g1", groupName="G"), "shop")[0] == "shop/group:g1"
    assert inbound.chat_identity(msg("r"), "default")[0] == "wxid_u"
    # ---- send_message 目标语法 ----
    ref = entry.target_ref_syntax_fn
    assert ref("shop/wxid_abc") == ("shop/wxid_abc", None)
    assert ref("shop/group:r1|wxid_a,wxid_b") == ("shop/group:r1|wxid_a,wxid_b", None)
    assert ref("group:r1") == ("group:r1", None) and ref("wxid_abc") == ("wxid_abc", None)
    assert ref("shop/hello") is None and ref("a/b/wxid_x") is None

async def wait(pred, n=60, dt=0.1):
    for _ in range(n):
        if pred(): return True
        await asyncio.sleep(dt)
    return pred()

async def main():
    unit_tests()
    srv = HTTPServer(("127.0.0.1", PORT_HTTP), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{PORT_MQTT}"}},
                     "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
                     "topic-check": {"enabled": False}})
    await broker.start()

    out = {"a": [], "b": []}
    spy = pm.Client(pm.CallbackAPIVersion.VERSION1, client_id="spy")
    spy.on_message = lambda c, u, m: out["a" if m.topic.endswith("/a/send") else "b"].append(json.loads(m.payload))
    spy.connect("127.0.0.1", PORT_MQTT); spy.subscribe("/im/a/send"); spy.subscribe("/im/b/send"); spy.loop_start()
    pub = pm.Client(pm.CallbackAPIVersion.VERSION1, client_id="im")
    pub.connect("127.0.0.1", PORT_MQTT); pub.loop_start()
    def send_in(acct, **kw):
        cfg = CFGS["KEY_A" if acct == "a" else "KEY_B"]
        pub.publish(cfg["reciveTopic"], json.dumps(msg(cfg["robotId"], **kw)), qos=1)

    extra = {"api_base_url": BASE, "api_key": "KEY_A",
             "accounts": [{"name": "shop", "api_key": "KEY_B", "allowed_users": "wxid_boss"}]}
    a = ad.WmsChatAdapter(PlatformConfig(enabled=True, extra=extra))
    events = []
    async def handler(ev): events.append(ev)
    a.handle_message = handler
    assert await a.connect(), "connect failed"
    assert set(a._sessions) == {"default", "shop"} and all(s.is_connected for s in a._sessions.values())

    # ---- 入站：同一个好友、同一个群，同时被两个账号看到 -> 会话分开 ----
    send_in("a", content="from A", messageId="x1")                                  # default 账号，wxid_u 私聊
    send_in("b", content="from B", messageId="x1", senderId="wxid_boss")             # shop 账号（messageId 相同！）
    send_in("a", content="grp A", messageId="g1", isGroup=True, groupId="room1", groupName="R")
    send_in("b", content="grp B", messageId="g1", isGroup=True, groupId="room1", groupName="R", senderId="wxid_boss")
    assert await wait(lambda: len(events) >= 4), [(e.message_id, e.source.chat_id) for e in events]
    ids = {(e.metadata["wms_account"], e.source.chat_id) for e in events}
    assert ids == {("default", "wxid_u"), ("shop", "shop/wxid_boss"),
                   ("default", "group:room1"), ("shop", "shop/group:room1")}, ids
    # 同 messageId 来自两个账号：去重按账号隔离，两条都处理
    assert sum(1 for e in events if e.message_id == "x1") == 2

    # ---- 按账号的访问控制：shop 只允许 wxid_boss ----
    n = len(events)
    send_in("b", content="stranger to shop", messageId="s1", senderId="wxid_stranger")   # 被 shop 拦
    send_in("a", content="stranger to default", messageId="s2", senderId="wxid_stranger")  # default 不限
    assert await wait(lambda: len(events) >= n + 1)
    await asyncio.sleep(0.4)
    new = events[n:]
    assert [e.message_id for e in new] == ["s2"] and new[0].metadata["wms_account"] == "default", \
        [(e.message_id, e.metadata) for e in new]

    # ---- 同一条消息重复投递：同账号去重 ----
    n = len(events)
    send_in("a", content="dup", messageId="d1"); send_in("a", content="dup", messageId="d1")
    await asyncio.sleep(0.8)
    assert len(events) == n + 1

    # ---- 出站路由：按 chat_id 前缀选账号 ----
    r = await a.send("wxid_u", "reply to default"); assert r.success, r.error
    r = await a.send("shop/wxid_boss", "reply to shop"); assert r.success, r.error
    r = await a.send_image("shop/group:room1|wxid_boss", "https://x/y.jpg", caption="c"); assert r.success, r.error
    assert await wait(lambda: len(out["a"]) >= 1 and len(out["b"]) >= 2)
    assert out["a"] == [{"isGroup": False, "contactId": "wxid_u", "messages": [{"type": 1, "content": "reply to default"}]}]
    assert out["b"][0] == {"isGroup": False, "contactId": "wxid_boss", "messages": [{"type": 1, "content": "reply to shop"}]}
    assert out["b"][1] == {"isGroup": True, "groupId": "room1", "mentionIds": ["wxid_boss"],
                           "messages": [{"type": 1, "content": "c"}, {"type": 2, "url": "https://x/y.jpg"}]}
    assert len(out["a"]) == 1, "shop 的回复不能漏到 default 账号"

    # ---- 一个账号断线重连，不影响另一个 ----
    shop = a._sessions["shop"]; shop._base_delay = 0.05
    shop._client.disconnect()
    r = await a.send("wxid_u", "default still works"); assert r.success, r.error   # default 不受影响
    assert await wait(lambda: shop.is_connected, n=80), "shop did not reconnect"
    n = len(events); send_in("b", content="after", messageId="r1", senderId="wxid_boss")
    assert await wait(lambda: len(events) > n) and events[-1].message_id == "r1"

    # ---- standalone（cron）：按前缀选账号 ----
    o = await ad._standalone_send(a.config, "shop/wxid_cron", "cron to shop"); assert o.get("success") and o["account"] == "shop", o
    o = await ad._standalone_send(a.config, "wxid_cron", "cron to default"); assert o.get("success") and o["account"] == "default", o
    assert await wait(lambda: any(m.get("contactId") == "wxid_cron" for m in out["b"]) and
                              any(m.get("contactId") == "wxid_cron" for m in out["a"]))
    # 没有 default、多账号、无前缀 -> 明确报错而不是乱选
    amb = PlatformConfig(enabled=True, extra={"api_base_url": BASE, "accounts": [
        {"name": "p", "api_key": "KEY_A"}, {"name": "q", "api_key": "KEY_B"}]})
    o = await ad._standalone_send(amb, "wxid_x", "hi")
    assert "error" in o and "prefix" in json.dumps(o), o

    await a.disconnect(); assert not a.is_connected
    # 跨进程锁：另一个进程占着 KEY_A 的锁 -> 该账号被跳过，KEY_B 的账号照常连接
    import hashlib, os, subprocess
    ident = hashlib.sha256(b"KEY_A").hexdigest()[:16]
    holder = subprocess.Popen([sys.executable, "-c",
        "import sys,time; from gateway.status import acquire_scoped_lock as a;"
        f"ok,_=a('wms_chat','{ident}',metadata={{'owner_kind':'process'}}); print('HELD' if ok else 'FAIL',flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, env=os.environ.copy())
    try:
        assert holder.stdout.readline().strip() == "HELD", "helper process could not take the lock"
        locked = ad.WmsChatAdapter(PlatformConfig(enabled=True, extra={"api_base_url": BASE, "accounts": [
            {"name": "p", "api_key": "KEY_A"}, {"name": "q", "api_key": "KEY_B"}]}))
        locked.handle_message = handler
        assert await locked.connect(), "account q should still connect"
        assert set(locked._sessions) == {"q"}, f"p must be skipped while another process holds its key: {set(locked._sessions)}"
        await locked.disconnect()
        only_p = ad.WmsChatAdapter(PlatformConfig(enabled=True, extra={"api_base_url": BASE, "accounts": [
            {"name": "p", "api_key": "KEY_A"}]}))
        only_p.handle_message = handler
        assert await only_p.connect() is False and only_p.has_fatal_error, "all accounts locked -> connect must fail"
    finally:
        holder.kill(); holder.wait()

    # 断开后锁已释放：同样的 key 可以重新连接
    again = ad.WmsChatAdapter(PlatformConfig(enabled=True, extra={"api_base_url": BASE, "accounts": [
        {"name": "p", "api_key": "KEY_A"}, {"name": "q", "api_key": "KEY_B"}]}))
    again.handle_message = handler
    assert await again.connect(), "locks were not released on disconnect"
    # 无前缀、没有 default、多账号 -> 发送被拒绝（不乱选账号）
    r = await again.send("wxid_u", "ambiguous")
    assert not r.success and "prefix" in r.error and not r.retryable, r
    r = await again.send("q/wxid_u", "explicit"); assert r.success, r.error
    await again.disconnect()

    # 坏账号不拖垮好账号：一个 key 错误，另一个正常
    mixed = ad.WmsChatAdapter(PlatformConfig(enabled=True, extra={"api_base_url": BASE, "accounts": [
        {"name": "ok", "api_key": "KEY_A"}, {"name": "bad", "api_key": "NOPE"}]}))
    mixed.handle_message = handler
    assert await mixed.connect(), "one bad account must not fail the others"
    assert mixed._sessions["ok"].is_connected and not mixed._sessions["bad"].is_connected
    await mixed.disconnect()

    pub.loop_stop(); spy.loop_stop(); await broker.shutdown(); srv.shutdown()
    print("MULTI-ACCOUNT OK")

asyncio.run(main())
