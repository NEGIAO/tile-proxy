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

## 相同字节瓦片检测

用于识别「不同瓦片返回同一份字节」的异常响应 —— 典型是 OSM 的「403 Access blocked」
拦截图：HTTP 200、`Content-Type: image/png`、体积恒定，任何按状态码/类型的健康检查
都会放过它。判定命中后该响应回 `X-Tile-Suspect: 1` + `no-store`，并记 WARNING 日志。

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_IDENTICAL_TILE_DETECT` | true | 检测总开关 |
| `PROXY_IDENTICAL_TILE_THRESHOLD` | 12 | 同一 host 下同一指纹命中多少个不同 URL 即判定异常 |
| `PROXY_SUSPECT_SCAN_MAX_KB` | 64 | 参与检测的响应体上限；超出走流式、不检测 |

> 已知误判：大面积纯色区域（远洋、极地）渲染结果本就相同。代价仅是这些瓦片不走
> 长缓存（每次回源），不影响正确性；嫌回源多就把阈值调高。

## 限流

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_RATE_LIMIT` | **0（不限流）** | 每 IP 每分钟；0=关闭。**公网部署请显式设 ≥120**。触发后瓦片请求返回 `assets/notices/error1.png`（默认 **203**，见 `PROXY_RATE_LIMIT_MODE`；不缓存，窗口过后自动恢复） |
| `PROXY_RATE_LIMIT_MODE` | `image-203` | 限流时瓦片返回什么：`image-203`（图片+203，默认）/ `image-200`（图片+200，给 Cesium）/ `json-429`（纯 JSON）。**可在首页 `/` 开关里实时切换**，无需重启 |
| `TILE_PROXY_ADMIN_PASSWORD` | 空 | 仅用于**无人值守部署**：首次启动时直接写入管理密码。留空则保持「未初始化」，需打开 `/admin` 由人工设置（推荐） |
| `TILE_PROXY_ADMIN_DB` | `<repo>/data/admin.db`（镜像内 `/data/admin/admin.db`） | 配置库（SQLite）：配置覆盖 + 密码哈希 + 令牌密钥。**必须放在持久卷上**，否则容器重建会丢密码 |
| `TILE_PROXY_SESSION_TTL` | `43200` | 管理登录令牌有效期（秒，默认 12 小时） |

> **本地 Docker 容易 429？** 端口映射（`-p 127.0.0.1:9002:9002`）后，浏览器请求在容器里
> 全部来自网桥网关 IP（`172.17.0.1`），**所有客户端挤进同一个计数桶**；本地又没有 nginx 缓存
> 兜底，地图平移一次就是 15~30 片、来回几次就超过阈值。本地调试建议 `-e PROXY_RATE_LIMIT=0`。
> 线上不会有这个问题：nginx 设了 `X-Real-IP`/`X-Forwarded-For`（按真实客户端分桶），
> 且 7 天瓦片缓存会把绝大多数请求挡在应用之外（`X-NGINX-Cache: HIT` 根本不到应用）。

## 纠偏护栏

| 变量 | 默认 | 说明 |
|---|---|---|
| `GCJRE_MAX_CONCURRENCY` | 16 | 单请求源片并发。VPS 建议 4 |
| `GCJRE_MAX_TILES_PER_REQUEST` | 64 | 网格片数上限（正常 2×2~3×3） |
| `GCJRE_TILE_MAX_MB` | 8 | 单源片字节 |
| `GCJRE_MAX_IMAGE_PIXELS` | 16777216 | 解码像素硬上限 |
| `GCJRE_FETCH_ATTEMPTS` | 3 | 单源片取失败重试次数；用尽即抛错（路由返回 5xx，不进缓存） |
| `GCJRE_BLANK_RETRY_SECONDS` | 600 | 空白源片缓存视为"软命中"的时限；超时后重新向上游取 |
| `GCJRE_MAX_TRANSPARENT_PERCENT` | 10 | 输出透明像素占比上限，超过即判废并强制重取 |
| `GCJRE_MAX_SOURCE_ZOOM` | 18 | 纠偏源瓦片最大层级；超过直接返回「级别过大」提示图（200 + 缓存 1 天），不请求上游。0=关闭。高德卫星 z19 有影像，用卫星时设 19 |

