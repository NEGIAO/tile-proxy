# -*- coding: utf-8 -*-
"""源瓦片网格拉取 / 拼接 / 缓存（gcj/bd 双向共用）。

内容整体搬自旧 `gcj_rectify/rectify.py`（并发、重试、字节/网格护栏、
文件缓存、合成逻辑均原样保留），仅 import 前缀改为本包。
"""

from __future__ import annotations

import asyncio
import logging
import time
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from PIL import Image

from config import get_int

from domains.tiles.rectify.common.fetch import fetch_tile
from domains.tiles.rectify.common.geo import TILE_SIZE, bytes_to_image, image_to_bytes
from domains.tiles.rectify.common.url_template import TileUrlTemplate, build_tile_url

logger = logging.getLogger(__name__)

# 并发数：原为 100（注释自述"实际不限制"）；P1-4 S2 收紧至 16，
# 防单个纠偏请求打爆上游与本机 fd（重试机制仍由 fetch.py 处理限流错误）
MAX_CONCURRENCY = get_int("GCJRE_MAX_CONCURRENCY", 16)

# 全局回源并发闸门（跨请求共享）。原先是每个 `_fetch_tile_grid` 各建一个
# `Semaphore(MAX_CONCURRENCY)`：一屏 30 张输出同时算时，能一次打出去 30×16=480 路，
# 实际只靠 httpx 连接池（`PROXY_MAX_CONNECTIONS`，默认 80）兜底——既不可控，也容易
# 把上游惹毛触发限流。改成模块级后，整个进程同一时刻最多 MAX_CONCURRENCY 路回源。
_GLOBAL_FETCH_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _get_fetch_semaphore() -> asyncio.Semaphore:
    """惰性创建全局回源信号量（避免在 import 阶段绑定事件循环）。"""
    global _GLOBAL_FETCH_SEMAPHORE
    if _GLOBAL_FETCH_SEMAPHORE is None:
        _GLOBAL_FETCH_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENCY)
    return _GLOBAL_FETCH_SEMAPHORE

# --- SSRF/资源耗尽护栏（P1-4 S2；方案 Docs/TODO/proxy-ssrf-hardening-plan.md）---
# 单瓦片字节上限：上游返回超大图时直接判废，避免进入解码
_TILE_MAX_BYTES = max(0, get_int("GCJRE_TILE_MAX_MB", 8)) * 1024 * 1024
# 解码像素上限：Pillow 默认 MAX_IMAGE_PIXELS 仅告警不拦截 → 显式收紧为硬上限，防解压炸弹
_MAX_IMAGE_PIXELS = get_int("GCJRE_MAX_IMAGE_PIXELS", 4096 * 4096)
# 单请求合成网格瓦片数上限：合成图为 (nx*256)×(ny*256)，无上限时内存随请求参数放大
_MAX_TILES_PER_REQUEST = get_int("GCJRE_MAX_TILES_PER_REQUEST", 64)

# --- 空白瓦片自愈（2026-10-29 线上事故后加）---
# 上游（高德）会间歇性返回纯色/空白瓦片，或直接超时。旧实现遇到取瓦片异常时用
# 空白图顶替，拼出来的输出就是一整块透明 PNG，随后被写进磁盘缓存（7 天）、
# nginx（7 天）、CF 边缘（7 天）——用户看到的就是一块永久白斑。
# 现在三管齐下：
#   1) 取瓦片失败重试 _FETCH_ATTEMPTS 次，仍失败就抛错（路由返回 5xx，不会被缓存）；
#   2) 空白缓存视为"软命中"，超过 _BLANK_RETRY_SECONDS 就重新向上游要一次；
#   3) 空白输出不落盘、不打长 TTL，下次请求立即重算。
_BLANK_RETRY_SECONDS = get_int("GCJRE_BLANK_RETRY_SECONDS", 600)
_FETCH_ATTEMPTS = max(1, get_int("GCJRE_FETCH_ATTEMPTS", 3))
# 输出可信度阈值：透明像素占比超过这个百分比就判定"缺片"，触发强制重取。
# 正常纠偏输出是全不透明的（alpha 恒 255）；出现大块透明说明源网格有瓦片没取到。
_MAX_TRANSPARENT_PERCENT = max(1, min(get_int("GCJRE_MAX_TRANSPARENT_PERCENT", 10), 100))
# 输出体积与源瓦片的比值下限（字节/像素 之比）。纠偏是裁剪不是缩放，
# 输出应当与源同量级；低于该比例说明输出丢了细节（纯色/纯透明）。
_MIN_DETAIL_RATIO = 0.2
# 输出透明率允许比源网格多出的百分点。几何上 warp 只从合成图内部取样，
# 输出的透明区域应当与源网格的透明区域**一致**；多出来的透明就是缺片/漏边。
# 不能用「源是否全不透明」做开关：实测 13241 个源片里有 395 个带 1~13 个透明像素
# （高德 PNG 的边缘/水印），布尔判断会被它们轻易绕过，3.5% 的条带照样漏网。
_MAX_EXTRA_TRANSPARENT_PCT = 1.0

