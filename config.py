# -*- coding: utf-8 -*-
"""VPS 瓦片代理薄配置（仅 env；无 WebGIS Admin DB）。"""

from __future__ import annotations

import os
from typing import Optional


def get_str(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name)
    if value is not None:
        return str(value).strip()
    return "" if default is None else str(default).strip()


def get_int(
    name: str,
    default: int = 0,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    raw = get_str(name, "")
    try:
        value = int(raw) if raw else int(default)
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def get_bool(name: str, default: bool = False) -> bool:
    raw = get_str(name, "").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def get_effective_int(
    name: str,
    default: int = 0,
    *,
    db_key: str = "",
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    return get_int(name, default, minimum=minimum, maximum=maximum)


def get_effective_str(env_key: str, db_key: str = "", default: str = "") -> str:
    return get_str(env_key, default)


# ==================== 瓦片缓存策略（唯一真源） ====================
# 上游各源的 cache-control 天差地别（2026-10-29 实测）：
#   Google 卫星 lyrs=s      → public, max-age=22222222（≈257 天）
#   Google 路网 lyrs=m/y    → public, max-age=3600
#   高德 webrd01/webst01    → max-age=3600
#   纠偏路由（gcj2wgs 等）  → 完全没有
# 与其原样透传，不如统一改写成显式策略：浏览器可撤、边缘长存。
TILE_BROWSER_TTL_SECONDS = get_int(
    "TILE_BROWSER_TTL_SECONDS", 86400, minimum=0, maximum=2592000
)
TILE_EDGE_TTL_SECONDS = get_int(
    "TILE_EDGE_TTL_SECONDS", 604800, minimum=0, maximum=31536000
)


def tile_cache_control() -> str:
    """瓦片响应统一的 Cache-Control 值（max-age=浏览器，s-maxage=CDN 边缘）。"""
    return "public, max-age=%d, s-maxage=%d" % (
        TILE_BROWSER_TTL_SECONDS,
        TILE_EDGE_TTL_SECONDS,
    )
