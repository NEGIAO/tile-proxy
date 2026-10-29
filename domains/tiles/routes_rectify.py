# -*- coding: utf-8 -*-
"""纠偏瓦片路由：/proxy/gcj2wgs、/proxy/wgs2gcj、/proxy/bd2wgs、/proxy/wgs2bd。

内容整体搬自旧 `api/proxy.py`，逻辑逐字保留；1.1.0 起四个端点共用
`_serve_tile`（解析 → 内存缓存 → 调构建函数 → 出口），避免四份重复的
错误码映射与 finally 收尾。

⚠️ 注册顺序约束：本路由必须先于通用流式代理
（`routes_passthrough.universal_stream_proxy` 的 `/proxy/{target_url:path}`）
挂载，否则纠偏路径会被通配路由吞掉。顺序由 `domains/tiles/__init__.py` 保证。
"""

import logging
from typing import Awaitable, Callable, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from PIL import Image

from config import max_source_zoom
from domains.tiles.notices import notice_response
from domains.tiles.proxy_shared import (
    _build_proxy_target_url,
    _rate_limit_check,
    _tile_cache,
    build_http_client,
)
from domains.tiles.rectify import (
    get_bd2wgs_tile,
    get_cache_dir,
    get_gcj2wgs_tile,
    get_wgs2bd_tile,
    get_wgs2gcj_tile,
    parse_tile_url,
)
from domains.tiles.rectify.common.grid import assess_tile_quality
from domains.tiles.rectify.common.url_template import TileUrlTemplate

logger = logging.getLogger(__name__)

router = APIRouter()

# 源瓦片的最大层级：超过它的请求上游必然没有数据（高德/百度矢量 18，百度 18），
# 直接返回「级别过大」提示图，不去打上游。0 = 关闭该检查。
# 注意：高德**卫星**在 z19 仍有影像，用卫星底图时可设成 19 或 0。
# 每次请求实时读（管理页可改，改完立刻生效），env GCJRE_MAX_SOURCE_ZOOM 作兜底。
def _max_source_zoom() -> int:
    return max_source_zoom()

# 构建函数签名：(xyz, template, cache_dir, client) -> 瓦片字节
TileBuilder = Callable[[object, TileUrlTemplate, object, Optional[httpx.AsyncClient]], Awaitable[bytes]]


def _tile_response(
    data: bytes,
    media_type: str = "image/png",
    *,
    ok: Optional[bool] = None,
) -> Response:
    """纠偏瓦片的统一出口。

    不合格的瓦片（大块透明 / 体积远小于源）打 `no-store` + `X-Tile-Blank: 1`：
      * 浏览器/CF 不再把它缓存 7 天（cache_policy 中间件认这个标记）；
      * nginx 侧 `proxy_no_cache $upstream_http_x_tile_blank` 同样跳过缓存。
    这样即便重取后仍不合格，下一次请求也会重算，而不是白一整个缓存周期。

    `ok` 是调用方已算好的质量结论；传 None 才在这里现算（避免同一张图
    在"计算 → 落盘 → 出口"链路上被反复解码）。
    """
    if ok is None:
        ok, _why = assess_tile_quality(data)
    if not ok:
        return Response(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "no-store", "X-Tile-Blank": "1"},
        )
    return Response(content=data, media_type=media_type)


def _finish_tile(cache_key: str, label: str, xyz, data: bytes, ok: Optional[bool] = None) -> Response:
    """校验 → 合格才写内存缓存 → 统一出口。

    `ok` 由构建函数（`_build_with_quality` / bd 的循环）算出，带上了「与源网格
    比较透明率」这条信息——那是出口这边拿不到的。传进来就直接用，省一次解码；
    传 None（z≤9 直通、缓存命中）才在这里现算。

    不合格的瓦片不进内存缓存：否则一次上游抖动会让同一块白斑在进程内
    继续被命中 5 分钟（`PROXY_TILE_CACHE_TTL_SECONDS`），重试也没用。
    """
    if ok is None:
        ok, why = assess_tile_quality(data)
    else:
        why = "构建期已判废" if not ok else "ok"
    if ok:
        _tile_cache.set(cache_key, data, "image/png")
        logger.debug(
            "瓦片缓存写入 [%s z=%d/x=%d/y=%d] 当前缓存: %d 条目",
            label, xyz.z, xyz.x, xyz.y, _tile_cache.size,
        )
    else:
        logger.warning(
            "瓦片不合格(%s)，跳过内存缓存 [%s z=%d/x=%d/y=%d]",
            why, label, xyz.z, xyz.x, xyz.y,
        )
    return _tile_response(data, ok=ok)


