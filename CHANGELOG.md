# Changelog

## 1.1.0 — 2026-10-29

### 新增

- **内置首页**：`GET /` 返回 `examples/openlayers-demo.html`（OpenLayers 示例页）。
  容器或裸 `uvicorn` 启动后直接打开 `http://127.0.0.1:9002/` 就能验证服务，不必再去仓库里找 html。
  六个示例一键切换：OSM 原生 / Google 影像直通 / **高德「原始」**（直连上游）/
  高德 `gcj2wgs` / **百度「原始」**（直连上游）/ 百度 `bd2wgs`。
  其中百度是非标准网格（原点居中、Y 轴向上、res=2^(18-z)），直接当 XYZ 用只会拿到
  「200 + 全白」，因此页面给它配了一张自定义 `tileGrid`（世界原点 ±33554432m、
  分辨率 2^(18-z)）并用 `tileUrlFunction` 把列/行翻回百度索引；
  **点任一示例就把该图层的完整 XYZ 模板 URL 填进输入框——输入框里就是 OL 实际加载的底图**，
  可随时手改后按回车/「加载」即时预览自己想要的源，配「复制」按钮直接粘到
  OL / Leaflet / MapLibre / Cesium 使用。
- 首页自动加载 `examples/data/forbidden-city-buildings.geojson`（故宫 3896 个建筑白模，
  WGS84，由 `/demo/data/{name}` 提供）作为对照层，初始视图 fit 到该数据范围：
  切「原始」能直观看到 GCJ/BD 的整体偏移，切 `gcj2wgs`/`bd2wgs` 则与白模严丝合缝。
  顶部有探活角标（读 `/health`，含版本号）与 `/docs` 入口。
  页面基址默认取 `location.origin`：换端口、走 nginx 反代、换域名都不需要手改；
  仅在用 `file://` 直接打开时才回落到 `http://127.0.0.1:9002`。
- `GET /health` 增加 `version` 字段（如 `{"ok":true,"service":"tile-proxy","version":"1.1.0"}`），
  供首页角标与外部监控读取。
- 源码树缺少 `examples/` 时 `/` 返回一页极简说明（指向 `/health`、`/docs`），不报 404。
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

### 修复

- **纠偏输出永久白斑**。旧实现 `_fetch_tile_grid` 在单块源瓦片取失败时用空白图顶替
  （`image_to_bytes(_build_blank_tile())`），拼出的输出是一整块透明 PNG，随后被写入
  磁盘缓存（7 天）→ nginx（7 天）→ CDN 边缘（7 天）。一次上游抖动会被逐层放大成
  一周白斑，用户看到的就是一块永远填不上的空白。
  现在：取瓦片失败重试 `GCJRE_FETCH_ATTEMPTS`（默认 3）次后**抛错**，整块请求以 5xx
  结束（nginx 只缓存 200、CDN 不缓存 5xx）；空白源片缓存超过 `GCJRE_BLANK_RETRY_SECONDS`
  视为软未命中，上游恢复后自动覆盖；空白输出不落盘，历史空白产物读到即删。
- **瓦片偶发 CORS 错误（共享缓存放大了缺头响应）**。`CORSMiddleware` 只在请求带
  `Origin` 时才回 `Access-Control-Allow-Origin`，而 nginx / CDN 的缓存键只有 URL、
  也不看 `Vary`：先被 curl 之类无 Origin 的客户端取一次，就会把「没有 ACAO 的响应」
  写进共享缓存，之后浏览器命中的全是它 → 前端报 CORS。新增
  `CorsCacheSafeMiddleware`（最外层纯 ASGI）：任何响应只要没有 ACAO 就补 `*`，
  让响应与 Origin 无关；`TILE_PROXY_ENFORCE_ORIGIN=1` 白名单模式下不启用。
