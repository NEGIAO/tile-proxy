# tile-proxy examples

| 文件 | 说明 |
|---|---|
| `openlayers-demo.html` | 服务内置首页（`GET /`）。六个示例（OSM / Google 影像 / 高德·百度「原始」直连 / 高德 gcj2wgs / 百度 bd2wgs）点一下就把该图层**实际加载的 XYZ 模板 URL** 填进输入框，可手改后回车加载，也可直接复制到 OL / Leaflet / Cesium |
| `data/forbidden-city-buildings.geojson` | 故宫建筑白模（3896 个面，WGS84），首页自动加载作对照层；由 `GET /demo/data/{name}` 提供 |

该页由 `app.py` 的 `/` 路由返回，容器镜像里也会带（`Dockerfile: COPY examples ./examples`），
所以启动服务后直接访问根路径即可：

```bash
uvicorn app:app --port 9002
# 浏览器打开 http://127.0.0.1:9002/
```

页面基址默认取当前站点（`location.origin`），因此换端口、走 nginx 反代、换域名都不用改；
用 `file://` 直接打开时才回落到 `http://127.0.0.1:9002`。
Google 影像示例需要服务端能访问 `mt0.google.com`（境外 VPS 常见），国内直连会拿不到瓦片。

> 页面依赖 `cdn.jsdelivr.net` 上的 OpenLayers；离线环境请把 `ol.js` / `ol.css` 本地化后改两处 `<script>` / `<link>`。
