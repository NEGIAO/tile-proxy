# -*- coding: utf-8 -*-
"""瓦片指纹检测：识别「不同瓦片返回完全相同字节」的异常响应。

## 为什么需要它

2026-10-30 实测：OSM 判定调用方违反瓦片使用政策后，会对**任意坐标**返回同一张
256×256 的「403 Access blocked」提示图 —— 但 **HTTP 状态是 200**、`Content-Type: image/png`、
体积恒为 6987 字节。任何按状态码 / 内容类型做的健康检查都会放过它：单次请求看起来完全正常。
只有把多个不同瓦片放在一起比对才会暴露：**不同坐标返回同一份字节**。

同一机制也覆盖其它源的固定占位图（"No data"、"zoom not available"、水印图、纯色兜底片）。

## 判定规则

同一 host 下，若同一个 sha256 在 `window_seconds` 内被 ≥ `threshold` 个**不同 URL** 命中，
即判定该 digest 为「固定应答」，并在其后 `suspect_ttl_seconds` 内持续标记。

**只标记、不拦截**：调用方据此给响应加 `X-Tile-Suspect` / `X-Tile-NoCache`，
让缓存策略降级为 `no-store` —— 否则错误图会被边缘缓存钉住（OSM 拦截图自带
`Cache-Control: max-age=604800`，一次命中就是一整周）。

## 已知局限

大面积纯色区域（远洋、极地）的渲染结果本就相同，可能被误判为固定应答。
代价仅是这些瓦片不走长缓存（每次回源），不影响正确性；阈值可通过
`PROXY_IDENTICAL_TILE_THRESHOLD` 调整，检测本身可用 `PROXY_IDENTICAL_TILE_DETECT` 关闭。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

# 观察窗口：超过该时长的历史 URL 不再计入「不同 URL」计数
DEFAULT_WINDOW_SECONDS = 1800
# 判定成立后的保持时长（避免每次请求都重新累积）
DEFAULT_SUSPECT_TTL_SECONDS = 1800
# 单 host 保留的 digest 条目上限（LRU 淘汰，防内存无界）
DEFAULT_MAX_DIGESTS_PER_HOST = 512


@dataclass
class _DigestEntry:
    """一个 digest 的观察记录。"""

    urls: Set[str] = field(default_factory=set)
    window_started: float = 0.0
    last_seen: float = 0.0
    suspect_until: float = 0.0


class IdenticalTileDetector:
    """按 host 统计响应体指纹，识别「同一份字节应答不同瓦片」的固定应答。"""

    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        suspect_ttl_seconds: int = DEFAULT_SUSPECT_TTL_SECONDS,
        max_digests_per_host: int = DEFAULT_MAX_DIGESTS_PER_HOST,
    ) -> None:
        self._window = max(1, int(window_seconds))
        self._suspect_ttl = max(1, int(suspect_ttl_seconds))
        self._max_digests = max(16, int(max_digests_per_host))
        self._hosts: Dict[str, "OrderedDict[str, _DigestEntry]"] = {}
        self._lock = threading.Lock()
        # 观测计数：便于在状态页/日志里判断检测是否真的在工作
        self._observed = 0
        self._suspect_responses = 0
        self._suspect_digests = 0

    def observe(
        self,
        host: str,
        url: str,
        digest: str,
        *,
        threshold: int = 12,
        now: Optional[float] = None,
    ) -> bool:
        """记录一次响应并判断是否为「固定应答」。

        Args:
            host: 上游主机名（同一主机内独立统计）。
            url: 本次请求的上游 URL（用于统计「不同 URL」数量）。
            digest: 响应体 sha256。
            threshold: 触发判定的不同 URL 数量阈值。
            now: 单调时钟，仅供测试注入。

        Returns:
            True 表示该 digest 已被判定为固定应答（调用方应放弃长缓存）。
        """
        moment = time.monotonic() if now is None else float(now)
        threshold = max(2, int(threshold))
        host_key = (host or "").lower()

        with self._lock:
            self._observed += 1
            bucket = self._hosts.get(host_key)
            if bucket is None:
                bucket = OrderedDict()
                self._hosts[host_key] = bucket

            entry = bucket.get(digest)
            if entry is None:
                entry = _DigestEntry(window_started=moment)
                bucket[digest] = entry

            # 窗口过期：重新开始累积（否则一周前的旧 URL 会一直把计数撑高）
            if moment - entry.window_started > self._window:
                entry.urls.clear()
                entry.window_started = moment

            entry.urls.add(url)
            entry.last_seen = moment

            if entry.suspect_until <= moment and len(entry.urls) >= threshold:
                entry.suspect_until = moment + self._suspect_ttl
                self._suspect_digests += 1

            suspicious = entry.suspect_until > moment
            if suspicious:
                self._suspect_responses += 1

            # LRU：按最近一次命中时间淘汰，防内存无界
            bucket.move_to_end(digest)
            while len(bucket) > self._max_digests:
                bucket.popitem(last=False)

            return suspicious

    def stats(self) -> Dict[str, int]:
        """返回观测计数与当前活跃 digest 数（供状态页/日志使用）。"""
        with self._lock:
            return {
                "observed": self._observed,
                "suspect_responses": self._suspect_responses,
                "suspect_digests": self._suspect_digests,
                "tracked_digests": sum(len(b) for b in self._hosts.values()),
            }

    def reset(self) -> None:
        """清空全部观察状态（测试与管理页手动复位用）。"""
        with self._lock:
            self._hosts.clear()
            self._observed = 0
            self._suspect_responses = 0
            self._suspect_digests = 0


_detector = IdenticalTileDetector()


def get_detector() -> IdenticalTileDetector:
    """进程级单例：所有出站面共享同一份观察状态。"""
    return _detector
