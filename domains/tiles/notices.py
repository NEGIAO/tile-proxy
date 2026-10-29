# -*- coding: utf-8 -*-
"""统一「提示瓦片」：限流 429 / 缩放级别过大 时返回的占位图。

设计取舍：
- **只用于瓦片请求**（`/proxy/*`）。非瓦片请求仍返回 JSON，API 调用方要的是可解析的错误。
- 图片在进程内**只读一次**并常驻（两张 256×256 PNG，合计 ~70KB），请求时直接返回 bytes，
  零解码开销。
- 状态码分开：限流用 **429**（语义正确，客户端/监控可识别；`<img>` 仍会把它画出来），
  缩放过大用 **200**（确定性结果，可以正常缓存，省掉后续回源）。
- 缓存头分开：429 必须 `no-store`（否则限流图会被边缘缓存 7 天），
  缩放过大可以 `max-age=86400`。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)

# tile-proxy 根目录：domains/tiles/notices.py → 上两级
_NOTICES_DIR = Path(__file__).resolve().parents[2] / "assets" / "notices"

_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

# 文件名（不含扩展名）→ (bytes, media_type)；进程内缓存，读一次
_CACHE: Dict[str, Tuple[bytes, str]] = {}


def _load(name: str) -> Optional[Tuple[bytes, str]]:
    """按名字加载提示图（如 `error1`），找不到返回 None。"""
    cached = _CACHE.get(name)
    if cached is not None:
        return cached
    if not _NOTICES_DIR.is_dir():
        return None
    for suffix, media_type in _MEDIA_TYPES.items():
        path = _NOTICES_DIR / (name + suffix)
        if path.is_file():
            try:
                data = path.read_bytes()
            except OSError as exc:
                logger.warning("提示图读取失败：%s（%s）", path, exc)
                return None
            _CACHE[name] = (data, media_type)
            logger.info("提示图已加载：%s（%d 字节）", path, len(data))
            return _CACHE[name]
    logger.warning("提示图不存在：%s/*", _NOTICES_DIR / name)
    return None


def is_tile_request(request: Request) -> bool:
    """瓦片请求判定：只看路径前缀，避免依赖 Accept（浏览器里 Accept 五花八门）。"""
    return request.url.path.startswith("/proxy/")


def notice_response(
    name: str,
    *,
    status_code: int = 200,
    cache_control: str = "public, max-age=86400",
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    """返回一张提示瓦片；图片缺失时退化为纯文本（不影响主流程）。"""
    loaded = _load(name)
    extra = {"Cache-Control": cache_control, "X-Tile-Notice": name}
    if headers:
        extra.update(headers)
    if loaded is None:
        return Response(
            content="tile-proxy: %s" % name,
            status_code=status_code,
            media_type="text/plain; charset=utf-8",
            headers=extra,
        )
    data, media_type = loaded
    return Response(content=data, status_code=status_code, media_type=media_type, headers=extra)
