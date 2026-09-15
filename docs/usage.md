# 使用说明

先分清你的角色：

| 角色 | 典型目标 | 起点 |
|---|---|---|
| **线上部署** | 站点/APP 统一走 `https://你的域名/proxy/...` | [deployment.md](deployment.md) §B/C → 回来读契约与前端案例 |
| **个人本机** | clone 后本机纠偏，OL/Cesium 连 `127.0.0.1:9002` | [deployment.md](deployment.md) §A → 「索引语义」+ `examples/openlayers-demo.html` |

## 1. 路由契约

基址记作 `BASE`（本地 `http://127.0.0.1:9002`，生产 `https://your-domain`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | `{"ok":true,"service":"tile-proxy"}` |
| GET | `{BASE}/proxy/{host}/{path}?query` | 直通；query 会转发给上游 |
| GET | `{BASE}/proxy/{scheme}://{完整URL}` | 直通完整 URL 形态 |
| GET | `{BASE}/proxy/gcj2wgs/{完整上游URL}` | GCJ 瓦片 → WGS84 对齐 |
| GET | `{BASE}/proxy/wgs2gcj/{完整上游URL}` | WGS84 → GCJ 对齐 |
| GET | `{BASE}/proxy/bd2wgs/{完整上游URL}` | BD-09 → WGS84（标准 XYZ 索引） |
| GET | `{BASE}/proxy/wgs2bd/{完整上游URL}` | WGS84 源 → 百度网格输出 |

错误：

| 状态 | 含义 |
|---|---|
| 400 | URL 解析失败 / 护栏拒绝 / BD 索引越界 |
| 403 | 目标主机被 SSRF 规则拒绝 |
| 413 | 上游响应过大 |
| 429 | 触发 `PROXY_RATE_LIMIT` |
| 502 | 上游失败 |
| 504 | 上游超时 |

响应：`image/png`（纠偏输出恒为 PNG）或上游流式字节（直通）。

---

## 2. 索引语义（必读）

| 端点 | URL 里的 `{x}{y}{z}` |
|---|---|
| `gcj2wgs` | **标准 XYZ**（你的地图引擎索引） |
| `bd2wgs` | **标准 XYZ** |
| `wgs2bd` | **百度网格**索引 |
| 直通 `/proxy/` | 与上游一致，原样转发 |

---

## 3. 前端接入

### 3.1 统一基址（推荐）

```js
const TILE_PROXY = import.meta.env.VITE_TILE_PROXY_BASE_URL || "http://127.0.0.1:9002";

export const tileProxyUrl = (hostAndPath) =>
  `${TILE_PROXY}/proxy/${hostAndPath.replace(/^\/+/, "")}`;

export const gcj2wgsProxyUrl = (upstreamUrl) =>
  `${TILE_PROXY}/proxy/gcj2wgs/${upstreamUrl}`;

export const bd2wgsProxyUrl = (upstreamUrl) =>
  `${TILE_PROXY}/proxy/bd2wgs/${upstreamUrl}`;
```

### 3.2 OpenLayers：纠偏高德

```js
import TileLayer from "ol/layer/Tile";
import XYZ from "ol/source/XYZ";

const layer = new TileLayer({
  source: new XYZ({
    url: gcj2wgsProxyUrl(
      "https://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
    ),
  }),
});
```

### 3.3 Leaflet：直通 Carto（境外服务器）

```js
L.tileLayer(
  tileProxyUrl("a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"),
  { crossOrigin: true }
).addTo(map);
```

### 3.4 MapLibre GL：Google 影像直通

```js
map.addSource("google", {
  type: "raster",
  tiles: [tileProxyUrl("mt1.google.com/vt?lyrs=s&x={x}&y={y}&z={z}")],
  tileSize: 256,
});
```

### 3.5 Cesium

```js
const provider = new Cesium.UrlTemplateImageryProvider({
  url: gcj2wgsProxyUrl(
    "https://webrd01.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}"
  ),
  // Cesium 的 {s} 若上游需要子域，可写 mt{s}.google.com/... 并配 subdomains
});
viewer.imageryLayers.addImageryProvider(provider);
```

