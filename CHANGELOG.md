# Changelog

## 1.2.0 — 2026-10-30

### 修复

- **OSM 被「403 Access blocked」拦截**：出站请求一直以浏览器身份访问（`Chrome/126` UA +
  `sec-ch-ua` + `Sec-Fetch-*`，且把终端用户的浏览器 UA 原样透传），违反 OSM 瓦片使用政策的两条
  明文禁令 —— *"never impersonate another app or a browser"* 与 *"do not … tunnel all clients
  behind a single, anonymous identity"*。表现为任意坐标返回同一张 256×256 提示图，HTTP 仍为 200。
  现按 host 分流：命中 `IDENTIFIED_UA_BY_DOMAIN` 的上游改用带应用名与联系方式的稳定 UA
  （`WebGIS-Dev/1.0 (+https://webgis.negiao.cn; contact: …)`），并剥掉 `sec-ch-ua*` /
  `Sec-Fetch-*` / `Accept-Language`；客户端 `Referer` 仍然端到端保留（政策要求不得剥离）。

### 新增

- **相同字节瓦片检测**（`domains/tiles/infra/tile_fingerprint.py`）：同一 host 下若同一个
  sha256 在窗口期内被 ≥ 阈值个**不同 URL** 命中，判定该 digest 为固定应答（拦截图/占位图/
  水印图），响应加 `X-Tile-Suspect: 1` + `X-Tile-NoCache: 1`，`cache_policy` 据此改写为
  `no-store` 并记 WARNING。此前这类响应会被打上 `s-maxage=604800`，**一次命中就在 CDN 边缘
  钉住一周**，且 `X-Proxy-Status` 仍标 `SUCCESS`，排查时完全看不出来。
  新增配置：`PROXY_IDENTICAL_TILE_DETECT` / `PROXY_IDENTICAL_TILE_THRESHOLD` /
  `PROXY_SUSPECT_SCAN_MAX_KB`。
- 仅对 `200 + image/* + 声明长度 ≤ 上限` 的响应生效（默认 64 KB），大图保持流式转发不变。

### 已知局限

- 远洋等纯色区域的瓦片渲染结果本就相同，可能被误判 —— 代价仅是这些瓦片不走长缓存。
- 即使身份修复到位，OSM 政策第 5 节仍写明「generally do not recommend」自建缓存代理前置；
  长期稳定方案是商业源或自托管瓦片。

## 1.1.0 — 2026-10-29

### 新增

- **内置页的 OSM 底图改为经本服务代理**（原来走 `ol.source.OSM()`，浏览器直连
  `tile.openstreetmap.org`）。直连在两类环境下必然失败：**DNS 被污染**（域名被解析到无关 IP，
  返回 403）和 **OSM 对高频/无 Referer 请求的限流**。改成
  `/proxy/tile.openstreetmap.org/{z}/{x}/{y}.png` 后由服务端取图，浏览器只和本站通信，
  与其余五个示例行为一致。
- **HTML 页面加 `Cache-Control: no-cache`**（`/` 与 `/admin`）：页面随版本更新，
  不加的话浏览器会拿旧 UI 缓存很久。

- **修 `/docs` 打不开（Swagger 报 `404 /openapi.json`）**：应用默认在**根路径**提供
  `/docs` 与 `/openapi.json`，但页面被反代到 `/tile-proxy/` 前缀后，Swagger 里的
  `/openapi.json` 仍是绝对路径，于是打到站点根 → nginx 静态 404。现在 nginx 把
  `/docs`、`/openapi.json`、`/redoc` 三条精确路径也反代给应用；两处页面里的
  「API 文档 / 探活」链接改成绝对路径，不再受前缀影响。

- **内置联调页 `/tile-proxy/` 换成 Claude 主题**（与首页、控制台同一套色板：暖白 `#faf9f5`、
  橙色 `#da7756`、Inter 字体），按钮/输入框/状态徽章统一风格，顶部加品牌图标与
  「配置控制台 / API 文档 / 探活」导航；「原始」模式仍用琥珀色区分。
- **兜底旧链接**：`/tile-proxy/tile-proxy.html`（相对路径写错）→ 302 到站点根的控制台页面，
  不再返回 404 JSON。