if _MAX_IMAGE_PIXELS > 0:
    # Pillow 的全局阈值：超过即抛 DecompressionBombError（而非仅 warning）
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS


def _reject_oversized_tile_bytes(data: bytes, url: str) -> None:
    """瓦片字节数超上限时抛 ValueError（由路由层转 400/502）。

    参数：data —— 上游返回字节；url —— 仅用于日志定位。无返回。
    """
    if _TILE_MAX_BYTES > 0 and data is not None and len(data) > _TILE_MAX_BYTES:
        logger.warning("纠偏瓦片超字节上限已拒绝：%s（%d > %d）", url, len(data), _TILE_MAX_BYTES)
        raise ValueError(
            f"上游瓦片过大：{len(data)} 字节 > 上限 {_TILE_MAX_BYTES} 字节"
        )


def _tile_cache_path(
    cache_dir: Path,
    template: TileUrlTemplate,
    category: str,
    z: int,
    x: int,
    y: int,
) -> Path:
    return cache_dir / template.cache_key / category / str(z) / str(x) / f"{y}.png"


def _save_tile_bytes(tile_path: Path, tile_bytes: bytes) -> None:
    """保存瓦片字节到缓存文件"""
    tile_path.parent.mkdir(parents=True, exist_ok=True)
    tile_path.write_bytes(tile_bytes)


def is_blank_tile_bytes(data: bytes, *, max_bytes: int = 2048) -> bool:
    """判断字节是否为「全透明空瓦片」。

    真实影像的 256×256 PNG 普遍 >2KB，而全透明 PNG 压完只有 300~500 字节，
    所以先用体积做前置过滤，避免每块瓦片都走解码。仅用于识别"整块全透明"，
    部分透明的瓦片（例如影像覆盖边缘）不算空白——那是上游真实的覆盖范围。
    """
    if not data or len(data) > max_bytes:
        return False
    try:
        with Image.open(BytesIO(data)) as im:
            # 调色板图必须真的带透明索引才算候选，否则 convert 后 alpha 恒 255
            if im.mode == "P" and "transparency" not in im.info:
                return False
            return im.convert("RGBA").getchannel("A").getextrema() == (0, 0)
    except Exception:
        return False


def transparent_pct(image: "Image.Image") -> float:
    """统计一张 RGBA 图里非全不透明像素的占比（%）。用 histogram，C 层实现，快。"""
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    alpha = rgba.getchannel("A")
    total = max(1, rgba.width * rgba.height)
    return 100.0 * sum(alpha.histogram()[:255]) / total


