"""端到端：真实 Hermes 基类 + 真实插件包加载 + 本地 HTTP 配置/媒体服务 + 本地 MQTT broker。
运行：HERMES_HOME=/tmp/h PYTHONPATH=<hermes-agent> python tests/test_e2e.py
依赖：paho-mqtt amqtt（仅测试）"""
import asyncio, importlib.util, json, os, pathlib, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
# 以包的形式加载插件（与 Hermes 的目录插件一致：相对导入可用）
spec = importlib.util.spec_from_file_location(
    "wms_chat_plugin", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
pkg = importlib.util.module_from_spec(spec); sys.modules["wms_chat_plugin"] = pkg; spec.loader.exec_module(pkg)
ad = sys.modules["wms_chat_plugin.adapter"]
inbound = sys.modules["wms_chat_plugin.inbound"]
cfgmod = sys.modules["wms_chat_plugin.config"]

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
assert entry and entry.group_allowed_chats_env == "WMS_CHAT_GROUP_ALLOWED_CHATS"

# 测试服务在回环地址上，Hermes 的 SSRF 防护会拦截；仅在测试里放行（生产路径不变）
import httpx, tools.url_safety as _us
_us.is_safe_url = lambda url: True
_us.create_ssrf_safe_async_client = lambda **kw: httpx.AsyncClient(**kw)

PORT_MQTT, PORT_HTTP = 18841, 18842
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")
CFG = dict(host="127.0.0.1", port=str(PORT_MQTT), username="u", password="p", clientId="bot-1",
           reciveTopic="/im/bot/msg", sendTopic="/im/bot/send", robotId="wxid_bot", userId="1")

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/files/a.png"):
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.end_headers()
            self.wfile.write(PNG); return
        if self.path.startswith("/files/doc.txt"):
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b"hello doc"); return
        ok = "apiKey=KEY123" in self.path and self.path.startswith("/openapi/v1/openclaw/chat/config")
        self.send_response(200 if ok else 401); self.end_headers()
        self.wfile.write(json.dumps(CFG if ok else {}).encode())
    def log_message(self, *a): pass

def S(**kw):
    return cfgmod.load_settings({"api_key": "k", **kw})

def msg(**kw):
    m = dict(content="hi", robotId="wxid_bot", senderId="wxid_u", senderName="Al", messageId="m", isGroup=False,
             timestamp=1700000000000)
    m.update(kw); return m

def unit_tests():
    # 默认：群消息不 @ 也放行
    g = dict(isGroup=True, groupId="r1", groupName="G", isGroupMention=False)
    assert inbound.admit(S(), msg(**g)) is None
    assert inbound.reply_expected(msg(**g)) is False
    assert inbound.reply_expected(msg(**{**g, "isGroupMention": True})) is True
    assert inbound.reply_expected(msg()) is None
    # require_mention=true 才拦截；免 @ 群例外
    assert inbound.admit(S(require_mention="true"), msg(**g)) == "not_mentioned"
    assert inbound.admit(S(require_mention="true", free_response_groups="r1"), msg(**g)) is None
    assert inbound.admit(S(require_mention="true"), msg(**{**g, "content": "@wxid_bot hi"})) is None
    # group_policy / group_allow_from
    assert inbound.admit(S(group_policy="disabled"), msg(**g)) == "group_disabled"
    assert inbound.admit(S(group_policy="allowlist", groups="r2"), msg(**g)) == "group_not_allowlisted"
    assert inbound.admit(S(group_policy="allowlist", groups="r1"), msg(**g)) is None
    assert inbound.admit(S(group_allow_from="bob"), msg(**g)) == "sender_not_allowed"
    assert inbound.admit(S(group_allow_from="AL"), msg(**g)) is None
    assert inbound.admit(S(group_allow_from="*"), msg(**g)) is None
    assert inbound.admit(S(), msg(isMyself=True)) == "self_echo"
    assert inbound.admit(S(), msg(senderId="wxid_bot")) == "self_echo"
    # .env 读取（env 优先于 YAML）
    os.environ["WMS_CHAT_REQUIRE_MENTION"] = "true"; os.environ["WMS_CHAT_GROUPS"] = "a, b"
    os.environ["WMS_CHAT_GROUP_POLICY"] = "allowlist"
    s = cfgmod.load_settings({"api_key": "k", "require_mention": False})
    assert s.require_mention is True and s.groups == {"a", "b"} and s.group_policy == "allowlist"
    for k in ("WMS_CHAT_REQUIRE_MENTION", "WMS_CHAT_GROUPS", "WMS_CHAT_GROUP_POLICY"): os.environ.pop(k)
    # env_enablement：只有 API key 就启用，并带 home channel
    os.environ["WMS_CHAT_API_KEY"] = "KK"; os.environ["WMS_CHAT_HOME_CHANNEL"] = "group:r9"
    seed = ad._env_enablement()
    assert seed["api_key"] == "KK" and seed["home_channel"]["chat_id"] == "group:r9", seed
    for k in ("WMS_CHAT_API_KEY", "WMS_CHAT_HOME_CHANNEL"): os.environ.pop(k)
    assert ad._env_enablement() is None
    # send_targets
    ref = entry.target_ref_syntax_fn
    assert sys.modules["wms_chat_plugin.media"].safe_url_for_log("https://o/a/b.png?sig=SECRET#x") == "https://o/a/b.png"
    assert ref("group:r1|wxid_a,wxid_b") == ("group:r1|wxid_a,wxid_b", None)
    assert ref("wxid_abc") == ("wxid_abc", None) and ref("hello world") is None

