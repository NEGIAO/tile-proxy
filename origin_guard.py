# -*- coding: utf-8 -*-
"""来源校验：阻止「别人网站」在浏览器里盗用本代理，但不影响本地/桌面端使用。

## 判定规则（三条）

| 请求特征 | 判定 | 典型来源 |
|---|---|---|
| 有 `Origin` | 必须在域名白名单，否则 403 | `fetch` / CORS / 跨域图片 |
| 无 `Origin`、有 `Referer` | 取 Referer 的 origin，必须在白名单，否则 403 | `<img>` 盗链（瓦片最常见形态） |
| **两者都没有** | **放行** | QGIS / ArcGIS Pro / curl / 服务端脚本 |

## 为什么这样设计

- 浏览器发出的请求**必然**带 `Origin`（CORS）或 `Referer`（no-cors 的 `<img>`），
  所以盗链一定会被识别；
- 桌面 GIS 与命令行工具**两者都不发**，因此天然放行，本地使用零配置；
- 代价：对方若刻意用 `referrerpolicy="no-referrer"` 隐藏来源，本策略拦不住
  （属于"防盗链"的固有限制，不是实现缺陷）。真要强认证需另加令牌。

## 缓存（2026-10-29 调整）

本中间件**不再改写 `Cache-Control`**。原先这里统一打 `private, max-age=300`，
目的是让共享缓存不缓存、从而保住白名单；但 2026-10-28 起「CF 边缘缓存 7 天」
已是既定策略，`private` 与它直接冲突——一旦启用本中间件，CF 会立刻对
`/proxy/*` 全部 `BYPASS`，边缘缓存归零（这正是此前该模块一直没启用的原因之一）。

缓存头现在由 `cache_policy.TileCachePolicyMiddleware` 统一负责
（`public, max-age=<浏览器 TTL>, s-maxage=<边缘 TTL>`）。两者分工明确：
本中间件管「谁能访问」，缓存策略管「能存多久」。代价见 server-ops.md §3.1——
边缘缓存后缓存键只有 URL，白名单在 CDN 层本就失效。

## 配置

- `TILE_PROXY_ALLOWED_ORIGINS`：逗号分隔，覆盖默认白名单；留空/全部非法时用内置列表
- `TILE_PROXY_ENFORCE_ORIGIN`：`1/true/yes/on` = 启用白名单校验（默认 `0` = 不挂载本中间件，
  CORS 同时全开放）。观察模式（只记 warning 不拦截）需在代码里显式调用
  `build_origin_guard(ALLOWED_ORIGINS, enforce=False)`——默认不开，免得刷日志。
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("tile-proxy.origin")

# 默认白名单：WebGIS 前端的全部部署域名 + 本代理站点自身 + 本地开发
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://webgis.negiao.cn",        # 正式域名（GitHub Pages 自定义域）
    "https://www.negiao.cn",           # 个人主页下的 /WebGIS/
    "https://negiao.github.io",        # Pages 默认域
    "https://negiao-webgis.hf.space",  # Hugging Face Space（前端）
    "https://negiao-web.static.hf.space",
    "https://gis.negiao.cn",           # 北京轻量站点自身（tile-proxy.html 的示例瓦片链接）
    "https://vpn.negiao.cn",           # Azure 站点自身（index.html 的示例瓦片链接）
    "http://localhost:5173",           # vite dev
    "http://127.0.0.1:5173",
    "http://localhost:4173",           # vite preview
    "http://127.0.0.1:4173",
)

# 无需来源校验的路径（探活/文档）
EXEMPT_PATHS: tuple[str, ...] = ("/health", "/docs", "/openapi.json", "/redoc")


def normalize_origin(value: str | None) -> str | None:
    """把任意 URL / Origin 归一成 `scheme://host[:port]`（小写、去尾斜杠）。

    非法值、空串、字面量 `null` 一律返回 None（`null` 视为"无来源"）。
    """
    text = (value or "").strip()
    if not text or text.lower() == "null":
        return None
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def parse_allowed(raw: str | None, fallback: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS) -> list[str]:
    """解析 `TILE_PROXY_ALLOWED_ORIGINS`；为空或全部非法时回落默认白名单。"""
    items = [normalize_origin(item) for item in (raw or "").split(",")]
    cleaned = [item for item in items if item]
    if cleaned:
        return cleaned
    return [origin for origin in (normalize_origin(x) for x in fallback) if origin]


def candidate_origin(request: Request) -> str | None:
    """取请求的来源：`Origin` 优先，缺失时回落 `Referer`；都没有返回 None。"""
    origin = request.headers.get("origin")
    if origin:
        return normalize_origin(origin)
    return normalize_origin(request.headers.get("referer"))


def build_origin_guard(allowed: list[str], enforce: bool = True):
    """构造来源校验中间件。

    @param allowed 允许的 origin 列表（已归一）
    @param enforce False = 观察模式：只对非白名单来源记 warning，不拦截
    """
    allowed_set = set(allowed)

    async def origin_guard(request: Request, call_next):
        path = request.url.path
        if any(path == item or path.startswith(item + "/") for item in EXEMPT_PATHS):
            return await call_next(request)

        origin = candidate_origin(request)

        if not enforce:
            if origin is not None and origin not in allowed_set:
                logger.warning(
                    "[origin-guard][observe] 非白名单来源 path=%s origin=%s ua=%s",
                    path, origin, request.headers.get("user-agent", "-"),
                )
            return await call_next(request)

        # 无 Origin 且无 Referer → 桌面 GIS / curl / 服务端脚本，放行
        if origin is None:
            return await call_next(request)

        if origin not in allowed_set:
            logger.warning("[origin-guard] 拒绝：来源不在白名单 path=%s origin=%s", path, origin)
            return JSONResponse(
                status_code=403,
                headers={"Cache-Control": "no-store"},
                content={
                    "detail": f"来源 {origin} 不在允许列表",
                    "hint": "本代理仅对自有部署域名开放；桌面端（QGIS / ArcGIS Pro）不带来源头，可直接访问",
                },
            )

        return await call_next(request)

    return origin_guard
