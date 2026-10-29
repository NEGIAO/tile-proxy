# -*- coding: utf-8 -*-
"""管理配置的持久化（SQLite）。

为什么要一个库而不是 JSON 文件：配置项里包含**管理密码**，必须有
「哈希存储 + 盐 + 可改」的能力，JSON 明文放密码是不行的；顺带把可调
配置项也放进同一张表，读写都走事务，比「读改写整个文件」稳。

结构（`data/admin.db`，路径可用 `TILE_PROXY_ADMIN_DB` 覆盖）：

| 表 | 用途 |
|---|---|
| `settings(key, value, updated_at)` | 运行时配置（值统一存 JSON 文本） |
| `meta(key, value)` | 密码哈希、盐、令牌签名密钥 |

密码用 **PBKDF2-HMAC-SHA256（200k 轮）**，只存 `salt$hash`；
校验用 `hmac.compare_digest` 做常量时间比较。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# **不内置默认密码**：库为空时管理接口处于「未初始化」状态，只能调用
# `POST /admin/setup` 设置第一个密码；设置完成前登录一律拒绝。
# 这样开源仓库不会带一个「人人皆知的口令」，也不存在「忘了改」的窗口期。
# 自动化部署（无浏览器）可以给 TILE_PROXY_ADMIN_PASSWORD 让它启动时直接初始化。
BOOTSTRAP_PASSWORD = os.getenv("TILE_PROXY_ADMIN_PASSWORD", "").strip()
# 登录令牌有效期（秒）
TOKEN_TTL = 12 * 3600
_PBKDF2_ROUNDS = 200_000

_DB_PATH = Path(
    os.getenv("TILE_PROXY_ADMIN_DB")
    or (Path(__file__).resolve().parent / "data" / "admin.db")
)

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

# settings 的 mtime 缓存：值很小，读一次全表比每次开连接便宜
_cache: Dict[str, Any] = {"ts": -1.0, "data": {}}


def db_path() -> Path:
    """数据库文件路径（供管理页回显）。"""
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        _conn.commit()
    return _conn


# ==================== meta（密码 / 密钥） ====================
def _meta_get(key: str) -> Optional[str]:
    with _lock:
        row = _connect().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(key: str, value: str) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    ).hex()


def is_initialized() -> bool:
    """管理密码是否已设置过（决定前端显示「初始化」还是「登录」）。"""
    return _meta_get("password") is not None


def ensure_password() -> None:
    """仅在给了 `TILE_PROXY_ADMIN_PASSWORD` 且库里还没有密码时自动初始化。

    没给的话保持「未初始化」，由页面上的首次设置流程写入——开源部署默认走这条路。
    """
    if BOOTSTRAP_PASSWORD and not is_initialized():
        set_password(BOOTSTRAP_PASSWORD)


def set_password(password: str) -> None:
    """设置管理密码（只存盐 + PBKDF2 哈希），并轮换令牌密钥让旧登录立即失效。"""
    salt = secrets.token_hex(16)
    _meta_set("salt", salt)
    _meta_set("password", _hash(password, salt))
    # 令牌是 `<exp>.<hmac(token_secret, exp)>` 的无状态签名：换掉密钥，
    # 所有已签发的令牌立刻验不过，等于「改密码后强制重新登录」。
    _meta_set("token_secret", secrets.token_hex(32))


def verify_password(password: str) -> bool:
    """校验管理密码（常量时间比较）；未初始化时一律 False。"""
    salt = _meta_get("salt")
    stored = _meta_get("password")
    if not salt or not stored:
        return False
    return hmac.compare_digest(_hash(password, salt), stored)


def _secret() -> str:
    value = _meta_get("token_secret")
    if not value:
        value = secrets.token_hex(32)
        _meta_set("token_secret", value)
    return value


# ==================== 登录令牌（无状态签名） ====================
def issue_token(ttl: int = TOKEN_TTL) -> Tuple[str, int]:
    """签发登录令牌：`<exp>.<hmac>`，服务端只验签不存会话。"""
    exp = int(time.time()) + ttl
    sig = hmac.new(_secret().encode(), str(exp).encode(), hashlib.sha256).hexdigest()
    return "%d.%s" % (exp, sig), ttl


def verify_token(token: str) -> bool:
    """校验令牌：签名对 + 未过期。"""
    if not token or "." not in token:
        return False
    exp_s, sig = token.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    want = hmac.new(_secret().encode(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


# ==================== settings（运行时配置） ====================
def _refresh(force: bool = False) -> Dict[str, Any]:
    now = time.time()
    if not force and now - _cache["ts"] < 1.0:
        return _cache["data"]
    try:
        rows = _connect().execute("SELECT key, value FROM settings").fetchall()
    except sqlite3.Error:
        return _cache["data"]
    data: Dict[str, Any] = {}
    for key, raw in rows:
        try:
            data[key] = json.loads(raw)
        except ValueError:
            data[key] = raw
    _cache["data"] = data
    _cache["ts"] = now
    return data


def all_settings() -> Dict[str, Any]:
    """全部运行时配置（1 秒内复用缓存，避免每张瓦片都查库）。"""
    return dict(_refresh())


def get_setting(key: str, default: Any = None) -> Any:
    return _refresh().get(key, default)


def set_settings(values: Dict[str, Any]) -> None:
    """批量写入（值统一 JSON 序列化）；传 None 表示删除该键。"""
    if not values:
        return
    with _lock:
        conn = _connect()
        now = time.time()
        for key, value in values.items():
            if value is None:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
        conn.commit()
    _refresh(force=True)


def updated_at() -> Dict[str, float]:
    """各配置项最后修改时间（管理页展示用）。"""
    try:
        rows = _connect().execute("SELECT key, updated_at FROM settings").fetchall()
    except sqlite3.Error:
        return {}
    return {k: v for k, v in rows}
