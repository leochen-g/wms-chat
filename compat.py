"""Hermes version compatibility layer.

Hermes' plugin surface changes between releases (0.15.x lacks several helpers and ``PlatformEntry``
fields that ``main`` has, and some builds spell fields differently). Everything version-dependent
lives here so the rest of the plugin can be written once:

* shared helpers (``extra_or_secret`` ...) fall back to local implementations;
* ``MessageEvent`` / ``build_source`` are called with only the fields the running Hermes knows;
* media caching falls back to the older ``cache_*_from_bytes`` helpers;
* ``register_platform`` only passes ``PlatformEntry`` fields that exist (renaming
  ``target_ref_syntax_fn`` -> ``parse_target_ref_fn`` where needed), so one unknown keyword can no
  longer make the whole registration fail.
"""

import asyncio
import dataclasses
import inspect
import logging
import mimetypes
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# gateway.platforms._shared
# --------------------------------------------------------------------------
try:
    from gateway.platforms._shared import (  # type: ignore[attr-defined]
        extra_or_secret,
        get_scoped_secret,
        seed_extra_from_env,
        send_error,
    )
except ImportError:  # Hermes releases without the shared helper module

    def get_scoped_secret(name: str, default: Any = None, **_kw: Any) -> Any:
        val = os.environ.get(name)
        return val if val is not None else default

    def extra_or_secret(extra: Optional[dict], key: str, env: str, default: Any = "",
                        *, blank_is_unset: bool = True) -> Any:
        """env var -> ``extra[key]`` -> default. A blank env value is unset; ``False``/``0`` in YAML is real."""
        if env:
            env_value = get_scoped_secret(env, None)
            if env_value is not None and str(env_value).strip():
                return env_value
        value = (extra or {}).get(key)
        if value is None or (blank_is_unset and isinstance(value, str) and not value.strip()):
            return default
        return value

    def seed_extra_from_env(spec: Iterable, *, home_env: Optional[str] = None, home_default: str = "") -> dict:
        seed: dict = {}
        for env, key, conv in spec:
            raw = str(get_scoped_secret(env, "") or "").strip()
            if not raw:
                continue
            try:
                seed[key] = conv(raw) if conv else raw
            except ValueError:
                pass
        if home_env:
            home = str(get_scoped_secret(home_env, "") or "").strip() or home_default
            if home:
                seed["home_channel"] = {"chat_id": home, "name": get_scoped_secret(f"{home_env}_NAME", "Home")}
        return seed

    def send_error(message: Any) -> dict:
        return {"error": str(message)}


# --------------------------------------------------------------------------
# MessageEvent / MessageType / BasePlatformAdapter helpers
# --------------------------------------------------------------------------
try:
    from gateway.platforms.event import MessageEvent, MessageType
except ImportError:  # older layout: defined in base.py
    from gateway.platforms.base import MessageEvent, MessageType  # type: ignore[no-redef]

_EVENT_FIELDS = {f.name for f in dataclasses.fields(MessageEvent)}


def make_event(**kwargs: Any) -> Any:
    """Build a MessageEvent with only the fields this Hermes knows (newer ones, e.g. ``reply_expected``,
    are dropped on older builds instead of raising TypeError)."""
    return MessageEvent(**{k: v for k, v in kwargs.items() if k in _EVENT_FIELDS})


def event_supports(field: str) -> bool:
    return field in _EVENT_FIELDS


def build_source(adapter: Any, **kwargs: Any) -> Any:
    accepted = set(inspect.signature(adapter.build_source).parameters)
    return adapter.build_source(**{k: v for k, v in kwargs.items() if k in accepted})


def wire_plugin_handlers(adapter: Any) -> None:
    fn = getattr(adapter, "_wire_plugin_handlers", None)
    if callable(fn):
        fn(None)


# --------------------------------------------------------------------------
# Media caching
# --------------------------------------------------------------------------
@dataclass
class CachedMedia:
    path: str
    media_type: str
    kind: str  # image | video | audio | document
    display_name: str


_FALLBACK_MAX_BYTES = 50 * 1024 * 1024
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".amr", ".silk", ".flac"}


def inbound_media_max_bytes() -> int:
    try:
        from gateway.platforms.base import get_inbound_media_max_bytes  # type: ignore[attr-defined]
        return int(get_inbound_media_max_bytes())
    except ImportError:
        return _FALLBACK_MAX_BYTES