- **管理控制台 `tile-proxy.html`**（站点根，Claude 主题，与首页同源）：一页包含
  说明文档 + 实时状态 + **全部 31 项配置的管理表单**，替代原来分散的静态介绍页与内置开关。
  - 顶部状态：版本、运行时长、代理开关、限流开关/速率、限流响应形态，30 秒自动刷新；
  - 配置按 **服务开关 / 缓存 / 纠偏 / 网络 / 安全** 五组渲染，每项标注 **立即生效 / 重启后生效**，
    带范围提示与说明；改动先攒在内存里，点「保存」才一次性 `PUT`；
  - 敏感操作全部要**密码登录**：`POST /admin/login` 换 12 小时令牌，`PUT /admin/config` 必须带
    `Authorization: Bearer`；登录失败按 IP 节流（5 次 / 5 分钟）；改密码会轮换令牌密钥，
    旧令牌立即失效。
- **配置库 SQLite（`data/admin.db`）**：`settings`（配置覆盖）+ `meta`（密码哈希 / 盐 / 令牌密钥）。
  密码用 **PBKDF2-HMAC-SHA256（200k 轮）**，只存哈希。读取优先级：**库 > 环境变量 > 代码默认**，
  `admin_store` 带 1 秒缓存。
- **首次使用必须自己设密码**：仓库**不内置任何默认口令**。库为空时 `GET /admin/status`
  返回 `initialized: false`，页面显示「完成初始化」表单，`POST /admin/setup`（≥8 位）写入后
  才签令牌；未初始化时 `POST /admin/login` 直接 409。无人值守部署可给
  `TILE_PROXY_ADMIN_PASSWORD` 让启动时自动初始化。
- **控制台页面随仓库分发**：`examples/admin.html`，由 `GET /admin` 直接提供；同一份文件也
  能挂在站点 `/tile-proxy/` 前缀下（内部按 `location.pathname` 自动选 API 基址）。
- **Docker 持久化**：镜像新增 `TILE_PROXY_ADMIN_DB=/data/admin/admin.db` 与 `/data/admin` 卷，
  compose 增加 `tile-proxy-admin`；重建容器不会丢密码，删卷即重置。
- **瓦片代理总开关**（`proxy_gate.py` 中间件）：关闭后 `/proxy/*` 立刻 503（`no-store` +
  `X-Tile-Notice: disabled`），不打上游、不做纠偏，控制台本身仍可用。
- **限流拆成「开关 + 速率」**：`PROXY_RATE_LIMIT_ENABLED` 与 `PROXY_RATE_LIMIT`（次/分/IP），
  关掉开关等价于不限流。
- 首页导航加入「tile-proxy 控制台」与「地图演示 /tile-proxy/」；内置联调页去掉旧的开关行，
  改为链接到控制台。

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
- **两层校验**：第一层 `_validate_source_tile` 在落盘前逐张校验源片（空响应 / 过小 /
  无法解码 / 全透明空白片），不合格抛 `SourceTileError` 并绕过缓存重试，仍失败整块 5xx；
  第二层 `assess_tile_quality` 校验合成输出（透明率不得比源多出 1 个百分点、体积比、解码）。
- **源片并发去重**（单飞）：同一源片的并发请求共用一把 `asyncio.Lock` + 二次检查，
  16 张并发输出从「25 张源片请求 186 次」降到 25 次（理论最优）；3×3 并发 0.92s。
- **提示瓦片**（`assets/notices/`）：缩放越界不再返回空白片/502。
  - **层级过大**：`GCJRE_MAX_SOURCE_ZOOM`（默认 18）以上的请求直接返回 `error2.png`
    （200 + `max-age=86400`，确定性结果可缓存），**不再打上游**——省掉 3 次 404 重试。
    四条纠偏路由全部生效。图片进程内只读一次常驻；缺图时退化为纯文本。
  - **限流提示**：瓦片请求返回 `error1.png`，**状态码 200**（不是 429），非瓦片请求仍是 JSON 429。
    理由：瓦片是 `<img>` 拉的，用户只看得到一块空白、**看不到状态码**；换成一张写着
    「429 速率受限，请稍后重试」的图，非技术用户才能明白是自己请求太快、该停下来。
    之所以用 200：各加载器对非 2xx 处理不一致（Cesium 的 ImageryProvider 校验
    `statusCode === 200`，非 200 直接 reject → 什么都不显示），200 才能保证「谁都看得见这张图」；
    限流语义改由响应头表达：`X-Tile-Notice: error1` + `Retry-After`（窗口真正空出来的秒数）。
    **不缓存**是硬要求：`no-store` + `X-Tile-Blank: 1`（nginx `proxy_no_cache` 生效）+
    异常分支不进内存缓存。已用一份与线上同配置的 nginx（`proxy_cache_valid 200 7d` +
    `proxy_no_cache $upstream_http_x_tile_blank`）实测：限流期间连续三次请求全是 `MISS`
    且缓存目录里没有新增文件，窗口一过立刻 `MISS → HIT` 并返回真实瓦片。
  - **「永不缓存」标记**：提示图除 `no-store` 外还带 `X-Tile-Blank: 1` + `X-Tile-NoCache: 1`，
    nginx 侧 `proxy_no_cache` 同时接这两个变量（`X-Tile-NoCache` 是专用语义，避免借用
    「空白瓦片」的含义）。用线上同款配置（含 `proxy_ignore_headers Cache-Control`）实测：
    连续 3 次全 `MISS`、缓存目录零新增。
  - **CDN 侧不保证**：若 CDN 缓存规则是「忽略源站缓存头 + 固定 TTL」，`no-store`、`private`、
    甚至 `Set-Cookie` 都拦不住它（2026-10-29 在 Cloudflare 上三种变体实测均被 `HIT`）。
    此时必须改 CDN 规则（Edge TTL → Use origin cache-control，或 `/proxy/*` 直接 Bypass），
    详见 `docs/deployment.md` §C.4。
  - **缓存诊断探针** `?__cache_probe=1`：返回一张 `200 + no-store` 的提示图，专门用来验证
    CDN 认不认 `no-store`；不带该参数时不可达，正常请求不受影响。
