# -*- coding: utf-8 -*-
"""直通瓦片路由：通用流式代理 `/proxy/{target_url:path}`。

内容整体搬自旧 `api/proxy.py`，逻辑逐字保留。
1.1.0 起移除了 `/tiles/ships66/{z}/{x}/{y}.png` 专用海图端点
（`SHIPS66_TILE_URL_TEMPLATE` 同步废弃）：它只是 `build_tile_url` 的
一次性包装，用 `/proxy/{上游模板}` 即可等价表达。

⚠️ 注册顺序约束：必须后于 `routes_rectify` 挂载（见该模块文档）。
"""

import logging
from typing import Dict
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from domains.tiles.proxy_shared import (
    PROXY_DEFAULT_REQUEST_HEADERS,
    PROXY_HOP_BY_HOP_HEADERS,
    PROXY_PASSTHROUGH_HEADERS,
    _build_proxy_request_headers,
    _build_proxy_target_url,
    _limited_stream,
    _rate_limit_check,
    _reject_if_content_length_exceeds,
)

router = APIRouter()

logger = logging.getLogger(__name__)

# 同源重定向最多跟几跳（防环）
_MAX_REDIRECTS = 3

# 「上游讲的不是 TLS」类错误的特征串（OpenSSL / BoringSSL 两种措辞）
_SCHEME_MISMATCH_MARKERS: tuple[str, ...] = (
    "WRONG_VERSION_NUMBER",
    "record layer failure",
    "unknown protocol",
)

# ==================== 通用流式代理 ====================
@router.get("/proxy/{target_url:path}")
async def universal_stream_proxy(target_url: str, request: Request, _: None = Depends(_rate_limit_check)):
    """通用流式代理接口"""
    upstream_url = _build_proxy_target_url(
        target_url, request.url.query,
        referer=request.headers.get("referer", ""),
    )

    proxy_request_headers = _build_proxy_request_headers(request, upstream_url)

    shared_client = getattr(request.app.state, "http_client", None)
    fallback_client = None
    client = shared_client

    if client is None:
        fallback_client = httpx.AsyncClient(follow_redirects=False)
        client = fallback_client

    try:
        upstream_request = client.build_request("GET", upstream_url, headers=proxy_request_headers)
        upstream_response = await client.send(upstream_request, stream=True)

        # 同源重定向自动跟随（最多 3 跳）。
        # 典型场景：ESRI 的 WMTS 路径大小写不敏感、且旧版本号会 301 到新版本
        # （`.../tile/26334/...` → `/arcgis/rest/services/world_imagery/.../tile/25285/...`），
        # 而 301 的 Location 是**相对路径**——原样透给浏览器会被解析到我们自己的域名下，
        # 于是 404。这里直接跟随，客户端拿到的就是最终瓦片。
        # 只跟随**同 host**：跨域重定向仍按原样透传，不扩大 SSRF 面。
        hops = 0
        while upstream_response.status_code in (301, 302, 303, 307, 308) and hops < _MAX_REDIRECTS:
            location = upstream_response.headers.get("location")
            if not location:
                break
            next_url = urljoin(str(upstream_response.url), location)
            if urlparse(next_url).netloc != urlparse(str(upstream_response.url)).netloc:
                break
            await upstream_response.aclose()
            hops += 1
            logger.debug("同源重定向跟随(%d)：%s", hops, next_url)
            upstream_request = client.build_request("GET", next_url, headers=proxy_request_headers)
            upstream_response = await client.send(upstream_request, stream=True)

        if upstream_response.status_code in (301, 302, 307, 308):
            location = upstream_response.headers.get("location")

            background = BackgroundTasks()
            background.add_task(upstream_response.aclose)
            if fallback_client:
                background.add_task(fallback_client.aclose)

            return Response(
                status_code=upstream_response.status_code,
                headers={"Location": location},
                background=background,
            )
    except httpx.TimeoutException:
        if fallback_client is not None:
            await fallback_client.aclose()
        return JSONResponse(
            status_code=504,
            content={"detail": "代理请求超时", "upstream": upstream_url},
            headers={"X-Proxy-Status": "TIMEOUT"},
        )
    except httpx.HTTPError as exc:
        if fallback_client is not None:
            await fallback_client.aclose()
        hint = _scheme_hint(str(exc), target_url)
        if hint:
            # 用 400 而非 502：Cloudflare 会把源站的 5xx 换成自家「error code: 502」页，
            # 真实原因到不了调用方；4xx 会原样透出 body，提示才看得见。
            return JSONResponse(
                status_code=400,
                content={"detail": hint, "upstream": upstream_url, "error": str(exc)},
                headers={"X-Proxy-Status": "SCHEME_MISMATCH"},
            )
        return JSONResponse(
            status_code=502,
            content={"detail": "代理请求失败", "upstream": upstream_url, "error": str(exc)},
            headers={"X-Proxy-Status": "UPSTREAM_ERROR"},
        )

    response_headers: Dict[str, str] = {}
    for key, value in upstream_response.headers.items():
        lower_key = key.lower()
        if lower_key in PROXY_HOP_BY_HOP_HEADERS:
            continue
        if lower_key in PROXY_PASSTHROUGH_HEADERS:
            response_headers[key] = value

    proxy_status = "SUCCESS" if upstream_response.status_code < 400 else "UPSTREAM_ERROR"
    response_headers["X-Proxy-Status"] = proxy_status

    background_tasks = BackgroundTasks()
    background_tasks.add_task(upstream_response.aclose)
    if fallback_client is not None:
        background_tasks.add_task(fallback_client.aclose)

    # 响应体上限（P1-4 S2）：先按 Content-Length 预检，再由 _limited_stream 兜住无长度声明的流
    try:
        _reject_if_content_length_exceeds(upstream_response)
    except HTTPException:
        await upstream_response.aclose()
        if fallback_client is not None:
            await fallback_client.aclose()
        raise

    return StreamingResponse(
        _limited_stream(upstream_response, upstream_url),
        status_code=upstream_response.status_code,
        headers=response_headers,
        background=background_tasks,
    )


def _scheme_hint(error_text: str, raw_target: str) -> str:
    """TLS 协议错配时给出可操作提示（返回空串表示不是该场景）。

    背景：`_build_proxy_target_url` 对**不带协议**的 target 默认补 `https://`。
    若上游其实是明文 HTTP（常见于 `IP:端口` 形式），握手会报
    `SSL: WRONG_VERSION_NUMBER`，而 Cloudflare 会把源站 502 换成自家的
    「error code: 502」页面，真实原因被藏掉——排查时极易误判成"代理坏了"。

    只认「对方讲的是别的协议」这一类特征。证书校验失败（`SSLCertVerificationError`）
    等其它 TLS 错误不走这条提示——那是另一码事，套用这套说辞反而误导。
    """
    if not any(marker in error_text for marker in _SCHEME_MISMATCH_MARKERS):
        return ""
    if str(raw_target or "").startswith(("http://", "https://")):
        return ""
    return (
        "上游疑似明文 HTTP 却被按 https 访问（target 未带协议时默认补 https）。"
        f"请显式带上协议，例如把 /proxy/{raw_target} 改为 /proxy/http://{raw_target}"
    )