def validate_media_size(size: int, *, media_type: str = "media", max_bytes: Optional[int] = None) -> None:
    limit = inbound_media_max_bytes() if max_bytes is None else max_bytes
    if limit and size > limit:
        raise ValueError(f"Inbound {media_type} payload is too large ({size} bytes > {limit} bytes)")


def _classify(ext: str, mime: str, default_kind: Optional[str]) -> str:
    mime = (mime or "").lower()
    for kind, exts in (("image", _IMAGE_EXT), ("video", _VIDEO_EXT), ("audio", _AUDIO_EXT)):
        if mime.startswith(f"{kind}/") or ext in exts or default_kind == kind:
            return kind
    return "document"


async def cache_media_bytes(data: bytes, *, filename: str = "", mime_type: str = "",
                            default_kind: Optional[str] = None) -> Optional[CachedMedia]:
    """Classify and cache attachment bytes in Hermes' media cache. None = an "image" that is not an image."""
    try:
        from gateway.platforms.base import cache_media_bytes_async  # type: ignore[attr-defined]
    except ImportError:
        cache_media_bytes_async = None
    if cache_media_bytes_async is not None:
        return await cache_media_bytes_async(
            data, filename=filename, mime_type=mime_type, default_kind=default_kind)

    from gateway.platforms import base

    ext = os.path.splitext(filename)[1].lower() if filename else ""
    if not ext and mime_type:
        ext = mimetypes.guess_extension(mime_type.split(";")[0].strip()) or ""
    kind = _classify(ext, mime_type, default_kind)
    display = filename or (ext.lstrip(".") or "file")

    def _do() -> Optional[CachedMedia]:
        if kind == "image":
            try:
                path = base.cache_image_from_bytes(data, ext if ext in _IMAGE_EXT else ".jpg")
            except ValueError:
                return None
            return CachedMedia(path, mime_type if mime_type.startswith("image/") else "image/jpeg", kind, display)
        if kind == "video":
            path = base.cache_video_from_bytes(data, ext if ext in _VIDEO_EXT else ".mp4")
            return CachedMedia(path, mime_type if mime_type.startswith("video/") else "video/mp4", kind, display)
        if kind == "audio":
            path = base.cache_audio_from_bytes(data, ext if ext in _AUDIO_EXT else ".ogg")
            return CachedMedia(path, mime_type if mime_type.startswith("audio/") else "audio/ogg", kind, display)
        name = filename or (f"document{ext}" if ext else "document.bin")
        path = base.cache_document_from_bytes(data, name)
        mt = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        return CachedMedia(path, mt, "document", display or name)

    return await asyncio.to_thread(_do)


def url_is_safe(url: str) -> bool:
    from tools import url_safety
    return bool(url_safety.is_safe_url(url))


def ssrf_safe_client(**kwargs: Any) -> Any:
    """httpx.AsyncClient that refuses private/internal targets. Newer Hermes validates at connect time;
    older builds only expose ``is_safe_url``, so there we check the URL up front and every redirect hop."""
    from tools import url_safety
    factory = getattr(url_safety, "create_ssrf_safe_async_client", None)
    if factory is not None:
        return factory(**kwargs)
    import httpx

    async def _guard(response: Any) -> None:
        if response.has_redirect_location:
            target = str(response.url.join(response.headers["location"]))
            if not url_safety.is_safe_url(target):
                raise ValueError("Blocked redirect to private/internal address")

    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [*hooks.get("response", []), _guard]
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)


# --------------------------------------------------------------------------
# Platform registration
# --------------------------------------------------------------------------
def platform_entry_fields() -> set[str]:
    from gateway.platform_registry import PlatformEntry
    return {f.name for f in dataclasses.fields(PlatformEntry)}


def supports_trusted_inbound() -> bool:
    return "trusted_inbound" in platform_entry_fields()


def register_platform(ctx: Any, **kwargs: Any) -> None:
    """``ctx.register_platform`` with only the keywords this Hermes' ``PlatformEntry`` knows."""
    fields = platform_entry_fields()
    # Same hook, spelled differently across builds.
    if "target_ref_syntax_fn" in kwargs and "target_ref_syntax_fn" not in fields:
        fn = kwargs.pop("target_ref_syntax_fn")
        if "parse_target_ref_fn" in fields:
            kwargs.setdefault("parse_target_ref_fn", fn)
    dropped = sorted(k for k in kwargs if k not in fields)
    if dropped:
        logger.debug("[wms_chat] this Hermes does not support PlatformEntry fields: %s (ignored)", ", ".join(dropped))
    ctx.register_platform(**{k: v for k, v in kwargs.items() if k in fields})