def _resolve_gcj_cache_dir(request: Request):
    cache_dir = getattr(request.app.state, "gcj_rectify_cache_dir", None)
    if cache_dir is None:
        cache_dir = get_cache_dir()
        request.app.state.gcj_rectify_cache_dir = cache_dir
    return cache_dir


def _resolve_gcj_http_client(request: Request) -> Tuple[httpx.AsyncClient, bool]:
    shared_client = getattr(request.app.state, "http_client", None)
    if shared_client is not None:
        return shared_client, False
    fallback_client = build_http_client()
    return fallback_client, True


async def _serve_tile(
    request: Request,
    target_url: str,
    *,
    label: str,
    cache_prefix: str,
    builder: TileBuilder,
) -> Response:
    """纠偏/直出瓦片的公共流程：解析 → 内存缓存 → 构建 → 出口。

    Args:
        request: 原始请求（取 query、app.state 上的客户端与缓存目录）
        target_url: 路径里捕获的上游 URL（可能不含协议，由 `_build_proxy_target_url` 补全）
        label: 日志与内存缓存键里的短名，如 `gcj2wgs`
        cache_prefix: 内存缓存键前缀（与 label 分开：便于后续换实现时不冲掉旧缓存）
        builder: 实际产出瓦片字节的协程函数
    """
    upstream_url = _build_proxy_target_url(
        target_url, request.url.query,
        referer=request.headers.get("referer", ""),
    )
    try:
        template, xyz = parse_tile_url(upstream_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 缓存诊断探针：返回一张「200 + no-store + X-Tile-Blank」的提示图，用来验证
    # 上游 CDN 到底认不认 no-store（2026-10-29 实测：某 CF 缓存规则会无视它，
    # 见 docs/deployment.md「CDN 与提示瓦片」）。默认不可达，只有显式带
    # `?__cache_probe=1` 才会命中，正常请求完全不受影响。
    _probe = request.query_params.get("__cache_probe")
    if _probe:
        # `?__cache_probe=1` → 200；`?__cache_probe=203` → 203（用来对比 CDN 是否缓存）
        return notice_response(
            "error2", status_code=203 if _probe == "203" else 200,
            cache_control="no-store, no-cache, must-revalidate, max-age=0",
            headers={"X-Tile-Blank": "1", "X-Tile-NoCache": "1", "X-Tile-Notice": "cache-probe"},
        )

    # 层级过大：确定性结果，直接给提示图（200 + 可缓存），不消耗上游请求。
    _zoom_limit = _max_source_zoom()
    if _zoom_limit > 0 and xyz.z > _zoom_limit:
        logger.debug("层级超出上限，返回提示图 [%s z=%d]", label, xyz.z)
        return notice_response("error2", status_code=200)

    cache_key = f"{cache_prefix}:{template.cache_key}:{xyz.z}/{xyz.x}/{xyz.y}"
    cached = _tile_cache.get(cache_key)
    if cached:
        logger.debug("瓦片缓存命中 [%s z=%d/x=%d/y=%d]", label, xyz.z, xyz.x, xyz.y)
        # 内存缓存只收合格瓦片（见 _finish_tile），命中无需再解码校验
        return _tile_response(cached.content, cached.media_type, ok=True)

    cache_dir = _resolve_gcj_cache_dir(request)
    client, should_close = _resolve_gcj_http_client(request)
    try:
        result = await builder(xyz, template, cache_dir, client)
        # 构建函数可以只返回 bytes，也可以返回 (bytes, ok) 把第一/二层的结论带出来
        if isinstance(result, tuple):
            tile_bytes, quality_ok = result
        else:
            tile_bytes, quality_ok = result, None
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="纠偏请求超时") from exc
    except ValueError as exc:
        # 资源护栏拒绝（瓦片字节/网格数超上限，P1-4 S2）属请求问题 → 400 而非 502
        raise HTTPException(status_code=400, detail=f"纠偏请求被拒绝: {exc}") from exc
    except Image.DecompressionBombError as exc:
        raise HTTPException(status_code=400, detail="上游瓦片像素超上限，已拒绝解码") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"纠偏失败: {exc}") from exc
    finally:
        if should_close:
            await client.aclose()

    return _finish_tile(cache_key, label, xyz, tile_bytes, quality_ok)


