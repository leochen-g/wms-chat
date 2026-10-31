"""入站媒体本地化：OSS URL -> Hermes 媒体缓存（~/.hermes/cache/{images,audio,videos,documents}）。

走 Hermes 自带的 SSRF 防护客户端和大小上限；base64（平台未配置 OSS）一律忽略。
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import unquote, urlparse, urlunparse

from . import compat

logger = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"文件名[:：]\s*(.+)")
# 微秘书消息 type -> (cache default_kind, MessageType 名)
KIND_BY_TYPE = {
    "图片": ("image", "PHOTO"),
    "视频": ("video", "VIDEO"),
    "语音": ("audio", "VOICE"),
    "文件": (None, "DOCUMENT"),
}


def safe_url_for_log(url: str) -> str:
    """去掉 query/fragment（OSS 签名 URL 里有凭据），日志只留 scheme://host/path。"""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except ValueError:
        return "<invalid url>"


@dataclass
class LocalizedMedia:
    path: str
    mime: str
    kind: str
    filename: str


def guess_filename(content: str, url: str) -> str:
    """优先取 content 里的『文件名：xxx』，否则取 URL 路径的 basename。"""
    m = _FILENAME_RE.search(content or "")
    if m:
        return os.path.basename(m.group(1).strip().splitlines()[0])
    try:
        return os.path.basename(unquote(urlparse(url).path)) or ""
    except ValueError:
        return ""


async def localize(url: str, msg_type: str, content: str = "") -> Optional[LocalizedMedia]:
    """下载并缓存；任何失败返回 None（不阻塞文字派发）。"""
    default_kind, _ = KIND_BY_TYPE.get(msg_type, (None, "DOCUMENT"))
    if not compat.url_is_safe(url):
        logger.warning("[wms_chat] blocked unsafe media URL %s", safe_url_for_log(url))
        return None
    try:
        max_bytes = compat.inbound_media_max_bytes()
        chunks: list[bytes] = []
        total = 0
        async with compat.ssrf_safe_client(timeout=30.0, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "HermesAgent/1.0"}) as resp:
                resp.raise_for_status()
                mime = resp.headers.get("content-type", "").split(";")[0].strip()
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit():
                    compat.validate_media_size(int(declared), media_type=msg_type, max_bytes=max_bytes)
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    compat.validate_media_size(total, media_type=msg_type, max_bytes=max_bytes)
                    chunks.append(chunk)
        filename = guess_filename(content, url)
        cached = await compat.cache_media_bytes(
            b"".join(chunks), filename=filename, mime_type=mime, default_kind=default_kind
        )
        if cached is None:
            return None
        return LocalizedMedia(cached.path, cached.media_type, cached.kind, filename or cached.display_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wms_chat] media download failed (%s): %s", safe_url_for_log(url), exc)
        return None