def assess_tile_quality(
    data: bytes,
    *,
    source_bytes_total: int = 0,
    source_pixels: int = 0,
    source_transparent_pct: float = -1.0,
) -> Tuple[bool, str]:
    """评估一张纠偏输出是否可信，返回 (是否合格, 不合格原因)。

    三条判据（任一条不过即判废，由调用方强制重取一次）：

    1. **透明占比**：正常输出是全不透明的；大块透明 = 源网格里有瓦片没取到。
       这是最主要的判据——线上那两张白斑瓦片分别是 100% 和 93.8% 透明。
    2. **体积与源瓦片比对**：纠偏是「裁剪+重采样」不是缩放，输出的字节/像素
       应与源同量级。输出远小于源说明内容被抹平了（纯色、纯透明）。
       需要 `source_bytes_total` 与 `source_pixels` 同时给出（后者为源网格
       真实像素数，由调用方按 `片数 × TILE_SIZE²` 传入），缺一个就跳过该判据。
    3. **解码失败**：连图都不是，直接判废。

    另有一条**不变量**（`source_transparent_pct >= 0` 时生效）：输出的透明率不能
    比源网格多出 `_MAX_EXTRA_TRANSPARENT_PCT` 个百分点。几何上 warp 只从合成图
    内部取样，输出的透明区域应当与源一致；多出来的透明就是有源片被空白顶替
    （线上 z17/x=107151/y=51994 右侧 12.9%、z11/x=1676/y=811 右侧 3.5% 的透明带
    都是这种，透明率没到 10% 阈值，于是被当成好片缓存了）。

    注意：**不透明白色不算废**——那是上游真实的覆盖边界（卫星影像范围外），
    误判会让空白区域陷入无限重试。只认「透明」这个确定性信号。
    """
    if not data:
        return False, "empty"

    try:
        with Image.open(BytesIO(data)) as im:
            w, h = im.size
            rgba = im.convert("RGBA")
    except Exception as e:  # noqa: BLE001 - 解码失败一律判废
        return False, "decode: %r" % (e,)

    total_px = max(1, w * h)
    alpha = rgba.getchannel("A")
    lo, _hi = alpha.getextrema()
    if lo == 0:
        # 口径必须与源侧一致（transparent_pct 数的是 alpha<255，这里也一样）：
        # 只数 alpha==0 会漏掉 BILINEAR 在透明边缘混出来的半透明像素。
        pct = transparent_pct(rgba)
        if source_transparent_pct >= 0 and pct > source_transparent_pct + _MAX_EXTRA_TRANSPARENT_PCT:
            return False, "输出透明 %.1f%% 比源 %.2f%% 多（缺片/漏边）" % (pct, source_transparent_pct)
        if pct > _MAX_TRANSPARENT_PERCENT:
            return False, "transparent %.1f%% > %d%%" % (pct, _MAX_TRANSPARENT_PERCENT)

    if source_bytes_total > 0 and source_pixels > 0:
        # 两侧都归一成「字节/像素」再比：输出用自身像素数，源用源网格真实像素数。
        # 若图省事拿源字节直接除以输出像素，2×2 网格会把源的 B/px 放大 4 倍，
        # 阈值随之抬高 4 倍——海面/沙漠/纯色街区这类正常但极好压的瓦片会被
        # 误判成"丢细节"，陷入每次请求强制重取 + 永不缓存的死循环。
        out_bpp = len(data) / float(total_px)
        src_bpp = source_bytes_total / float(source_pixels)
        if src_bpp > 0 and out_bpp < src_bpp * _MIN_DETAIL_RATIO:
            return False, "detail %.3f B/px < %.3f×源 %.3f" % (
                out_bpp, _MIN_DETAIL_RATIO, src_bpp)

    return True, "ok"


def load_output_cache(output_path: Path) -> Optional[bytes]:
    """读纠偏输出缓存；不合格产物视为无效（删除并返回 None，让调用方重算）。

    历史 bug 把空白/大块透明的输出当成有效结果落了盘，这些文件要主动清掉，
    否则会一直被当作"已纠偏"直接喂给用户。判据与出口一致，见 assess_tile_quality。
    """
    if not output_path.exists():
        return None
    try:
        cached = output_path.read_bytes()
    except OSError:
        return None
    ok, why = assess_tile_quality(cached)
    if ok:
        return cached
    try:
        output_path.unlink()
        logger.warning("清理不合格的纠偏产物(%s)：%s", why, output_path)
    except OSError:
        pass
    return None