# ==================== GCJ 瓦片纠偏代理 ====================
@router.get("/proxy/gcj2wgs/{target_url:path}")
async def gcj2wgs_proxy(target_url: str, request: Request, _: None = Depends(_rate_limit_check)):
    """GCJ02 -> WGS84 瓦片纠偏代理"""
    return await _serve_tile(
        request, target_url,
        label="gcj2wgs", cache_prefix="gcj2wgs",
        builder=lambda xyz, template, cache_dir, client: get_gcj2wgs_tile(
            xyz.x, xyz.y, xyz.z, template, cache_dir, client=client
        ),
    )


@router.get("/proxy/wgs2gcj/{target_url:path}")
async def wgs2gcj_proxy(target_url: str, request: Request, _: None = Depends(_rate_limit_check)):
    """WGS84 -> GCJ02 瓦片纠偏代理"""
    return await _serve_tile(
        request, target_url,
        label="wgs2gcj", cache_prefix="wgs2gcj",
        builder=lambda xyz, template, cache_dir, client: get_wgs2gcj_tile(
            xyz.x, xyz.y, xyz.z, template, cache_dir, client=client
        ),
    )


# ==================== 百度 BD-09 瓦片纠偏代理 ====================
# 百度瓦片使用独立 BD09MC 网格（居中原点、Y 轴向上、res=2^(18-z)），
# 与标准 XYZ 网格既不平移等价也不分辨率等价（z_bd ≈ z±1），
# 纠偏必须跨网格拉源重采样，实现见 domains/tiles/rectify/。
# 与 gcj 路由同契入式契约：URL 模板中 {x}{y}{z} 由客户端按其工作网格填充。


@router.get("/proxy/bd2wgs/{target_url:path}")
async def bd2wgs_proxy(target_url: str, request: Request, _: None = Depends(_rate_limit_check)):
    """百度 BD-09 → WGS84 瓦片纠偏代理。

    客户端以标准 XYZ 索引填充百度 URL 模板，返回同索引的
    WGS84 对齐瓦片（源为对应区域的百度瓦片网格）。
    """
    return await _serve_tile(
        request, target_url,
        label="bd2wgs", cache_prefix="bd2wgs",
        builder=lambda xyz, template, cache_dir, client: get_bd2wgs_tile(
            xyz.x, xyz.y, xyz.z, template, cache_dir, client=client
        ),
    )


@router.get("/proxy/wgs2bd/{target_url:path}")
async def wgs2bd_proxy(target_url: str, request: Request, _: None = Depends(_rate_limit_check)):
    """WGS84 → 百度 BD-09 瓦片纠偏代理。

    客户端工作在百度网格空间（如原生加载百度底图叠加 WGS 图源），
    传入 WGS 图源 URL 模板、以百度网格索引填充 {x}{y}{z}，
    返回同索引、内容重采样自 WGS 源的百度网格瓦片。
    """
    return await _serve_tile(
        request, target_url,
        label="wgs2bd", cache_prefix="wgs2bd",
        builder=lambda xyz, template, cache_dir, client: get_wgs2bd_tile(
            xyz.x, xyz.y, xyz.z, template, cache_dir, client=client
        ),
    )
