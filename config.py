# -*- coding: utf-8 -*-
"""VPS 瓦片代理配置：环境变量 + 管理页覆盖（SQLite）。

优先级：**管理页保存的值（SQLite）> 环境变量 > 代码默认**。

- `get_str/get_int/get_bool` 每次都会先问一次数据库（`admin_store` 里有 1 秒缓存，
  所以热路径上读也不心疼），因此**标了 `hot=True` 的项改完立即生效**；
- 启动时读成模块常量、或用来创建连接池/缓存的项（`hot=False`），改完要重启服务；
- `RUNTIME_SETTINGS` 同时是「管理页渲染 + 后端校验」的唯一真源：加一项配置只要往表里
  加一条，页面会自动出现对应控件，PUT 也会自动按类型/范围校验。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from admin_store import (
    all_settings,
    db_path as _admin_db_path,
    get_setting,
    set_settings,
    updated_at as _settings_updated_at,
)

# ==================== 基础读取（env + 管理页覆盖） ====================

def get_str(name: str, default: Optional[str] = None) -> str:
    """取配置：管理页覆盖 > 环境变量 > 默认值。"""
    override = get_setting(name, None)
    if override is not None:
        return str(override).strip()
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


# ==================== 配置项清单（管理页 + 校验的唯一真源） ====================
# 字段说明：
#   key     环境变量名（也是数据库里的键名，便于「env 改了再重启」与页面值对齐）
#   type    bool / int / enum / str
#   hot     True = 改完立即生效；False = 重启服务后生效
#   group   页面分组
RUNTIME_SETTINGS: Dict[str, Dict[str, Any]] = {}


def _item(key: str, label: str, group: str, kind: str, default: str, *,
          help: str = "", hot: bool = False, unit: str = "",
          minimum: Optional[int] = None, maximum: Optional[int] = None,
          options: Optional[List[str]] = None, maxlen: Optional[int] = None) -> None:
    RUNTIME_SETTINGS[key] = {
        "label": label, "group": group, "type": kind, "default": default,
        "help": help, "hot": hot, "unit": unit,
        "minimum": minimum, "maximum": maximum,
        "options": options, "maxlen": maxlen,
    }


# ---- 服务开关 ----
_item("PROXY_ENABLED", "瓦片代理总开关", "服务开关", "bool", "1", hot=True,
      help="关闭后 /proxy/* 一律返回 503（带提示头），不再请求上游、也不做纠偏。")
_item("PROXY_RATE_LIMIT_ENABLED", "限流开关", "服务开关", "bool", "1", hot=True,
      help="打开后按下面的「限流速率」限流；关闭等价于不限流。")
_item("PROXY_RATE_LIMIT", "限流速率", "服务开关", "int", "0", hot=True,
      unit="次/分/IP", minimum=0, maximum=100000,
      help="每个 IP 每分钟允许的请求数。一屏地图约 20~30 片，公网建议 ≥120；0 = 不限。")
_item("PROXY_RATE_LIMIT_MODE", "限流响应形态", "服务开关", "enum", "image-203", hot=True,
      options=["image-203", "image-200", "json-429"],
      help="image-203：提示图 + 203（默认，能渲染且天然不落缓存）；"
           "image-200：提示图 + 200（Cesium 只认 200）；json-429：纯 JSON。")

# ---- 缓存 ----
_item("TILE_BROWSER_TTL_SECONDS", "浏览器缓存", "缓存", "int", "86400", hot=True,
      unit="秒", minimum=0, maximum=2592000,
      help="回给浏览器的 max-age。出问题时调小，坏图能更快自愈。")
_item("TILE_EDGE_TTL_SECONDS", "边缘缓存", "缓存", "int", "604800", hot=True,
      unit="秒", minimum=0, maximum=31536000,
      help="回给 CDN / nginx 的 s-maxage，默认 7 天。")
_item("PROXY_TILE_CACHE_MAX_SIZE", "进程内缓存条数", "缓存", "int", "3000",
      unit="条", minimum=0, maximum=100000,
      help="直通瓦片的内存缓存条数，0 = 关闭内存缓存。")
_item("PROXY_TILE_CACHE_TTL_SECONDS", "进程内缓存时长", "缓存", "int", "300",
      unit="秒", minimum=0, maximum=86400,
      help="内存缓存条目的存活时间。")
_item("GCJRE_CACHE_MAX_MB", "磁盘缓存上限", "缓存", "int", "512",
      unit="MB", minimum=0, maximum=65536,
      help="纠偏源片/输出瓦片的磁盘缓存上限。")
_item("GCJRE_CACHE_MAX_AGE_DAYS", "磁盘缓存天数", "缓存", "int", "7",
      unit="天", minimum=1, maximum=365,
      help="超过这个天数的磁盘缓存会被清理任务删除。")
_item("GCJRE_CACHE_CLEANUP_INTERVAL_S", "清理间隔", "缓存", "int", "3600",
      unit="秒", minimum=60, maximum=86400,
      help="磁盘缓存清理任务的运行间隔。")

# ---- 纠偏 ----
_item("GCJRE_MAX_SOURCE_ZOOM", "最大源层级", "纠偏", "int", "18", hot=True,
      unit="级", minimum=0, maximum=22,
      help="超过该层级直接返回「级别过大」提示图，不打上游。0 = 不检查。"
           "高德/百度矢量最高 18；高德卫星 z19 仍有影像，用卫星时设 19。")
_item("GCJRE_MAX_TRANSPARENT_PERCENT", "透明率上限", "纠偏", "int", "10",
      unit="%", minimum=0, maximum=100,
      help="纠偏输出透明像素超过该比例即判废并重取。")
_item("GCJRE_FETCH_ATTEMPTS", "取片重试次数", "纠偏", "int", "3",
      unit="次", minimum=1, maximum=10,
      help="单张源片取失败（含空白片）后的重试次数，重试会绕过缓存。")
_item("GCJRE_MAX_CONCURRENCY", "回源并发上限", "纠偏", "int", "16",
      unit="路", minimum=1, maximum=256,
      help="整个进程同时向上游发起的请求数上限（进程级信号量）。")
_item("GCJRE_MAX_TILES_PER_REQUEST", "单请求源片上限", "纠偏", "int", "64",
      unit="片", minimum=4, maximum=256,
      help="防止参数被构造放大内存；正常一个输出最多 9 片。")
_item("GCJRE_MAX_IMAGE_PIXELS", "单图最大像素", "纠偏", "int", "40000000",
      unit="px", minimum=100000, maximum=200000000,
      help="解码上游图片时的像素上限，防解压炸弹。")
_item("GCJRE_TILE_MAX_MB", "单瓦片最大体积", "纠偏", "int", "8",
      unit="MB", minimum=1, maximum=64,
      help="单张上游瓦片的字节上限。")
_item("GCJRE_BLANK_RETRY_SECONDS", "空白片重试窗口", "纠偏", "int", "300",
      unit="秒", minimum=0, maximum=86400,
      help="磁盘里缓存超过该时长的空白源片会被当作未命中重取。")

# ---- 网络 ----
_item("PROXY_HTTP_TIMEOUT_SECONDS", "请求超时", "网络", "int", "30",
      unit="秒", minimum=1, maximum=300, help="向上游发起请求的总超时。")
_item("PROXY_HTTP_CONNECT_TIMEOUT_SECONDS", "连接超时", "网络", "int", "10",
      unit="秒", minimum=1, maximum=60, help="建立 TCP/TLS 连接的超时。")
_item("PROXY_MAX_CONNECTIONS", "最大连接数", "网络", "int", "80",
      unit="条", minimum=1, maximum=1000, help="httpx 连接池上限。")
_item("PROXY_MAX_KEEPALIVE_CONNECTIONS", "保活连接数", "网络", "int", "20",
      unit="条", minimum=0, maximum=1000, help="连接池里保持 keep-alive 的连接数。")
_item("PROXY_MAX_RESPONSE_MB", "响应体上限", "网络", "int", "32",
      unit="MB", minimum=1, maximum=1024, help="单次上游响应允许的最大字节数，超出 413。")
_item("PROXY_USER_AGENT", "上游 User-Agent", "网络", "str", "",
      maxlen=300, help="留空时按上游域名自动挑一个浏览器 UA。")

# ---- 安全 ----
_item("PROXY_ALLOWED_HOSTS", "允许的目标主机", "安全", "str", "",
      maxlen=2000, help="逗号分隔的域名白名单；留空 = 不限制（仍受 SSRF 规则约束）。")
_item("PROXY_ALLOW_PRIVATE_HOSTS", "允许私网地址", "安全", "bool", "0",
      help="是否允许代理到内网 IP（127.0.0.1 / 10.x / 192.168.x 等）。")
_item("PROXY_DNS_GUARD", "DNS 重绑定防护", "安全", "bool", "1",
      help="解析目标域名并拒绝解析到私网的结果。")
_item("PROXY_VERIFY_SSL", "校验上游证书", "安全", "bool", "1",
      help="关闭后不校验上游 TLS 证书，仅用于上游证书有问题的场景。")
_item("TILE_PROXY_ENFORCE_ORIGIN", "启用来源白名单", "安全", "bool", "0",
      help="开启后只允许白名单里的 Referer/Origin 访问 /proxy/*。")
_item("TILE_PROXY_ALLOWED_ORIGINS", "来源白名单", "安全", "str", "",
      maxlen=2000, help="逗号分隔的 Origin / 域名列表，仅在上面的开关打开时生效。")

GROUPS: List[str] = ["服务开关", "缓存", "纠偏", "网络", "安全"]


# ==================== 读取 / 写入 / 校验 ====================

def _coerce(key: str, raw: Any) -> Any:
    """按 schema 把值转成 Python 类型（给管理页回显用）。"""
    meta = RUNTIME_SETTINGS.get(key) or {}
    kind = meta.get("type")
    if kind == "bool":
        return str(raw).lower() in {"1", "true", "yes", "on"}
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return int(meta.get("default") or 0)
    return "" if raw is None else str(raw)


def runtime_values() -> Dict[str, Any]:
    """当前生效的全部运行时配置（默认值 + 数据库覆盖），按 schema 类型转好。"""
    stored = all_settings()
    out: Dict[str, Any] = {}
    for key, meta in RUNTIME_SETTINGS.items():
        raw = stored.get(key, meta.get("default"))
        out[key] = _coerce(key, raw)
    return out


def runtime_updated_at() -> Dict[str, float]:
    return _settings_updated_at()


def config_store_path() -> Path:
    """配置库路径（管理页回显用）。"""
    return _admin_db_path()


def validate_setting(key: str, raw: Any) -> str:
    """校验并规范化一个配置值；不合法抛 ValueError。"""
    meta = RUNTIME_SETTINGS.get(key)
    if meta is None:
        raise ValueError("未知配置项：%s" % key)
    kind = meta["type"]

    if kind == "bool":
        if isinstance(raw, bool):
            return "1" if raw else "0"
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return "1"
        if text in {"0", "false", "no", "off"}:
            return "0"
        raise ValueError("%s 需要布尔值" % key)

    if kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError("%s 需要整数" % key)
        lo, hi = meta.get("minimum"), meta.get("maximum")
        if lo is not None and value < lo:
            raise ValueError("%s 不能小于 %s" % (key, lo))
        if hi is not None and value > hi:
            raise ValueError("%s 不能大于 %s" % (key, hi))
        return str(value)

    if kind == "enum":
        text = str(raw).strip().lower()
        if text not in (meta.get("options") or []):
            raise ValueError("%s 只能是 %s" % (key, " / ".join(meta.get("options") or [])))
        return text

    text = "" if raw is None else str(raw).strip()
    maxlen = meta.get("maxlen")
    if maxlen and len(text) > maxlen:
        raise ValueError("%s 太长（最多 %s 字符）" % (key, maxlen))
    return text


def save_settings(changes: Dict[str, Any]) -> Dict[str, Any]:
    """批量保存（先全部校验，再一次性写库）；返回新的运行时值。"""
    normalized: Dict[str, str] = {}
    for key, raw in changes.items():
        normalized[key] = validate_setting(key, raw)
    set_settings(normalized)
    return runtime_values()


def schema_payload() -> List[Dict[str, Any]]:
    """给前端渲染表单用的清单（带分组、范围、单位、生效方式）。"""
    out: List[Dict[str, Any]] = []
    for key, meta in RUNTIME_SETTINGS.items():
        out.append({
            "key": key,
            "label": meta["label"],
            "group": meta["group"],
            "type": meta["type"],
            "default": meta["default"],
            "unit": meta.get("unit", ""),
            "hot": bool(meta.get("hot")),
            "help": meta.get("help", ""),
            "min": meta.get("minimum"),
            "max": meta.get("maximum"),
            "options": meta.get("options"),
        })
    return out


# ==================== 限流 / 开关 ====================

RATE_LIMIT_MODES = ("image-203", "image-200", "json-429")
RATE_LIMIT_MODE_DEFAULT = "image-203"


def rate_limit_mode() -> str:
    """当前限流响应形态：管理页 > env PROXY_RATE_LIMIT_MODE > 默认 image-203。"""
    value = get_str("PROXY_RATE_LIMIT_MODE", RATE_LIMIT_MODE_DEFAULT).strip().lower()
    return value if value in RATE_LIMIT_MODES else RATE_LIMIT_MODE_DEFAULT


def rate_limit_enabled() -> bool:
    """限流开关（默认开；速率为 0 时等于不限流）。"""
    return get_bool("PROXY_RATE_LIMIT_ENABLED", True)


def rate_limit_per_minute() -> int:
    """当前生效的每分钟阈值；0 = 不限流。"""
    if not rate_limit_enabled():
        return 0
    return get_int("PROXY_RATE_LIMIT", 0, minimum=0, maximum=100000)


def proxy_enabled() -> bool:
    """瓦片代理总开关（默认开）。关掉后 /proxy/* 一律 503。"""
    return get_bool("PROXY_ENABLED", True)


def max_source_zoom() -> int:
    """源瓦片最大层级（0 = 不限制）。"""
    return get_int("GCJRE_MAX_SOURCE_ZOOM", 18, minimum=0, maximum=22)


# ==================== 瓦片缓存策略（唯一真源） ====================
# 上游各源的 cache-control 天差地别（2026-10-29 实测）：
#   Google 卫星 lyrs=s      → public, max-age=22222222（≈257 天）
#   Google 路网 lyrs=m/y    → public, max-age=3600
#   高德 webrd01/webst01    → max-age=3600
#   纠偏路由（gcj2wgs 等）  → 完全没有
# 与其原样透传，不如统一改写成显式策略：浏览器可撤、边缘长存。

def tile_cache_control() -> str:
    """瓦片响应统一的 Cache-Control 值（max-age=浏览器，s-maxage=CDN 边缘）。

    每次调用都读配置——管理页改完 TTL 立刻生效，不用重启。
    """
    browser = get_int("TILE_BROWSER_TTL_SECONDS", 86400, minimum=0, maximum=2592000)
    edge = get_int("TILE_EDGE_TTL_SECONDS", 604800, minimum=0, maximum=31536000)
    return "public, max-age=%d, s-maxage=%d" % (browser, edge)
