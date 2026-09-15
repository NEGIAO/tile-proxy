# 配置参考

所有配置均为 **环境变量**（L1）。未设置时使用代码内默认。

## 服务

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | `__main__` 直跑时监听地址 |
| `PORT` | `9002` | 端口 |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING |

## 直通 / 出站 HTTP

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_HTTP_TIMEOUT_SECONDS` | 20 | 总超时 |
| `PROXY_HTTP_CONNECT_TIMEOUT_SECONDS` | 5 | 连接超时 |
| `PROXY_MAX_CONNECTIONS` | 100 | 连接池 |
| `PROXY_MAX_KEEPALIVE_CONNECTIONS` | 20 | keep-alive |
| `PROXY_MAX_RESPONSE_MB` | 32 | 单响应上限；0=不限 |
| `PROXY_VERIFY_SSL` | true | 校验上游证书 |
| `PROXY_USER_AGENT` | （内置 Chrome UA） | 可覆盖出站 UA |
| `PROXY_ALLOWED_HOSTS` | 空 | 非空=仅允许这些 host（收紧模式） |
| `PROXY_DNS_GUARD` | true | 解析后私网复判 |
| `PROXY_ALLOW_PRIVATE_HOSTS` | **false** | true 仅限本机/内网调试 |

## 内存瓦片缓存

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_TILE_CACHE_TTL_SECONDS` | 300 | 条目 TTL（10–3600） |
| `PROXY_TILE_CACHE_MAX_SIZE` | **100000** | 条目上限。**生产小内存请改 2000–5000** |

条目数 × 约 15KB ≈ 内存占用；10 万条可能到数 GB。

## 限流

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_RATE_LIMIT` | 0 | 每 IP 每分钟；0=关闭。公网建议 ≥120 |

## 纠偏护栏

| 变量 | 默认 | 说明 |
|---|---|---|
| `GCJRE_MAX_CONCURRENCY` | 16 | 单请求源片并发。VPS 建议 4 |
| `GCJRE_MAX_TILES_PER_REQUEST` | 64 | 网格片数上限（正常 2×2~3×3） |
| `GCJRE_TILE_MAX_MB` | 8 | 单源片字节 |
| `GCJRE_MAX_IMAGE_PIXELS` | 16777216 | 解码像素硬上限 |

## 磁盘缓存

| 变量 | 默认 | 说明 |
|---|---|---|
| `GCJRE_CACHE` | `./data/gcj_rectify_cache` | 缓存根。生产建议 `/var/cache/tile-proxy` |
| `GCJRE_CACHE_MAX_AGE_DAYS` | 7 | 按 mtime 清理；0=关 |
| `GCJRE_CACHE_MAX_MB` | 2048 | 容量上限，超限删最旧；0=关 |
| `GCJRE_CACHE_CLEANUP_INTERVAL_S` | 3600 | 后台清理间隔；0=关 |

## 可选

| 变量 | 默认 | 说明 |
|---|---|---|
| `SHIPS66_TILE_URL_TEMPLATE` | 空 | `/tiles/ships66/{z}/{x}/{y}.png` 上游模板 |

## 生产组合示例（1GB VPS）

```env
PROXY_TILE_CACHE_MAX_SIZE=3000
PROXY_TILE_CACHE_TTL_SECONDS=180
PROXY_RATE_LIMIT=180
PROXY_MAX_CONNECTIONS=40
GCJRE_MAX_CONCURRENCY=4
GCJRE_MAX_TILES_PER_REQUEST=16
GCJRE_CACHE=/var/cache/tile-proxy
GCJRE_CACHE_MAX_MB=512
GCJRE_CACHE_MAX_AGE_DAYS=3
GCJRE_CACHE_CLEANUP_INTERVAL_S=1800
PROXY_ALLOW_PRIVATE_HOSTS=false
```

对应 systemd 可直接抄 `deploy/tile-proxy.service` 里的 `Environment=` 行。
