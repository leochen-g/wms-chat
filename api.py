"""微秘书后端：获取 MQTT 连接参数。"""

import json
import urllib.parse
import urllib.request
from typing import Any

CONFIG_PATH = "/openapi/v1/openclaw/chat/config"
REQUIRED_FIELDS = ("host", "port", "username", "password", "clientId", "reciveTopic", "sendTopic")


def fetch_mqtt_config(api_base_url: str, api_key: str, timeout: float = 10.0) -> dict[str, Any]:
    """同步 GET；调用方在 executor 中运行。校验必填字段。"""
    url = f"{api_base_url.rstrip('/')}{CONFIG_PATH}?apiKey={urllib.parse.quote(api_key, safe='')}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-configured URL)
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config response is not an object")
    # 后端的业务错误也是 HTTP 200：{"code":403,"msg":"请检查apiKey是否填写错误"} / {"error":"..."}
    if "host" not in data and (data.get("msg") or data.get("error") or data.get("message")):
        detail = data.get("msg") or data.get("error") or data.get("message")
        code = f" (code {data['code']})" if "code" in data else ""
        raise ValueError(f"WMS backend rejected the request{code}: {str(detail)[:200]}")
    for key in REQUIRED_FIELDS:
        if not data.get(key):
            raise ValueError(f'missing field "{key}" in config response')
    return data


def strip_scheme(host: str) -> str:
    for scheme in ("mqtt://", "tcp://"):
        if host.startswith(scheme):
            return host[len(scheme):]
    return host
