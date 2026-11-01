"""宿主 socket 层兼容：Hermes 的 IPv6/IPv4 竞速连接层会把 paho 传的 source_address=("", 0) 当真实网卡去绑。

用 Hermes 源码里的真实竞速实现（HERMES_SRC 指向 hermes-agent 源码目录）复现，再验证 shim 能绕过。
没有 Hermes 源码时，用等价的最小竞速实现（对空源地址同样调用 getaddrinfo 后 bind）代替。
运行：[HERMES_SRC=<hermes-agent>] python tests/test_host_socket.py     依赖：paho-mqtt amqtt"""
import faulthandler; faulthandler.dump_traceback_later(90, exit=True)
import asyncio, importlib.util, os, pathlib, socket, sys, types

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
pkg = types.ModuleType("wp"); pkg.__path__ = [str(PLUGIN_DIR)]; sys.modules["wp"] = pkg
def load(name):
    spec = importlib.util.spec_from_file_location(f"wp.{name}", PLUGIN_DIR / f"{name}.py")
    m = importlib.util.module_from_spec(spec); sys.modules[f"wp.{name}"] = m; spec.loader.exec_module(m); return m
load("api"); ms = load("mqtt_session")

NATIVE = socket.create_connection          # 在安装竞速层之前留住原生实现

def install_host_racer():
    src = os.environ.get("HERMES_SRC")
    if src and (pathlib.Path(src) / "hermes_bootstrap.py").exists():
        sys.path.insert(0, src)
        import hermes_bootstrap as hb
        racer = hb._happy_eyeballs_create_connection
        label = "Hermes' real racer"
    else:
        def racer(address, timeout, source_address=None, socket_options=()):
            host, port = address; last = None
            for fam, st, pr, _c, sa in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                s = socket.socket(fam, st, pr)
                try:
                    if source_address is not None:   # 把 ("", 0) 当真实网卡：getaddrinfo("", 0) 会抛 gaierror
                        s.bind(socket.getaddrinfo(source_address[0], source_address[1], family=fam, type=st)[0][4])
                    s.settimeout(timeout); s.connect(sa); return s
                except OSError as e: s.close(); last = e
            raise last
        label = "built-in stand-in racer"
    def patched(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, all_errors=False):
        eff = socket.getdefaulttimeout() if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        return racer(address, eff, source_address=source_address)
    socket.create_connection = patched
    return label

from amqtt.broker import Broker
PORT = 18871
CFG = dict(host="127.0.0.1", port=str(PORT), username="u", password="p", clientId="B", reciveTopic="/t/m", sendTopic="/t/s")
async def noop(raw): pass

async def main():
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{PORT}"}},
                     "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]}, "topic-check": {"enabled": False}})
    await broker.start()
    mk = lambda: ms.MqttSession(fetch_config=lambda: dict(CFG), on_message=noop, on_exhausted=noop, max_attempts=1, base_delay=0.1, label="t")

    # 1) 原生 Python：行为不变
    s = mk(); await s.start(); assert s.is_connected; await s.stop()
    print("native socket layer          -> connected")

    label = install_host_racer(); print("installed:", label)

    # 2) 对照：竞速层 + 不带 shim 的 paho，确实连不上（复现用户的现象）
    import paho.mqtt.client as pm
    c = ms.new_paho_client("raw-1"); c.username_pw_set("u", "p")
    got = []; c.on_connect = lambda *a: got.append(a[3] if len(a) > 3 else None)
    c.connect_async("127.0.0.1", PORT); c.loop_start(); await asyncio.sleep(2.5); c.loop_stop()
    assert not got, f"unpatched paho unexpectedly connected under the racer: {got}"
    print("racer + plain paho           -> NOT connected (reproduces the bug)")

    # 3) 竞速层 + 插件的会话（带 shim）：连上、订阅
    s = mk(); await s.start(); assert s.is_connected, "shim did not work around the racer"
    print("racer + plugin session(shim) -> connected")
    # 4) shim 只在拨号那一刻生效，之后恢复，不污染进程里的其他代码
    assert not getattr(socket.create_connection, "_wms_shim", False), "shim leaked after connect"
    print("shim restored after dial     -> ok")
    await s.stop()

    # 5) 自检：宿主 socket 层有问题时，提示的是“宿主 socket 层”，而不是 DNS/防火墙/代理
    msg = ms.diagnose_connect_failure("127.0.0.1", PORT)
    assert "host socket layer problem" in msg, msg
    print("diagnosis (racer, reachable) ->", msg[:110], "...")

    # 6) 自检：真的连不通时，提示的是网络
    s2 = socket.socket(); s2.bind(("127.0.0.1", 0)); dead = s2.getsockname()[1]; s2.close()
    msg = ms.diagnose_connect_failure("127.0.0.1", dead, timeout=1.0)
    assert "plain socket cannot reach" in msg and "real network problem" in msg, msg
    print("diagnosis (closed port)      ->", msg[:110], "...")

    # 7) 自检：裸 socket 与 create_connection 都通 -> 说明问题在 TCP 之上
    socket.create_connection = NATIVE
    msg = ms.diagnose_connect_failure("127.0.0.1", PORT)
    assert "above TCP" in msg, msg
    print("diagnosis (all healthy)      ->", msg[:110], "...")

    await broker.shutdown(); print("HOST-SOCKET OK")

asyncio.run(main())