### 3.6 fallback：直连失败再走代理

```js
async function loadTile(directUrl, proxiedUrl) {
  try {
    const r = await fetch(directUrl, { mode: "cors" });
    if (!r.ok) throw new Error(r.status);
    return directUrl;
  } catch {
    return proxiedUrl;
  }
}
```

---

## 4. 真实场景案例

### 案例 1：国内用户 + 境外 VPS 拉 Google/Carto

- **问题**：浏览器直连境外源不稳或无 CORS。
- **做法**：VPS 部署 tile-proxy，前端 `tiles: [BASE/proxy/mt1.google.com/vt?...]`。
- **注意**：遵守 Google/图源 ToS；开 `PROXY_RATE_LIMIT`。

### 案例 2：高德电子图纠偏后叠 WGS84 数据（推荐模板）

- **问题**：高德 `webrd0x.is.autonavi.com` 电子/影像是 **GCJ-02**；按标准 XYZ 直接贴到 WGS84 画布会偏移，GPS 轨迹、OSM、Esri 等叠不上。
- **做法**：整段上游 URL 放进 `/proxy/gcj2wgs/`，引擎索引仍是标准 `{x}{y}{z}`。

线上生产模板示例：

```text
https://vpn.negiao.cn/proxy/gcj2wgs/http://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}
```

本地：

```text
http://127.0.0.1:9002/proxy/gcj2wgs/http://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}
```

OpenLayers 完整片段：

```js
const PROXY = "https://vpn.negiao.cn"; // 或 http://127.0.0.1:9002
const amapGcj =
  "http://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}";

const basemap = new ol.layer.Tile({
  source: new ol.source.XYZ({
    url: `${PROXY}/proxy/gcj2wgs/${amapGcj}`,
  }),
});

// WGS84 数据层（轨迹 / POI）坐标保持原样，即可对齐
const track = new ol.layer.Vector({ /* GeoJSON in EPSG:4326 / 3857 */ });
```

说明：`style=8` 电子图；换影像或注记 style 时 **只改上游 query，纠偏前缀不变**。返回瓦片已是 WGS 对齐 PNG。

### 案例 3：只想要百度底图风格，但数据全是 WGS84

- **做法**：百度瓦片模板经 `bd2wgs`（索引用标准 XYZ），数据层不动。

### 案例 4：内网 GeoServer 无 CORS（仅内网部署时）

- **问题**：浏览器拉 GetCapabilities 失败。
- **做法**：若 tile-proxy 与 GeoServer 同内网，可临时 `PROXY_ALLOW_PRIVATE_HOSTS=true`（**公网禁止**），或用 nginx 另做内网反代。

### 案例 5：给静态站点加底图，不想暴露真实 key

- **做法**：若上游需要 key，可把 key 放在 VPS 的 nginx/环境里做 URL 改写；**不要**把可滥用的 key 写进前端。tile-proxy 本身不存 key，只转发你拼好的 URL。

---

## 5. 与地图引擎的 z/x/y 对齐检查清单

1. 先用引擎原生 XYZ 直连一张已知城市瓦片，确认能显示。
2. 把同一模板套 `gcj2wgs` / 直通，对比地标（广场、河流拐点）。
3. 百度源务必用 `bd2wgs`，不要假设与标准 XYZ 同网格。
4. 纠偏首屏略慢（拼源片）；二次访问走缓存应明显变快。

## 6. 调试

```bash
# 看响应头与体积
curl -D - -o /tmp/t.png -w "code=%{http_code} size=%{size_download}\n" \
  "http://127.0.0.1:9002/proxy/gcj2wgs/https://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=6920&y=3077&z=13"

# 日志
LOG_LEVEL=DEBUG uvicorn app:app --port 9002
```

磁盘缓存默认在 `GCJRE_CACHE`；删目录可强制回源。