def save_output_cache(output_path: Path, data: bytes) -> None:
    """写纠偏输出缓存；不合格产物不落盘（否则会变成 7 天白斑）。"""
    ok, why = assess_tile_quality(data)
    if not ok:
        logger.warning("纠偏输出不合格(%s)，跳过落盘（下次请求重算）：%s", why, output_path)
        return
    _save_tile_bytes(output_path, data)


# ==================== 第一层校验：源瓦片 ====================
class SourceTileError(RuntimeError):
    """源瓦片不合格（空响应 / 过小 / 无法解码 / 全透明空白）。"""


# 一张有效瓦片的字节下限：纯色 PNG 约 200B，全透明约 300B；低于这个数基本是
# 错误页或截断响应。
_SOURCE_MIN_BYTES = 100


def _validate_source_tile(data: bytes, url: str) -> None:
    """校验**单个源瓦片**能否用于合成（第一层判断，最关键的一层）。

    四张源片里只要有一张是坏的，拼出来的输出就会缺一块（透明条带），而且会被
    磁盘 / nginx / CDN 逐层放大。所以在**落盘之前**逐张把关：

    1. 非空、字节数不低于 `_SOURCE_MIN_BYTES`（错误页 / 截断响应）
    2. 能解码、尺寸合理（`Image.verify()` 抓截断与损坏；宽高 >= 32）
    3. 不是「全透明空白片」——高德对无覆盖区返回的是**不透明白色/纯色**瓦片
       （2026-10-29 抽查 z3/z5/z10/z13/z17 五个海洋与边缘瓦片，alpha 全 255），
       所以全透明片只可能是上游抖动，不能当数据用。

    不合格抛 `SourceTileError`，由 `_fetch_tile_grid` 重试（重试会绕过缓存）。
    """
    if not data:
        raise SourceTileError("空响应：%s" % url)
    if len(data) < _SOURCE_MIN_BYTES:
        raise SourceTileError("响应过小（%d 字节）：%s" % (len(data), url))
    try:
        with Image.open(BytesIO(data)) as im:
            w, h = im.size
            im.verify()
    except Exception as e:  # noqa: BLE001 - 任何解码失败都判不合格
        raise SourceTileError("无法解码（%r）：%s" % (e, url))
    # 必须是 256×256：`_merge_tiles` 是按网格位置直接 paste 的，尺寸不符会静默错位
    if (w, h) != (TILE_SIZE, TILE_SIZE):
        raise SourceTileError(
            "尺寸异常 %dx%d（应为 %dx%d）：%s" % (w, h, TILE_SIZE, TILE_SIZE, url)
        )
    # 全透明判据直接看 alpha——不能用 is_blank_tile_bytes 的 2KB 体积预筛：
    # 带元数据的空白 PNG 会超过 2KB 而漏检。
    try:
        with Image.open(BytesIO(data)) as im:
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                if im.convert("RGBA").getchannel("A").getextrema() == (0, 0):
                    raise SourceTileError("全透明空白片：%s" % url)
    except SourceTileError:
        raise
    except Exception as e:  # noqa: BLE001
        raise SourceTileError("无法解码（%r）：%s" % (e, url))


# 同一源片的并发请求只打一次上游（单飞）。瓦片是连续的：一屏几十张输出瓦片
# 共享同一批源片，没有这把锁时实测「16 张并发输出 → 25 张源片被请求 186 次」；
# 有了它降到 25 次（理论最优）。
_SOURCE_LOCKS: Dict[str, asyncio.Lock] = {}
_SOURCE_LOCKS_MAX = 20000


def _read_cached_tile(tile_path: Path) -> Optional[bytes]:
    """读源片缓存。空白缓存只算「软命中」——超过 `_BLANK_RETRY_SECONDS` 就重取，
    上游恢复后能自动覆盖，不至于让一次抖动钉死一整个缓存周期。"""
    if not tile_path.exists():
        return None
    try:
        cached = tile_path.read_bytes()
    except OSError:
        return None
    if not is_blank_tile_bytes(cached):
        return cached
    try:
        age = time.time() - tile_path.stat().st_mtime
    except OSError:
        age = 0
    if age <= _BLANK_RETRY_SECONDS:
        return cached
    logger.info("空白瓦片缓存已超 %d 秒，重新向上游取：%s", _BLANK_RETRY_SECONDS, tile_path)
    return None


