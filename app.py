# -*- coding: utf-8 -*-
"""vpn.negiao.cn 瓦片纠偏/直通代理（FastAPI）。

高内聚：本目录 + domains/tiles（不含 download）即可运行。
HF Space 不再承载任何 /proxy/* 瓦片中转；对外仅经 nginx 反代本进程。
跨域：浏览器前端在 webgis.negiao.cn / localhost:5173 / HF Space，必须允许 CORS。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_str
from domains.tiles import build_http_client, cache_cleanup_loop, tiles_router

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


app = FastAPI(title="vpn.negiao.cn tile-proxy", version="1.0.0", lifespan=lifespan)

# 瓦片/能力文档被浏览器 fetch；前端与本域不同源（Pages / HF / localhost）
# allow_origins=* 且无 credentials，满足 img/fetch 场景
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

app.include_router(tiles_router)


@app.get("/health")
async def health():
    return {"ok": True, "service": "tile-proxy"}
