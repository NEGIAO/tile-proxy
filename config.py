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