- **直通路由跟随同源重定向**（最多 3 跳）。起因：ESRI Wayback 的 WMTS 路径大小写不敏感、
  旧版本号会 301 到新版本（`.../tile/26334/...` → `/arcgis/rest/services/world_imagery/.../tile/25285/...`），
  而 `Location` 是**相对路径**——原样透给浏览器会被解析到本站域名下，于是 404。
  现在同 host 的重定向由代理直接跟随（跨域仍原样透传，不扩大 SSRF 面）。

- **`scripts/smoke.sh`**：部署后一条命令验 `health` + 四条纠偏路由 + ESRI 直通 + 层级提示图，
  失败即非零退出（`./scripts/smoke.sh [base-url]`）。改完 rectfy/路由先跑它，避免静默 502。

### 新增

- **限流响应形态可运行时切换**（首页开关，无需重启）：
  `PROXY_RATE_LIMIT_MODE` 三档 `image-203`（默认）/ `image-200` / `json-429`，
  运行时覆盖写在 `data/runtime.json`（优先级高于 env）。
  首页新增「限流时返回」一行 + 管理令牌输入框，点一下即 `PUT /admin/rate-limit` 落盘生效；
  （后来演进为独立的控制台页面 + SQLite 配置库，见「管理控制台」一条）。
- **默认状态码从 200 改为 203**（`Non-Authoritative Information`）：浏览器 / OpenLayers
  照常渲染图片（实测 200/201/202/203/206/429 在 `<img>` 里都能画出来），但 **203 不在
  nginx `proxy_cache_valid 200`、也不在 CF 默认可缓存状态码里**，天然不落缓存；
  同时状态码与正常瓦片区分开，监控一眼能看出来。需要 Cesium（只认 200）时切到 `image-200`。
- 新增 `config.get_runtime/set_runtime`：带 mtime 缓存的 JSON 覆盖配置，原子写入
  （临时文件 + `os.replace`），损坏时退化为「无覆盖」而不是 500。


- **瓦片有效性校验与强制重取**：`grid.assess_tile_quality()` 按「透明像素占比 /
  输出与源瓦片的体积比 / 解码是否成功」判定纠偏输出是否可信，不合格则
  `force_refresh=True` 强制重取一次（绕过源瓦片缓存），仍不合格则响应打
  `no-store` + `X-Tile-Blank: 1`。四个纠偏入口全部接入。
  **判废只认「透明」**——不透明白色是上游真实的覆盖边界，误判会让空白区域无限重试。
