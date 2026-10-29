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
import os
import time
from typing import Any, Dict, List
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from cache_policy import TileCachePolicyMiddleware
from proxy_gate import ProxyGateMiddleware
from config import (
    GROUPS,
    config_store_path,
    get_str,
    proxy_enabled,
    rate_limit_enabled,
    rate_limit_mode,
    rate_limit_per_minute,
    runtime_updated_at,
    runtime_values,
    save_settings,
    schema_payload,
)
from admin_store import (
    ensure_password,
    is_initialized,
    issue_token,
    set_password,
    verify_password,
    verify_token,
)
from domains.tiles import build_http_client, cache_cleanup_loop, tiles_router
from domains.tiles.notices import is_tile_request, notice_response
from domains.tiles.proxy_shared import RateLimitExceeded
from origin_guard import build_origin_guard, parse_allowed

logging.basicConfig(
    level=get_str("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("tile-proxy")

# 版本唯一真源：FastAPI 的 /openapi.json、/health、内置首页的版本角标都取这里。
# 发版时同步 pyproject.toml / Dockerfile LABEL / CHANGELOG / README 徽章。
APP_VERSION = "1.1.0"

# 进程启动时间（管理页展示 uptime）
STARTED_AT = time.time()

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
    ensure_password()
    if not is_initialized():
        logger.warning(
            "管理密码尚未设置：打开 /admin 完成初始化后才能修改配置"
        )
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

# 代理总开关：关闭时 /proxy/* 直接 503（见 proxy_gate.py）
app.add_middleware(ProxyGateMiddleware)
# 瓦片缓存策略：统一改写 200 图片响应的 Cache-Control（详见 cache_policy.py）。
# 最后添加 = 最外层，确保它看到的是 CORS 等中间件处理完的最终响应头。
app.add_middleware(TileCachePolicyMiddleware)
# 最外层：保证任何响应（含缓存层放大的）都带 ACAO，见类文档
app.add_middleware(CorsCacheSafeMiddleware, enabled=not ENFORCE_ORIGIN)

app.include_router(tiles_router)


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded):
    """限流响应：形态由管理页的 `PROXY_RATE_LIMIT_MODE` 决定（改完立即生效）。

    | mode | 状态码 | 响应体 | 适用 |
    |---|---|---|---|
    | `image-203`（默认） | 203 | `error1.png` | 浏览器 / OL 能渲染；203 不在 nginx `proxy_cache_valid 200`、也不在 CF 默认可缓存状态码里，天然不落缓存 |
    | `image-200` | 200 | `error1.png` | 客户端只认 200（Cesium 的 ImageryProvider） |
    | `json-429` | 429 | JSON | 纯 API 调用方 |

    不缓存：`no-store` + `X-Tile-Blank: 1` + `X-Tile-NoCache: 1`；走异常分支，
    不进内存/磁盘缓存。窗口一过，同一个 URL 立刻回到正常流程返回真实瓦片。
    """
    retry_after = str(getattr(exc, "retry_after", 60))
    mode = rate_limit_mode()

    if mode == "json-429" or not is_tile_request(request):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests", "mode": mode},
            headers={"Retry-After": retry_after},
        )

    return notice_response(
        "error1",
        status_code=203 if mode == "image-203" else 200,
        cache_control="no-store, no-cache, must-revalidate, max-age=0",
        headers={
            "Retry-After": retry_after,
            "X-Tile-Blank": "1",
            "X-Tile-NoCache": "1",
        },
    )




@app.get("/", include_in_schema=False)
async def index():
    """内置首页：联调示例页（地图 + 各代理模式切换）。

    放在 `/` 而不是静态目录挂载，是为了让容器/裸 py 启动后直接打开
    `http://127.0.0.1:9002/` 就能验证服务，无需另找 html 文件。
    线上通过 nginx 的 `/tile-proxy/` 前缀访问这个页面。
    """
    if DEMO_PAGE.is_file():
        # 页面会随版本更新：加 no-cache 让浏览器每次校验，避免看到旧 UI
        return FileResponse(
            DEMO_PAGE, media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )
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


ADMIN_PAGE = Path(__file__).resolve().parent / "examples" / "admin.html"


@app.get("/admin", include_in_schema=False)
async def admin_page():
    """管理控制台（文档 + 状态 + 配置表单）。开源仓库自带这个页面。"""
    if ADMIN_PAGE.is_file():
        return FileResponse(
            ADMIN_PAGE, media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )
    return HTMLResponse(
        "<h1>管理页缺失</h1><p>仓库里缺少 <code>examples/admin.html</code>。</p>",
        status_code=404,
    )


@app.get("/tile-proxy.html", include_in_schema=False)
async def console_alias():
    """控制台的别名路径。

    两个场景都会落到这里：独立部署时用户直接输 `/tile-proxy.html`；站点上被
    `/tile-proxy/` 前缀反代后，页面里的相对链接会变成 `/tile-proxy/tile-proxy.html`。
    直接返回同一个页面（页面会按 pathname 自动选 API 基址），**不做重定向**——
    之前那句 `RedirectResponse("/tile-proxy.html")` 在独立部署里是「自己跳自己」，
    浏览器会直接报 ERR_TOO_MANY_REDIRECTS。
    """
    if ADMIN_PAGE.is_file():
        return FileResponse(
            ADMIN_PAGE, media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )
    return HTMLResponse(
        "<h1>管理页缺失</h1><p>仓库里缺少 <code>examples/admin.html</code>。</p>",
        status_code=404,
    )


