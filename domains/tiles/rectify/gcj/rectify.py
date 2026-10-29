# -*- coding: utf-8 -*-
"""GCJ-02 ↔ WGS84 瓦片纠偏（同网格同分辨率，精确四角 QUAD 重采样）。

由旧 `gcj_rectify/rectify.py` 拆分而来：网格拉取/拼接/护栏移入
`domains.tiles.rectify.common.grid`，`_quad_warp` 移入同包 `common.quad`，
坐标数学移入 `common.{transform,geo}`；本文件只保留
gcj 双向编排与 gcj 特有的重采样核选择。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional, Tuple

import httpx
from PIL import Image

from domains.tiles.rectify.common.geo import (
    TILE_SIZE,
    gcjbbox_to_wgs_corners,
    image_to_bytes,
    lonlat_to_global_px,
    wgsbbox_to_gcj_corners,
    xyz_to_bbox,
)
from domains.tiles.rectify.common.grid import (
    _fetch_tile_grid,
    _get_tile_cached,
    _merge_tiles,
    _tile_cache_path,
    assess_tile_quality,
    load_output_cache,
    save_output_cache,
    transparent_pct,
)
from domains.tiles.rectify.common.quad import _quad_warp
from domains.tiles.rectify.common.url_template import TileUrlTemplate

logger = logging.getLogger(__name__)

# 纠偏重采样核：BILINEAR（同网格 1:1 下与 BICUBIC 肉眼无差、更快、
# 核半径更小 → 柔化更轻）。如需逐像素锐度优先（bit 级保留源像素），可改
# 为 Image.NEAREST，但几何残差回到 ≤0.5px（四舍五入量化）。
_GCJ_RESAMPLE = Image.BILINEAR


async def _build_rectified_tile(
    source_corners,
    z: int,
    template: TileUrlTemplate,
    cache_dir: Path,
    source_category: str,
    client: Optional[httpx.AsyncClient],
    force_refresh: bool = False,
) -> Tuple[bytes, int, int, float]:
    """由源坐标系下的精确四角构造纠偏瓦片（QUAD 重采样，几何无缝）。

    旧实现只取两角点（LT/RB）+ ``int()`` 截断定位 256 窗口拷贝，带来
    达 1px 的量化台阶与约 −0.5px 系统偏移；现对四角逐点算全局像素
    （float），网格覆盖范围由其极值确定，重采样用共用 ``_quad_warp``。
    共享边端点由同一地理角点算出，相邻瓦片 bit 级一致。

    Args:
        source_corners: ((lt_lon, lt_lat), (rt_lon, rt_lat),
            (lb_lon, lb_lat), (rb_lon, rb_lat)) 源坐标系四角经纬度
        force_refresh: 跳过源瓦片缓存强制重取（输出被判废时用）

    Returns:
        (输出瓦片字节, 源网格瓦片字节总数, 源网格像素总数, 源网格透明像素占比)
        ——后三者供调用方做有效性校验（体积比对 + 「不透明输入不该产出透明输出」）。
    """
    (lt_lon, lt_lat), (rt_lon, rt_lat), (lb_lon, lb_lat), (rb_lon, rb_lat) = source_corners
    px_lt = lonlat_to_global_px(lt_lon, lt_lat, z)
    px_rt = lonlat_to_global_px(rt_lon, rt_lat, z)
    px_lb = lonlat_to_global_px(lb_lon, lb_lat, z)
    px_rb = lonlat_to_global_px(rb_lon, rb_lat, z)
    all_px = (px_lt[0], px_rt[0], px_lb[0], px_rb[0])
    all_py = (px_lt[1], px_rt[1], px_lb[1], px_rb[1])
    limit = 2**z - 1
    x_min = max(0, min(math.floor(min(all_px) / TILE_SIZE), limit))
    x_max = max(0, min(math.floor(max(all_px) / TILE_SIZE), limit))
    y_min = max(0, min(math.floor(min(all_py) / TILE_SIZE), limit))
    y_max = max(0, min(math.floor(max(all_py) / TILE_SIZE), limit))

    tiles = await _fetch_tile_grid(
        x_min,
        x_max,
        y_min,
        y_max,
        z,
        template,
        cache_dir,
        source_category,
        client,
        force_refresh,
    )
    source_bytes_total = sum(len(t) for t in tiles)
    source_pixels = len(tiles) * TILE_SIZE * TILE_SIZE
    composite = _merge_tiles(tiles, x_min, x_max, y_min, y_max)

    def _to_composite(px: float, py: float) -> Tuple[float, float]:
        return (px - x_min * TILE_SIZE, py - y_min * TILE_SIZE)

    quad = (
        _to_composite(*px_lt),  # NW
        _to_composite(*px_lb),  # SW
        _to_composite(*px_rb),  # SE
        _to_composite(*px_rt),  # NE
    )
    # 输出的透明率应当与合成图一致（warp 只从合成图内部取样）。把这个数交给
    # assess_tile_quality 做「多出来的透明」判据，能抓住"某片被空白顶替"却
    # 没过 10% 阈值的情况（线上 12.9% 与 3.5% 两种条带都是这样漏的）。
    src_trans = transparent_pct(composite)
    warped = _quad_warp(composite, quad, _GCJ_RESAMPLE)
    return image_to_bytes(warped), source_bytes_total, source_pixels, src_trans


async def _build_with_quality(
    label: str,
    corners,
    z: int,
    template: TileUrlTemplate,
    cache_dir: Path,
    category: str,
    client: Optional[httpx.AsyncClient],
    x: int,
    y: int,
) -> Tuple[bytes, bool]:
    """构建纠偏瓦片并做有效性校验，不合格则强制重取一次。

    不合格的典型成因是源网格里有瓦片没取到（拼出来大块透明）。这种结果一旦
    落盘/上行就会变成各层缓存 7 天的白斑，所以宁可在这里多花一次上游请求。
    重取仍不合格时只记 error，把数据交给调用方——出口 `_tile_response` 会给它
    打 `no-store` + `X-Tile-Blank: 1`，不落盘、不进缓存。
    """
    tile_bytes, src_total, src_px, src_trans = await _build_rectified_tile(
        corners, z, template, cache_dir, category, client
    )
    ok, why = assess_tile_quality(
        tile_bytes, source_bytes_total=src_total, source_pixels=src_px,
        source_transparent_pct=src_trans,
    )
    if ok:
        return tile_bytes, True

    logger.warning("%s 输出不合格(%s)，强制重取：z=%d x=%d y=%d", label, why, z, x, y)
    tile_bytes, src_total, src_px, src_trans = await _build_rectified_tile(
        corners, z, template, cache_dir, category, client, force_refresh=True
    )
    ok, why = assess_tile_quality(
        tile_bytes, source_bytes_total=src_total, source_pixels=src_px,
        source_transparent_pct=src_trans,
    )
    if not ok:
        logger.error("%s 重取后仍不合格(%s)：z=%d x=%d y=%d", label, why, z, x, y)
    # 把结论一起交出去：调用方据此决定「不落盘」，出口据此打 no-store
    return tile_bytes, ok


async def get_gcj2wgs_tile(
    x: int,
    y: int,
    z: int,
    template: TileUrlTemplate,
    cache_dir: Path,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[bytes, bool]:
    """Return a WGS tile built from GCJ tiles.

    GCJ-02 -> WGS84 纠偏流程：
    1. 检查输出缓存，命中则直接返回
    2. z <= 9 时直接返回源瓦片（低缩放级别偏差可忽略）
    3. z > 9 时执行像素级纠偏
    """
    output_path = _tile_cache_path(cache_dir, template, "gcj2wgs2", z, x, y)

    # 缓存命中：直接返回字节（空白产物由 load_output_cache 判定为无效并清理）
    cached = load_output_cache(output_path)
    if cached is not None:
        return cached, True

    # 低缩放级别：偏差可忽略，直接返回源瓦片（源片已过第一层校验）
    if z <= 9:
        tile_bytes = await _get_tile_cached(
            x, y, z, template, cache_dir, "source-gcj", client=client
        )
        save_output_cache(output_path, tile_bytes)
        return tile_bytes, True

    # 高缩放级别：执行像素级纠偏（四角逐点转换，勿取极值压扁）
    wgs_bbox = xyz_to_bbox(x, y, z)
    gcj_corners = wgsbbox_to_gcj_corners(wgs_bbox)
    tile_bytes, ok = await _build_with_quality(
        "gcj2wgs", gcj_corners, z, template, cache_dir, "source-gcj", client, x, y
    )
    if ok:
        save_output_cache(output_path, tile_bytes)
    return tile_bytes, ok


async def get_wgs2gcj_tile(
    x: int,
    y: int,
    z: int,
    template: TileUrlTemplate,
    cache_dir: Path,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[bytes, bool]:
    """Return a GCJ tile built from WGS tiles.

    WGS84 -> GCJ-02 纠偏流程：
    1. 检查输出缓存，命中则直接返回
    2. z <= 9 时直接返回源瓦片（低缩放级别偏差可忽略）
    3. z > 9 时执行像素级纠偏
    """
    output_path = _tile_cache_path(cache_dir, template, "wgs2gcj2", z, x, y)

    # 缓存命中：直接返回字节（空白产物由 load_output_cache 判定为无效并清理）
    cached = load_output_cache(output_path)
    if cached is not None:
        return cached, True

    # 低缩放级别：偏差可忽略，直接返回源瓦片（源片已过第一层校验）
    if z <= 9:
        tile_bytes = await _get_tile_cached(
            x, y, z, template, cache_dir, "source-wgs", client=client
        )
        save_output_cache(output_path, tile_bytes)
        return tile_bytes, True

    # 高缩放级别：执行像素级纠偏（四角逐点转换，勿取极值压扁）
    gcj_bbox = xyz_to_bbox(x, y, z)
    wgs_corners = gcjbbox_to_wgs_corners(gcj_bbox)
    tile_bytes, ok = await _build_with_quality(
        "wgs2gcj", wgs_corners, z, template, cache_dir, "source-wgs", client, x, y
    )
    if ok:
        save_output_cache(output_path, tile_bytes)
    return tile_bytes, ok
