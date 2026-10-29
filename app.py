# -*- coding: utf-8 -*-
"""vpn.negiao.cn 瓦片纠偏/直通代理（FastAPI）。

高内聚：本目录 + domains/tiles（不含 download）即可运行。
HF Space 不再承载任何 /proxy/* 瓦片中转；对外仅经 nginx 反代本进程。
跨域：CORS 全开放（瓦片走 <img>，浏览器侧无需凭据）；
滥用防护交给限流与连接数上限（PROXY_RATE_LIMIT / PROXY_MAX_CONNECTIONS）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from cache_policy import TileCachePolicyMiddleware
from config import get_str
from domains.tiles import build_http_client, cache_cleanup_loop, tiles_router
from origin_guard import build_origin_guard, parse_allowed

logging.basicConfig(
    level=get_str("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("tile-proxy")

# 版本唯一真源：FastAPI 的 /openapi.json、/health、内置首页的版本角标都取这里。
# 发版时同步 pyproject.toml / Dockerfile LABEL / CHANGELOG / README 徽章。
APP_VERSION = "1.1.0"

# 内置首页：仓库内的联调示例页，容器里由 Dockerfile COPY examples 带进去。
DEMO_PAGE = Path(__file__).resolve().parent / "examples" / "openlayers-demo.html"
# 首页用的示例数据（建筑白模 GeoJSON）
DEMO_DATA_DIR = DEMO_PAGE.parent / "data"

# 源码树里缺 examples/（例如只拷了 app.py + domains）时的兜底页：
# 不报 404，至少告诉用户服务活着、去哪儿看文档。
_FALLBACK_INDEX = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>tile-proxy</title></head>
<body style="font:15px/1.6 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em">
<h1 style="font-size:1.4em">tile-proxy 已启动</h1>
<p>未找到内置示例页 <code>examples/openlayers-demo.html</code>，服务本身正常。</p>
<ul>
  <li><a href="/health">/health</a> — 探活</li>
  <li><a href="/docs">/docs</a> — API 文档</li>
</ul>
<p>瓦片基址：<code>/proxy/&lt;上游模板&gt;</code>，纠偏：<code>/proxy/gcj2wgs/&lt;上游模板&gt;</code> 等。</p>
</body></html>
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("tile-proxy 启动")
    app.state.http_client = build_http_client()
    app.state.gcjre_cache_cleanup = asyncio.create_task(cache_cleanup_loop())
    yield
    task = getattr(app.state, "gcjre_cache_cleanup", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    client = getattr(app.state, "http_client", None)
    if client is not None:
        await client.aclose()
    logger.info("tile-proxy 已关闭")


class CorsCacheSafeMiddleware:
    """给所有响应补上 `Access-Control-Allow-Origin`，让响应与 Origin 无关。

    背景：Starlette 的 CORSMiddleware **只在请求带 Origin 时**才回 ACAO，而瓦片
    会被 nginx / CDN 按 URL 缓存（缓存键不含 Origin，nginx 也不看 Vary）。于是
    「先被非浏览器客户端取一次」就会把一份**没有 ACAO 的响应**存进共享缓存，
    之后浏览器带 Origin 来拿到的还是它 —— 前端直接报 CORS 错误。
    线上 z17/x=107153/y=51995 就是这样：命中 nginx 缓存，且完全没有 ACAO。
    让响应与 Origin 无关，任何缓存层都不会再放大这个问题。

    `enabled=False`（来源白名单模式）时不补，保持 CORSMiddleware 的按 Origin 判定。
    """

    def __init__(self, app, enabled: bool = True):
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(key.lower() == b"access-control-allow-origin" for key, _ in headers):
                    headers.append((b"access-control-allow-origin", b"*"))
                    new_message = dict(message)
                    new_message["headers"] = headers
                    message = new_message
            await send(message)

        await self.app(scope, receive, send_wrapper)


app = FastAPI(title="vpn.negiao.cn tile-proxy", version=APP_VERSION, lifespan=lifespan)

# 来源校验总开关（默认关闭）：TILE_PROXY_ENFORCE_ORIGIN=1 才启用白名单。
# 滥用防护已由限流承担（PROXY_RATE_LIMIT / PROXY_MAX_CONNECTIONS），
# 需要临时收紧时把开关置 1 即可，无需改代码。
ALLOWED_ORIGINS = parse_allowed(get_str("TILE_PROXY_ALLOWED_ORIGINS", ""))
ENFORCE_ORIGIN = get_str("TILE_PROXY_ENFORCE_ORIGIN", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

if ENFORCE_ORIGIN:
    # 先加校验（内层）再加 CORS（外层），使 403 响应也带 CORS 头
    app.middleware("http")(build_origin_guard(ALLOWED_ORIGINS, True))

# 跨域：开关关闭时全开放（瓦片走 <img>，无需凭据）；开启时只放行白名单域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ENFORCE_ORIGIN else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

logger.info(
    "来源校验：%s（白名单 %d 项）",
    "启用" if ENFORCE_ORIGIN else "关闭", len(ALLOWED_ORIGINS),
)

# 瓦片缓存策略：统一改写 200 图片响应的 Cache-Control（详见 cache_policy.py）。
# 最后添加 = 最外层，确保它看到的是 CORS 等中间件处理完的最终响应头。
app.add_middleware(TileCachePolicyMiddleware)
# 最外层：保证任何响应（含缓存层放大的）都带 ACAO，见类文档
app.add_middleware(CorsCacheSafeMiddleware, enabled=not ENFORCE_ORIGIN)

app.include_router(tiles_router)


@app.get("/", include_in_schema=False)
async def index():
    """内置首页：联调示例页（地图 + 各代理模式切换）。

    放在 `/` 而不是静态目录挂载，是为了让容器/裸 py 启动后直接打开
    `http://127.0.0.1:9002/` 就能验证服务，无需另找 html 文件。
    """
    if DEMO_PAGE.is_file():
        return FileResponse(DEMO_PAGE, media_type="text/html; charset=utf-8")
    return HTMLResponse(_FALLBACK_INDEX)


@app.get("/demo/data/{name}", include_in_schema=False)
async def demo_data(name: str):
    """首页示例数据（如建筑白模 GeoJSON）。

    只暴露 `examples/data/` 下的 json/geojson：文件名必须与 `Path.name` 一致
    （挡掉 `../`、子目录、绝对路径），且后缀白名单。
    """
    if (
        Path(name).name != name
        or name.startswith(".")
        or not name.endswith((".geojson", ".json"))
    ):
        raise HTTPException(status_code=404, detail="not found")
    path = DEMO_DATA_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="application/geo+json")


@app.get("/health")
async def health():
    return {"ok": True, "service": "tile-proxy", "version": APP_VERSION}