## 响应缓存（Cache-Control）

纠偏与直通的 **200 + `image/*`** 响应统一由 `cache_policy` 中间件写入，
覆盖上游原值并剥掉 `expires`/`pragma`：

```
Cache-Control: public, max-age=<浏览器>, s-maxage=<CDN 边缘>
```

| 变量 | 默认 | 说明 |
|---|---|---|
| `TILE_BROWSER_TTL_SECONDS` | 86400（1 天） | 浏览器 TTL。不宜过长——坏瓦片 purge 清得掉边缘、清不掉浏览器 |
| `TILE_EDGE_TTL_SECONDS` | 604800（7 天） | `s-maxage`。与 CDN 缓存规则对齐；规则失效时仍能兜住 |

> 空白/不合格瓦片（`X-Tile-Blank: 1`）会被改写成 `no-store`，不进任何一层缓存。
> 若用 nginx 做二级缓存，记得加 `proxy_no_cache $upstream_http_x_tile_blank;`。

## 磁盘缓存

| 变量 | 默认 | 说明 |
|---|---|---|
| `GCJRE_CACHE` | `./data/gcj_rectify_cache` | 缓存根。生产建议 `/var/cache/tile-proxy` |
| `GCJRE_CACHE_MAX_AGE_DAYS` | 7 | 按 mtime 清理；0=关 |
| `GCJRE_CACHE_MAX_MB` | 2048 | 容量上限，超限删最旧；0=关 |
| `GCJRE_CACHE_CLEANUP_INTERVAL_S` | 3600 | 后台清理间隔；0=关 |

## 来源校验（防盗链，默认关闭）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TILE_PROXY_ENFORCE_ORIGIN` | `0` | 总开关。`0`=不挂载校验、CORS 全开放；`1`=启用白名单，CORS 也只放行白名单 |
| `TILE_PROXY_ALLOWED_ORIGINS` | 内置白名单 | 逗号分隔的 origin；留空/全部非法时回落内置列表 |

启用后判定规则：有 `Origin` 或 `Referer` 就必须在白名单内（否则 403）；两者都没有的请求
（QGIS / ArcGIS Pro / curl / 服务端脚本）一律放行，本地与桌面端零配置。

> 代价：白名单只在应用层生效。一旦启用 CDN 边缘缓存（见上一节），缓存键只有 URL，
> 命中缓存的请求不再经过本进程，白名单在边缘层失效。真要强认证请另加令牌。

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

## 管理接口（HTTP）

管理页 `/admin` 之外，所有能力都有等价 HTTP 接口，便于脚本化。除 `status` / `setup` / `login` 外，
写操作一律要 `Authorization: Bearer <token>`（12 小时有效；改口令即让全部旧令牌失效）。

```bash
# 1) 首次初始化（仅当库里还没有口令；已初始化返回 409）
curl -X POST http://127.0.0.1:9002/admin/setup \
     -H 'Content-Type: application/json' -d '{"password":"至少8位"}'
# → {"token":"...","payload":{...}}

# 2) 登录换令牌
curl -X POST http://127.0.0.1:9002/admin/login \
     -H 'Content-Type: application/json' -d '{"password":"..."}'

# 3) 读 / 改配置（token 从上面取）
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:9002/admin/config
curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"settings":{"PROXY_RATE_LIMIT":300}}' http://127.0.0.1:9002/admin/config
```

| 接口 | 说明 |
|---|---|
| `GET /admin/status` | 公开只读：版本、运行时长、代理 / 限流开关（无需令牌） |
| `POST /admin/setup` | 首次初始化口令（仅未初始化时可用） |
| `POST /admin/login` | 口令换令牌（12 小时） |
| `POST /admin/password` | 改口令（需令牌，改完旧令牌全部失效） |
| `GET /admin/config` | 读全部配置（需令牌） |
| `PUT /admin/config` | 改配置（需令牌，整批校验，任一项不合法整体拒绝） |

> Docker 部署：管理库在 `/data/admin` 卷上（compose 里是 `tile-proxy-admin`），重建容器不会丢口令；
> **删这个卷等于重置管理口令**。
