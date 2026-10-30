# -*- coding: utf-8 -*-
"""瓦片缓存策略中间件：给所有 200 图片响应统一打 Cache-Control。

## 为什么需要它

直通路由把上游的 `cache-control` / `expires` **原样透传**，而各源取值天差地别
（2026-10-29 实测）：

| 上游 | cache-control |
|---|---|
| Google 卫星 `lyrs=s` | `public, max-age=22222222`（≈257 天） |
| Google 路网 `lyrs=m` / `y` | `public, max-age=3600` |
| 高德 `webrd01` / `webst01` | `max-age=3600` |
| 纠偏路由（gcj2wgs 等 4 条） | **完全没有**（浏览器只能启发式缓存） |

后果：卫星瓦片的浏览器 TTL 是 257 天。瓦片内容确实不会变，但一旦某块瓦片
因上游抖动被缓存成黑图/坏图，**CF purge 只清边缘、清不掉浏览器**，用户半年
内看到的都是坏的，且没有任何补救手段。

## 策略

统一改写为 `public, max-age=<浏览器>, s-maxage=<边缘>`（见 config.tile_cache_control）：

- 浏览器短（默认 1 天）：出问题隔天自愈，换源也能靠换 URL 立刻生效；
- 边缘长（默认 7 天）：与 CF Cache Rule 对齐；规则万一被改动或失效，
  `s-maxage` 仍能让共享缓存兜住 7 天。

只作用于 **200 + `image/*`**：本应用是通用代理，HTML/JSON 等不能被当瓦片缓存。

## 为什么用纯 ASGI 而不是 BaseHTTPMiddleware

直通路由返回 StreamingResponse。`BaseHTTPMiddleware` 会再包一层 anyio 流，
在改 header 的同时容易牵动 body 与 BackgroundTasks 的语义。这里只在
`http.response.start` 上改 header，body 一个字节都不碰。
"""

from __future__ import annotations

from config import tile_cache_control

# 需要剥掉的旧缓存头：留着会与新的 max-age 冲突（浏览器优先认 cache-control，
# 但 expires 并存时部分中间层/代理会做二次判断，徒增歧义）
_DROP_HEADERS = (b"cache-control", b"expires", b"pragma")


class TileCachePolicyMiddleware:
    """统一改写 200 图片响应的 Cache-Control。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                message = _rewrite(message)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _rewrite(message):
    """就地改写一条 http.response.start 消息，返回（可能是新的）消息。"""
    if message.get("status") != 200:
        return message

    headers = list(message.get("headers") or [])

    content_type = b""
    for key, value in headers:
        if key.lower() == b"content-type":
            content_type = value
            break
    if not content_type.lower().startswith(b"image/"):
        return message

    kept = [(k, v) for (k, v) in headers if k.lower() not in _DROP_HEADERS]

    # 空白瓦片：调用方已用 X-Tile-Blank: 1 声明"这块是空的、别缓存"
    # （见 routes_rectify._tile_response）。此时绝不能套用 7 天策略，
    # 否则一次上游抖动就会把白斑钉在边缘/浏览器里一整个周期。
    # 两个标记都算「别缓存」：X-Tile-Blank 是空白瓦片，X-Tile-NoCache 是限流提示图
    # 之类的临时响应。CDN 是否认这个头另说（有的缓存规则会无视），nginx 侧一定能拦住。
    blank = any(
        (k.lower() == b"x-tile-blank" and v.strip() == b"1")
        or (k.lower() == b"x-tile-nocache" and v.strip() == b"1")
        # X-Tile-Suspect：直通路由判定「不同瓦片返回同一份字节」（拦截图/占位图），
        # 同样是错误内容，绝不能套 7 天策略把它钉在边缘。
        or (k.lower() == b"x-tile-suspect" and v.strip() == b"1")
        for k, v in headers
    )
    kept.append((b"cache-control",
                 b"no-store" if blank else tile_cache_control().encode("latin-1")))

    new_message = dict(message)
    new_message["headers"] = kept
    return new_message