- **部分透明的「漏边」瓦片**（线上 z17/x=107151/y=51994 右侧 12.9% 透明带）。
  上游偶发返回 `200 + 全透明` 的空白片时，旧实现把它当正常片拼进合成图，
  输出里对应那一块就是透明的；7.9% 的透明率没到 10% 阈值，于是通过校验被
  落盘 + CDN 各缓存 7 天。现在三处一起收紧：
  1. **空白源片立即重取一次**（`_get_tile_cached`），仍空白才按「真·无覆盖」处理；
     空白片**一律不写磁盘缓存**，避免一次抖动被钉死一整个缓存周期；
  2. **新增不变量**：源网格全不透明 → 输出必须也全不透明（warp 只从合成图内部取样），
     超过 0.5% 透明即判废并强制重取；源本身含透明（真覆盖边界）时不套用该判据，
     不会让空白区域陷入无限重试；
  3. 判废后走既有链路：不落盘、响应 `no-store` + `X-Tile-Blank: 1`。
- **`origin_guard` 不再改写 `Cache-Control`**。它原先统一打 `private, max-age=300`
  以阻止 CDN 缓存、保住防盗链白名单，但 `private` 会让共享缓存全部 BYPASS，
  与边缘缓存直接互斥。缓存头统一交给 `cache_policy`，两者分工明确。
  其豁免路径同时加入 `/`（首页属公开入口，不应因 `Referer` 不在白名单被拦；
  匹配条件是 `path == "/"`，不会误放行其它路径）。
- **质量判据的源像素归一化**（发布前 review 修）。`assess_tile_quality` 原先把
  「源字节总数」除以**输出**像素数，2×2 网格会把源的 B/px 抬高 4 倍，阈值随之变严 4 倍：
  海面 / 沙漠 / 纯色街区这类正常但极好压的瓦片会被误判成「丢细节」，陷入
  「每次请求强制重取 + 永不缓存」。现在新增 `source_pixels` 参数，两侧都按真实
  「字节/像素」比较，缺该参数时跳过该判据。
- **热路径不再重复解码**。`_tile_response` 原先对每个响应（含内存命中）都跑一遍
  `assess_tile_quality`，一次冷请求要解码 3 次；且不合格瓦片会被写进进程内缓存 300s，
  用户重试仍拿到白斑。现在：校验只在计算后做一次，**只有合格瓦片进内存缓存**，
  命中直接返回。
- `_scheme_hint` 收窄触发条件：只认 `WRONG_VERSION_NUMBER` / `record layer failure` /
  `unknown protocol`。原先任何含 `SSL` 的错误（含证书校验失败）都会给出
  「上游是明文 HTTP」的错误结论。

### 变更

- **限流默认关闭**（`PROXY_RATE_LIMIT` 代码默认 600 → 0；Dockerfile / docker-compose / systemd 模板
  同步为 0）。自托管场景一屏瓦片就有 20~30 个请求，默认限流太容易误伤——本地 Docker 尤其明显：
  端口映射后所有请求都来自网桥网关 IP（`172.17.0.1`），共用一个计数桶，平移几次地图就 429。
  **公网部署务必显式设 ≥120**（README 与 `docs/configuration.md` 已标注）。

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
  同时 `COPY examples ./examples`，内置首页随镜像分发。
  境外构建可用 `--build-arg PIP_INDEX_URL=https://pypi.org/simple` 切回官方源。
- 版本号唯一真源是 `app.py` 的 `APP_VERSION`（`/openapi.json` 与 `/health` 共用），
  发版时同步 `pyproject.toml`、`Dockerfile` 的 `LABEL`、README 徽章与本文。

## 1.0.1 — 2026-09-15

- 发布默认 Docker 镜像 `negiao/tile-proxy:latest`；`docker-compose.yml` 默认拉取预构建镜像，
  无需本地 `build`。

## 1.0.0 — 2026-09-15

- 首个独立开源版本（自 WebGIS-Dev 抽出）
- 功能：GCJ/BD 双向纠偏、通用直通代理、内存+磁盘缓存、限流、SSRF 护栏
- 文档：架构 / 用法 / 本地·nginx·Docker 部署 / 配置
- 默认 CORS `*`
- **Docker Hub**：`negiao/tile-proxy:latest` / `:1.0.0`