- 镜像自带 `HEALTHCHECK`（探 `/health`），`docker ps` 可直接看到 `(healthy)`。
- **全链路复查补的 4 个洞**：① 第二层判废结论没有回传——`_build_with_quality` 只返回字节，
  调用方无条件 `save_output_cache`，3.5% 的条带仍会落盘（现在返回 `(bytes, ok)`，不合格
  不落盘、出口 `no-store`）；② 出口侧透明统计只数 `alpha==0`，与源侧的 `alpha<255` 口径
  不一致，会漏掉 BILINEAR 混出的半透明边缘（已统一）；③ 源片尺寸只校验 `>=32`，
  与 `_merge_tiles` 的 `paste` 假设不符（现在必须精确 `256×256`，拼接前再加一道断言）；
  ④ `bd2wgs` 里同一段 `quad = (...)` 写了两遍（清理）。
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
  2. **新增不变量**：输出透明率不得比**源网格**多出 1 个百分点（warp 只从合成图内部
     取样，多出来的透明就是缺片）。判据用比例而非「源是否全不透明」——实测 13241 个源片里
     有 395 个带 1~13 个零星透明像素（高德 PNG 边缘/水印），布尔开关会被它们绕过，
     3.5% 的条带照样漏网；源本身含透明（真覆盖边界）时按比例放行，不会陷入无限重试；
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

- **百度两条路由 502（`_BD_RESAMPLE` 未定义）**：删 `_crop_resize` 时按「到下一个 def 为止」
  删除，把紧跟其后、定义在模块级的重采样核常量 `_BD_RESAMPLE = Image.BICUBIC` 一起删了，
  `bd2wgs` / `wgs2bd` 抛 `NameError` → 502（`py_compile` 查不出来，运行到才炸）。已补回。
  **教训**：删函数按函数体范围删，中间可能夹着模块级常量；改完立刻跑冒烟。
- **拒绝「嵌套 URL」的目标地址**（`_build_proxy_target_url`）：目标 URL 里出现第二个 `://`
  直接 400，不再转发。典型成因是前端把「代理地址」拼到了「上游地址」后面，例如
  `...&z={z}http://127.0.0.1:9002/proxy/gcj2wgs/https://webrd01...&z={z}`——上游必然 404，
  原先要重试 3 次再 502（1.4s + 日志刷屏），现在 **17ms 返回 400** 并说清原因。
  检查放在**拼完 query 之后**：嵌套的整段 URL 常落在 query 里，拼之前只有一个 `://`，会漏判。

### 修复

- **`/admin` 在独立部署下无限重定向（ERR_TOO_MANY_REDIRECTS）**：上一版为兜住站点上
  「相对路径拼错」的 `/tile-proxy/tile-proxy.html`，加了一句 `RedirectResponse("/tile-proxy.html")`；
  但独立部署（裸 uvicorn / Docker）里 `/tile-proxy.html` 本身就是可达路径 → **自己跳自己**。
  现在改为「直接返回控制台页面」（不做重定向）：`/admin`、`/tile-proxy.html`、
  `/tile-proxy/tile-proxy.html` 三个入口都是 200。

### 变更

- **回源并发改为进程级闸门**：原先是每个 `_fetch_tile_grid` 各建一个 `Semaphore(16)`，
  一屏 30 张输出同时算能打出去 30×16=480 路，实际只靠 httpx 连接池（默认 80）兜底。
  改为模块级 `_get_fetch_semaphore()`（惰性创建，避免 import 时绑事件循环），
  整个进程同一时刻最多 `GCJRE_MAX_CONCURRENCY` 路回源。实测 3 个 4×4 块并发
  （75 张唯一源片）峰值并发 11 ≤ 16；复用仍是理论最优（16/16、25/25），耗时 0.92s 不变。
- **删除废弃实现**：`_crop_composite`（gcj）、`_crop_resize`（bd）、`_build_blank_tile`（grid）
  均无调用方（`_quad_warp` 早已取代轴对齐裁切），一并清掉约 90 行。
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
- **镜像必须与代码同批重建**：管理控制台是在 `1.1.0` 定稿过程中并入的，早期构建的
  `negiao/tile-proxy:1.1.0` 里没有 `admin_store.py` / `examples/admin.html`，打开 `/admin`
  会返回「管理页缺失」。若你拉到的是旧构建，请重新 `docker build -t negiao/tile-proxy:1.1.0 -t negiao/tile-proxy:latest .`
  后 push，或直接用最新 `latest`。

## 1.0.1 — 2026-09-15

- 发布默认 Docker 镜像 `negiao/tile-proxy:latest`；`docker-compose.yml` 默认拉取预构建镜像，
  无需本地 `build`。

## 1.0.0 — 2026-09-15

- 首个独立开源版本（自 WebGIS-Dev 抽出）
- 功能：GCJ/BD 双向纠偏、通用直通代理、内存+磁盘缓存、限流、SSRF 护栏
- 文档：架构 / 用法 / 本地·nginx·Docker 部署 / 配置
- 默认 CORS `*`
- **Docker Hub**：`negiao/tile-proxy:latest` / `:1.0.0`
