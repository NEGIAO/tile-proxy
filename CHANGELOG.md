# Changelog

## 1.1.0 — 2026-10-29

### 修复

- **纠偏输出永久白斑**。旧实现 `_fetch_tile_grid` 在单块源瓦片取失败时用空白图顶替
  （`image_to_bytes(_build_blank_tile())`），拼出的输出是一整块透明 PNG，随后被写入
  磁盘缓存（7 天）→ nginx（7 天）→ CDN 边缘（7 天）。一次上游抖动会被逐层放大成
  一周白斑，用户看到的就是一块永远填不上的空白。
  现在：取瓦片失败重试 `GCJRE_FETCH_ATTEMPTS`（默认 3）次后**抛错**，整块请求以 5xx
  结束（nginx 只缓存 200、CDN 不缓存 5xx）；空白源片缓存超过 `GCJRE_BLANK_RETRY_SECONDS`
  视为软未命中，上游恢复后自动覆盖；空白输出不落盘，历史空白产物读到即删。
- **`origin_guard` 不再改写 `Cache-Control`**。它原先统一打 `private, max-age=300`
  以阻止 CDN 缓存、保住防盗链白名单，但 `private` 会让共享缓存全部 BYPASS，
  与边缘缓存直接互斥。缓存头统一交给 `cache_policy`，两者分工明确。
- **质量判据的源像素归一化**（发布前 review 修）。`assess_tile_quality` 原先把
  「源字节总数」除以**输出**像素数，2×2 网格会把源的 B/px 抬高 4 倍，阈值随之变严 4 倍：
  海面 / 沙漠 / 纯色街区这类正常但极好压的瓦片会被误判成"丢细节"，陷入
  「每次请求强制重取 + 永不缓存」。现在新增 `source_pixels` 参数，两侧都按真实
  「字节/像素」比较，缺该参数时跳过该判据。
- **热路径不再重复解码**。`_tile_response` 原先对每个响应（含内存命中）都跑一遍
  `assess_tile_quality`，一次冷请求要解码 3 次；且不合格瓦片会被写进进程内缓存 300s，
  用户重试仍拿到白斑。现在：校验只在计算后做一次，**只有合格瓦片进内存缓存**，
  命中直接返回。
- `_scheme_hint` 收窄触发条件：只认 `WRONG_VERSION_NUMBER` / `record layer failure` /
  `unknown protocol`。原先任何含 `SSL` 的错误（含证书校验失败）都会给出
  「上游是明文 HTTP」的错误结论。

### 新增

- **`cache_policy` 中间件**：对 **200 + `image/*`** 响应统一写入
  `Cache-Control: public, max-age=<浏览器 TTL>, s-maxage=<边缘 TTL>`，并剥掉
  `expires`/`pragma`。动机是上游各源的 `cache-control` 差异极大（Google 卫星 257 天、
  高德与 Google 路网 1 小时、纠偏路由完全没有），原样透传会让浏览器缓存时长失控。
  纯 ASGI 实现，只改 `http.response.start` 的 header、不触碰 body，兼容 `StreamingResponse`。
- **瓦片有效性校验与强制重取**：`grid.assess_tile_quality()` 按「透明像素占比 /
  输出与源瓦片的体积比 / 解码是否成功」判定纠偏输出是否可信，不合格则
  `force_refresh=True` 强制重取一次（绕过源瓦片缓存），仍不合格则响应打
  `no-store` + `X-Tile-Blank: 1`。四个纠偏入口全部接入。
  **判废只认「透明」**——不透明白色是上游真实的覆盖边界，误判会让空白区域无限重试。
- 镜像自带 `HEALTHCHECK`（探 `/health`），`docker ps` 可直接看到 `(healthy)`。

### 移除（不兼容）

- **`GET /tiles/ships66/{z}/{x}/{y}.png`** 与 `SHIPS66_TILE_URL_TEMPLATE` 已删除。
  该端点只是「读模板 → 转发」的一次性包装，用 `/proxy/{上游模板}` 可等价表达：
  `/proxy/https://example.com/{z}/{x}/{y}.png`。若仍在用，请改前端 URL。

### 新增配置

`TILE_BROWSER_TTL_SECONDS`、`TILE_EDGE_TTL_SECONDS`、`GCJRE_FETCH_ATTEMPTS`、
`GCJRE_BLANK_RETRY_SECONDS`、`GCJRE_MAX_TRANSPARENT_PERCENT`，详见
`docs/configuration.md`；缓存分层与判据见 `docs/architecture.md` §6.1。

### 部署提示

- 用 nginx 做二级瓦片缓存时需加 `proxy_no_cache $upstream_http_x_tile_blank;`，
  否则空白瓦片仍会被本地缓存 7 天（完整片段见 `docs/deployment.md` §C.3）。
- `Dockerfile` 改为 `COPY *.py ./`，新增 app 级模块不再需要改 Dockerfile；
  境外构建可用 `--build-arg PIP_INDEX_URL=https://pypi.org/simple` 切回官方源。

## 1.0.1 — 2026-09-15

- 发布默认 Docker 镜像 `negiao/tile-proxy:latest`；`docker-compose.yml` 默认拉取预构建镜像，
  无需本地 `build`。

## 1.0.0 — 2026-09-15

- 首个独立开源版本（自 WebGIS-Dev 抽出）
- 功能：GCJ/BD 双向纠偏、通用直通代理、内存+磁盘缓存、限流、SSRF 护栏
- 文档：架构 / 用法 / 本地·nginx·Docker 部署 / 配置
- 默认 CORS `*`
- **Docker Hub**：`negiao/tile-proxy:latest` / `:1.0.0`