async def _get_tile_cached(
    x: int,
    y: int,
    z: int,
    template: TileUrlTemplate,
    cache_dir: Path,
    category: str,
    *,
    client: Optional[httpx.AsyncClient],
    force_refresh: bool = False,
) -> bytes:
    """取单个源瓦片：缓存 → 单飞 → 拉取 → **第一层校验** → 落盘。

    缓存命中直接返回文件字节，省掉解码/编码；`force_refresh=True` 时跳过缓存
    （用于输出被判废后的强制重取）。同一源片的并发请求走同一把锁，只打一次上游。
    """
    tile_path = _tile_cache_path(cache_dir, template, category, z, x, y)

    if not force_refresh:
        cached = _read_cached_tile(tile_path)
        if cached is not None:
            return cached

    key = str(tile_path)
    lock = _SOURCE_LOCKS.get(key)
    if lock is None:
        if len(_SOURCE_LOCKS) >= _SOURCE_LOCKS_MAX:
            _SOURCE_LOCKS.clear()
        lock = _SOURCE_LOCKS[key] = asyncio.Lock()

    async with lock:
        # 二次检查：并发里的另一路可能刚刚把这张源片取回来并落盘
        if not force_refresh:
            cached = _read_cached_tile(tile_path)
            if cached is not None:
                return cached

        url = build_tile_url(template, x, y, z)
        tile_bytes = await fetch_tile(url, client=client)
        # P1-4 S2：先判字节上限，再进入解码/落盘（拒绝路径不写缓存）
        _reject_oversized_tile_bytes(tile_bytes, url)
        # ★ 第一层判断：源片本身必须可用，坏片绝不进合成图、也绝不落盘
        _validate_source_tile(tile_bytes, url)

        try:
            if _is_valid_image_bytes(tile_bytes):
                _save_tile_bytes(tile_path, tile_bytes)
            else:
                png_bytes = image_to_bytes(bytes_to_image(tile_bytes))
                _save_tile_bytes(tile_path, png_bytes)
                return png_bytes
        except (OSError, IOError) as e:
            logger.warning("Failed to save tile to cache: %s", e)
        except Exception as e:
            logger.error("Unexpected error during tile validation: %s", e, exc_info=True)
        return tile_bytes


def _is_valid_image_bytes(data: bytes) -> bool:
    """快速检查字节是否为有效图像（通过魔数判断）

    支持的格式：PNG, JPEG, WebP, GIF

    Note:
        WebP 格式需要至少 12 字节（RIFF + 4字节大小 + WEBP）
    """
    # PNG 魔数: 89 50 4E 47 (4字节)
    if len(data) >= 4 and data[:4] == b'\x89PNG':
        return True
    # JPEG 魔数: FF D8 FF (3字节)
    if len(data) >= 3 and data[:3] == b'\xff\xd8\xff':
        return True
    # GIF 魔数: GIF87a 或 GIF89a (6字节)
    if len(data) >= 6 and data[:6] in (b'GIF87a', b'GIF89a'):
        return True
    # WebP 魔数: RIFF....WEBP (需要12字节)
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return True
    return False


