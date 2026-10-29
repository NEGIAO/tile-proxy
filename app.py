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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cache_policy import TileCachePolicyMiddleware
from config import get_str
from domains.tiles import build_http_client, cache_cleanup_loop, tiles_router
from origin_guard import build_origin_guard, parse_allowed

logging.basicConfig(
    level=get_str("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("tile-proxy")


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


app = FastAPI(title="vpn.negiao.cn tile-proxy", version="1.1.0", lifespan=lifespan)

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

app.include_router(tiles_router)


@app.get("/health")
async def health():
    return {"ok": True, "service": "tile-proxy"}