@app.get("/health")
async def health():
    return {"ok": True, "service": "tile-proxy", "version": APP_VERSION}


# ==================== 管理接口（密码登录 → 令牌） ====================
# 密码用 PBKDF2 存 SQLite（见 admin_store），登录换一个 12 小时有效的签名令牌；
# 写操作必须带 `Authorization: Bearer <token>`。登录失败按 IP 节流：5 次 / 5 分钟。
_LOGIN_FAILS: Dict[str, List[float]] = {}
_LOGIN_WINDOW = 300.0
_LOGIN_MAX_FAILS = 5


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（线上走 nginx，优先 X-Real-IP / X-Forwarded-For）。"""
    for header in ("x-real-ip", "x-forwarded-for"):
        value = request.headers.get(header, "")
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "-"


def _login_blocked(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAILS[ip] = hits
    return len(hits) >= _LOGIN_MAX_FAILS


def _require_admin(request: Request) -> None:
    """校验 `Authorization: Bearer <token>`；未登录/过期一律 401。"""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")


def _public_status() -> dict:
    """公开状态（不需要登录）：页面顶部徽章与「当前生效」用，不含任何敏感配置。"""
    return {
        "service": "tile-proxy",
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "proxy_enabled": proxy_enabled(),
        "rate_limit_enabled": rate_limit_enabled(),
        "rate_limit": rate_limit_per_minute(),
        "rate_limit_mode": rate_limit_mode(),
        "initialized": is_initialized(),
        "time": int(time.time()),
    }


def _admin_payload() -> dict:
    """登录后的完整数据：全部配置值 + 表单 schema + 分组 + 存储位置。"""
    return {
        **_public_status(),
        "settings": runtime_values(),
        "schema": schema_payload(),
        "groups": GROUPS,
        "updated_at": runtime_updated_at(),
        "store": str(config_store_path()),
    }


@app.get("/admin/status")
async def admin_status():
    """公开只读状态：版本、运行时长、代理开关、限流开关与速率。"""
    return _public_status()


@app.post("/admin/setup")
async def admin_setup(request: Request):
    """首次使用：设置管理密码（仅当库里还没有密码时可用，之后只能走 /admin/password）。

    这是开源仓库的「开箱即用」入口——不内置默认口令，谁部署谁设密码。
    """
    if is_initialized():
        raise HTTPException(
            status_code=409,
            detail="管理密码已设置过；如需修改请登录后使用「修改密码」",
        )
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    password = str(payload.get("password", ""))
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")
    set_password(password)
    token, ttl = issue_token()
    logger.info("管理密码初始化完成（ip=%s）", _client_ip(request))
    return {"token": token, "expires_in": ttl, "payload": _admin_payload()}


@app.post("/admin/login")
async def admin_login(request: Request):
    """密码登录，成功返回 12 小时有效的令牌。"""
    if not is_initialized():
        raise HTTPException(
            status_code=409,
            detail="尚未设置管理密码，请先在页面上完成初始化",
        )
    ip = _client_ip(request)
    if _login_blocked(ip):
        raise HTTPException(status_code=429, detail="尝试次数过多，请 5 分钟后再试")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    password = str(payload.get("password", ""))
    if not verify_password(password):
        _LOGIN_FAILS.setdefault(ip, []).append(time.time())
        raise HTTPException(status_code=401, detail="密码不正确")
    _LOGIN_FAILS.pop(ip, None)
    token, ttl = issue_token()
    logger.info("管理登录成功：%s", ip)
    return {"token": token, "expires_in": ttl, "payload": _admin_payload()}


@app.post("/admin/password")
async def admin_change_password(request: Request):
    """改管理密码（需已登录 + 旧密码）。改完所有旧令牌立即失效。"""
    _require_admin(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    old = str(payload.get("old", ""))
    new = str(payload.get("new", ""))
    if not verify_password(old):
        raise HTTPException(status_code=400, detail="旧密码不正确")
    if len(new) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    set_password(new)
    logger.info("管理密码已更新（ip=%s）", _client_ip(request))
    return {"ok": True, "message": "密码已更新，请重新登录"}


@app.get("/admin/config")
async def get_admin_config(request: Request):
    """读全部配置（需登录）。"""
    _require_admin(request)
    return _admin_payload()


@app.put("/admin/config")
async def put_admin_config(request: Request):
    """改配置（需登录）。body: `{"settings": {"PROXY_RATE_LIMIT": 300, ...}}`。

    先整批校验（类型 / 范围 / 枚举），任何一项不合法就整体拒绝，不会写一半。
    """
    _require_admin(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON")
    incoming = payload.get("settings")
    if not isinstance(incoming, dict) or not incoming:
        raise HTTPException(status_code=400, detail="settings 不能为空")
    try:
        save_settings(incoming)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("管理配置已更新（ip=%s）：%s", _client_ip(request), list(incoming))
    return _admin_payload()