async def main():
    unit_tests()
    srv = HTTPServer(("127.0.0.1", PORT_HTTP), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{PORT_MQTT}"}},
                     "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
                     "topic-check": {"enabled": False}})
    await broker.start()
    got_out = []
    spy = pm.Client(pm.CallbackAPIVersion.VERSION1, client_id="spy")
    spy.on_message = lambda c, u, m: got_out.append(json.loads(m.payload))
    spy.connect("127.0.0.1", PORT_MQTT); spy.subscribe(CFG["sendTopic"]); spy.loop_start()

    a = ad.WmsChatAdapter(PlatformConfig(enabled=True, extra={"api_key": "KEY123",
                          "api_base_url": f"http://127.0.0.1:{PORT_HTTP}"}))
    events = []
    async def handler(ev): events.append(ev)
    a.handle_message = handler
    assert await a.connect(), "connect failed"
    assert a.is_connected

    pub = pm.Client(pm.CallbackAPIVersion.VERSION1, client_id="im")
    pub.connect("127.0.0.1", PORT_MQTT); pub.loop_start()
    def send_in(**kw): pub.publish(CFG["reciveTopic"], json.dumps(msg(**kw)), qos=1)
    send_in(content="hello", messageId="m1")
    send_in(content="hello", messageId="m1")  # 重复
    send_in(content="no at", messageId="m2", isGroup=True, groupId="r1", groupName="G", isGroupMention=False)
    send_in(content="with at", messageId="m3", isGroup=True, groupId="r1", groupName="G", isGroupMention=True)
    for _ in range(50):
        if len(events) >= 3: break
        await asyncio.sleep(0.1)
    await asyncio.sleep(0.4)
    assert [e.message_id for e in events] == ["m1", "m2", "m3"], [e.message_id for e in events]
    assert events[0].source.chat_id == "wxid_u" and events[0].source.chat_type == "dm"
    assert events[0].reply_expected is None
    assert events[1].source.chat_id == "group:r1" and events[1].source.chat_type == "group"
    assert events[1].reply_expected is False and events[2].reply_expected is True

    # 媒体：图片 + 文件 -> 本地缓存
    base = f"http://127.0.0.1:{PORT_HTTP}/files"
    send_in(content="【图片消息】\n文件名：a.png", type="图片", url=f"{base}/a.png", messageId="m4")
    send_in(content="【文件消息】\n文件名：doc.txt", type="文件", url=f"{base}/doc.txt", messageId="m5")
    send_in(content="【图片消息】", type="图片", url="data:image/png;base64,AAAA", messageId="m6")
    for _ in range(80):
        if len(events) >= 6: break
        await asyncio.sleep(0.1)
    by_id = {e.message_id: e for e in events}
    if "m4" in by_id:
        e = by_id["m4"]
        print("m4:", e.message_type, e.media_urls, e.media_types, e.media_filenames)
    print("m5:", by_id.get("m5") and (by_id["m5"].message_type, by_id["m5"].media_urls, by_id["m5"].media_filenames))
    print("m6:", by_id.get("m6") and (by_id["m6"].message_type, by_id["m6"].media_urls))
    from gateway.platforms.event import MessageType as MT
    e4, e5, e6 = by_id["m4"], by_id["m5"], by_id["m6"]
    assert e4.message_type == MT.PHOTO and len(e4.media_urls) == 1 and e4.media_types[0] == "image/png", (e4.message_type, e4.media_types)
    assert os.path.isfile(e4.media_urls[0]) and open(e4.media_urls[0], "rb").read() == PNG
    assert "/cache/" in e4.media_urls[0] or "cache" in e4.media_urls[0]
    assert e5.message_type == MT.DOCUMENT and open(e5.media_urls[0], "rb").read() == b"hello doc"
    assert e5.media_filenames == ["doc.txt"], e5.media_filenames
    assert e6.message_type == MT.TEXT and not e6.media_urls  # base64 被忽略，仍派发描述文字
    pub_fail = False

    r = await a.send("wxid_u", "reply"); assert r.success, r.error
    r = await a.send_image("group:r1|wxid_a,wxid_b", "https://x/y.jpg", caption="cap"); assert r.success, r.error
    for _ in range(30):
        if len(got_out) >= 2: break
        await asyncio.sleep(0.1)
    assert got_out[0] == {"isGroup": False, "contactId": "wxid_u", "messages": [{"type": 1, "content": "reply"}]}
    assert got_out[1] == {"isGroup": True, "groupId": "r1", "mentionIds": ["wxid_a", "wxid_b"],
                          "messages": [{"type": 1, "content": "cap"}, {"type": 2, "url": "https://x/y.jpg"}]}

    # 断线 -> 自动重连
    a._session._base_delay = 0.05
    a._session._client.disconnect()
    await asyncio.sleep(0.3)
    for _ in range(80):
        if a._session.is_connected: break
        await asyncio.sleep(0.1)
    assert a._session.is_connected, "did not reconnect"
    n = len(events); send_in(content="after", messageId="m9")
    for _ in range(50):
        if len(events) > n: break
        await asyncio.sleep(0.1)
    assert events[-1].message_id == "m9"

    out = await ad._standalone_send(a.config, "wxid_z", "cron hi"); assert out.get("success"), out
    for _ in range(30):
        if len(got_out) >= 3: break
        await asyncio.sleep(0.1)
    assert got_out[2]["contactId"] == "wxid_z"

    b = ad.WmsChatAdapter(PlatformConfig(enabled=True, extra={"api_key": "BAD",
                          "api_base_url": f"http://127.0.0.1:{PORT_HTTP}"}))
    assert await b.connect() is False and b.has_fatal_error

    await a.disconnect(); assert not a.is_connected
    pub.loop_stop(); spy.loop_stop(); await broker.shutdown(); srv.shutdown()
    print("E2E OK" + ("  (media NOT localized — see above)" if pub_fail else ""))

asyncio.run(main())
