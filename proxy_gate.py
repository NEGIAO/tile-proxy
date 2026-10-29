# -*- coding: utf-8 -*-
"""瓦片代理总开关（`/proxy/*` 的入口闸门）。

管理页把「瓦片代理」关掉时，这里直接返回 503，连路由都不进——比在 5 条路由里
各加一个判断更省事，也不会漏掉将来新增的入口。

响应刻意带上：
- `Cache-Control: no-store` + `X-Tile-Notice: disabled`：任何一层都别缓存这条 503；
- `Retry-After`：告诉客户端「这是临时的」。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from config import proxy_enabled


class ProxyGateMiddleware:
    """总开关关闭时，让 `/proxy/*` 立刻 503。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if not scope.get("path", "").startswith("/proxy/") or proxy_enabled():
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"detail": "瓦片代理已关闭（可在管理页重新开启）", "disabled": True},
            status_code=503,
            headers={
                "Cache-Control": "no-store",
                "X-Tile-Notice": "disabled",
                "X-Tile-NoCache": "1",
                "Retry-After": "60",
            },
        )
        await response(scope, receive, send)