async def _fetch_tile_grid(
    x_min: int,
    x_max: int,
    y_min: int,
    y_max: int,
    z: int,
    template: TileUrlTemplate,
    cache_dir: Path,
    category: str,
    client: Optional[httpx.AsyncClient],
    force_refresh: bool = False,
) -> List[bytes]:
    """并发获取瓦片网格（受全局信号量 `MAX_CONCURRENCY` 限流）

    并发闸门是**进程级**的：所有请求共享同一批槽位，避免多张输出并发时
    把上游打爆。限流错误仍由 fetch.py 的重试机制处理（RemoteProtocolError）。

    **两层判断里的第一层就在这条路径上**：每张源片先过 `_validate_source_tile`
    （空响应/过小/无法解码/全透明），不合格立刻抛错；这里重试 `_FETCH_ATTEMPTS`
    次（重试绕过缓存），仍失败就整块抛错 → 路由返回 5xx（各层都不缓存），
    绝不拿坏片去合成（那会产出被缓存 7 天的透明条带）。
    """
    semaphore = _get_fetch_semaphore()

    async def _run(x: int, y: int) -> bytes:
        async with semaphore:
            last_exc: Optional[Exception] = None
            for attempt in range(_FETCH_ATTEMPTS):
                try:
                    # 第一层校验（_validate_source_tile）不合格会抛错；重试时
                    # 绕过缓存重取——缓存里那份很可能就是刚被判废的坏片。
                    return await _get_tile_cached(
                        x, y, z, template, cache_dir, category,
                        client=client,
                        force_refresh=force_refresh or attempt > 0,
                    )
                except Exception as e:  # noqa: BLE001 - 重试用尽后统一抛出
                    last_exc = e
                    if attempt + 1 < _FETCH_ATTEMPTS:
                        await asyncio.sleep(0.4 * (attempt + 1))
            # 关键：绝不用空白图顶替。空白会被拼成"全透明瓦片"并被各层缓存 7 天，
            # 用户看到的就是一块永久白斑（2026-10-29 线上复现）。宁可整块返回 5xx——
            # nginx 只缓存 200、CF 不缓存 5xx、浏览器会重试，下一次请求自然重来。
            logger.warning(
                "Failed to fetch tile %s/z=%d/x=%d/y=%d after %d attempts: %s",
                category, z, x, y, _FETCH_ATTEMPTS, repr(last_exc),
            )
            assert last_exc is not None
            raise last_exc

    # 计算瓦片数量
    x_count = x_max - x_min + 1
    y_count = y_max - y_min + 1
    total_tiles = x_count * y_count

    # P1-4 S2：网格瓦片数上限——合成图为 (x_count*256)×(y_count*256)，
    # 无上限时内存占用随请求间接放大（正常纠偏为 2×2~3×3，默认 64 留足余量）
    if _MAX_TILES_PER_REQUEST > 0 and total_tiles > _MAX_TILES_PER_REQUEST:
        raise ValueError(
            f"纠偏网格过大：{x_count}x{y_count}={total_tiles} 片 > 上限 {_MAX_TILES_PER_REQUEST} 片"
        )

    if total_tiles > 9:
        logger.info("Fetching %d tiles for %s z=%d (grid: %dx%d)", total_tiles, category, z, x_count, y_count)

    # 创建所有任务（无延迟，无并发限制）
    tasks = [
        asyncio.create_task(_run(ax, ay))
        for ax in range(x_min, x_max + 1)
        for ay in range(y_min, y_max + 1)
    ]

    return await asyncio.gather(*tasks)


def _merge_tiles(
    tiles: List[bytes],
    x_min: int,
    x_max: int,
    y_min: int,
    y_max: int,
) -> Image.Image:
    """拼接标准 XYZ 网格瓦片（Y 轴向下，行序与索引升序一致）。"""
    composite = Image.new(
        "RGBA",
        ((x_max - x_min + 1) * TILE_SIZE, (y_max - y_min + 1) * TILE_SIZE),
    )
    tile_index = 0
    for i, _ in enumerate(range(x_min, x_max + 1)):
        for j, _ in enumerate(range(y_min, y_max + 1)):
            with Image.open(BytesIO(tiles[tile_index])) as tile:
                if tile.size != (TILE_SIZE, TILE_SIZE):
                    raise ValueError(
                        "源瓦片尺寸 %s != %dx%d，拼接会错位" % (tile.size, TILE_SIZE, TILE_SIZE)
                    )
                composite.paste(tile.convert("RGBA"), (i * TILE_SIZE, j * TILE_SIZE))
            tile_index += 1
    return composite
