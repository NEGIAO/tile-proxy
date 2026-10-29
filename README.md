# tile-proxy

[![在线演示](https://img.shields.io/badge/demo-online-brightgreen)](https://vpn.negiao.cn/tile-proxy/)
[![版本](https://img.shields.io/badge/version-1.1.0-blue)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-GPL--2.0--or--later-green)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-negiao%2Ftile--proxy-2496ED)](https://hub.docker.com/r/negiao/tile-proxy)

<h3 align="center">
  <a href="https://vpn.negiao.cn/tile-proxy/">在线体验 · vpn.negiao.cn/tile-proxy/</a>
</h3>

<p align="center">
打开就是联调页，六个底图示例一键切换，不用装任何东西。<br>
演示站限流较紧（每 IP 每分钟 400 次，够看效果、不够批量抓），长期使用请自己部署。
</p>

**地图瓦片直通 + GCJ-02 / BD-09 → WGS84 纠偏。** FastAPI 单进程，1GB VPS 够用：把国内加偏瓦片
逐瓦片重采样成标准 WGS84 XYZ，也能让境外服务器代拉 Google / Carto 等瓦片并解决 CORS。

## 能做什么

| 场景 | 端点 |
|---|---|
| 浏览器拉不动境外瓦片 / 无 CORS | `GET /proxy/{host+path}` |
| 高德等 GCJ 瓦片要和 GPS / OSM 对齐 | `GET /proxy/gcj2wgs/{上游URL}` |
| 百度 BD-09 瓦片要和 WGS84 对齐 | `GET /proxy/bd2wgs/{上游URL}`（标准 XYZ 索引） |
| 反向：WGS 源贴到 GCJ / 百度网格 | `GET /proxy/wgs2gcj` · `/proxy/wgs2bd` |

另有 `/`（联调示例页）、`/health`（探活）、`/admin`（管理控制台）。

同一组 GCJ 源瓦片，左为按标准 XYZ 直接贴（整城偏移），右为经 `gcj2wgs` 输出的 WGS84 对齐结果：

| 纠偏前 | 纠偏后 |
|:---:|:---:|
| ![纠偏前：GCJ 瓦片直贴](demo/纠偏前.png) | ![纠偏后：gcj2wgs 输出](demo/纠偏后.png) |

## 快速开始

**方式 1 · 直接用已发布镜像（推荐，免构建、免装 Python）**

```bash
docker run -d --name tile-proxy -p 127.0.0.1:9002:9002 \
  -v tile-proxy-cache:/data/cache \
  -v tile-proxy-admin:/data/admin \
  negiao/tile-proxy:1.1.0
curl http://127.0.0.1:9002/health
```

镜像 [`negiao/tile-proxy`](https://hub.docker.com/r/negiao/tile-proxy)，标签 `latest` / `1.1.0` / `1.0.0`，
自带 `HEALTHCHECK`（`docker ps` 会显示 `healthy`）。镜像里已含管理控制台、内置联调页与纠偏依赖，
起来就能用，不需要装 Python 或改任何配置。仓库里的 `docker-compose.yml` 默认也是拉镜像，
所以 clone 下来直接 `docker compose up -d` 就行，不需要本地 build。

两个具名卷：`/data/cache` 存纠偏磁盘缓存，`/data/admin` 存管理配置（口令哈希 + 31 项运行时配置）。
**删 `tile-proxy-admin` 等于重置管理口令。**

**方式 2 · 从源码跑**

```bash
git clone https://github.com/NEGIAO/tile-proxy.git && cd tile-proxy
python3 -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 9002
```

想自己构建镜像：把 `docker-compose.yml` 里的 `image:` 换成 `build: .`，再 `docker compose up -d --build`。

打开 `http://127.0.0.1:9002/` 即内置联调页，切到「高德 gcj2wgs」就能看到纠偏前后差异。

生产部署（nginx 反代、systemd、升级、安全清单）→ [docs/deployment.md](docs/deployment.md)

## 使用方法

把上游模板整条接在前缀后面，地图引擎仍用标准 `{x}/{y}/{z}`：

```text
http://127.0.0.1:9002/proxy/gcj2wgs/http://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}
```

```js
// OpenLayers：此时 GPS / GeoJSON / OSM 叠加层的坐标不用做任何转换
new ol.layer.Tile({
  source: new ol.source.XYZ({ url: "<上面那条 URL>" }),
});
```

索引语义、Leaflet / MapLibre / Cesium 接法、五个真实案例 → [docs/usage.md](docs/usage.md)

## 管理控制台

`/admin` 一个页面覆盖说明、运行状态与**全部运行配置**——限流、缓存、纠偏护栏、网络、安全共 31 项，
改完保存即生效，不必重启、不必改 env（配置覆盖优先于环境变量）。

首次打开设置口令（不内置默认口令；无人值守部署可用 `TILE_PROXY_ADMIN_PASSWORD` 预置），
之后用 12 小时令牌，改口令即让旧令牌全部失效。配置项与 HTTP 接口 → [docs/configuration.md](docs/configuration.md)

## 文档

| 文档 | 内容 |
|---|---|
| [docs/deployment.md](docs/deployment.md) | Docker / systemd / nginx、反代与缓存、升级、安全清单 |
| [docs/usage.md](docs/usage.md) | 路由契约、索引语义、前端接入、五个真实案例、调试 |
| [docs/configuration.md](docs/configuration.md) | 全部配置项与默认值、管理接口、限流与缓存策略 |
| [docs/architecture.md](docs/architecture.md) | 纠偏原理、GCJ / BD 网格、模块结构、已知约束 |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |

原理延伸（站外笔记）：[非标准 XYZ 与 GCJ-02 瓦片](https://www.negiao.cn/Pages/Note/note-viewer/note-viewer.html?note=non-standard-xyz-and-gcj02) ·
[瓦片纠偏接缝与 QUAD 重采样](https://www.negiao.cn/Pages/Note/note-viewer/note-viewer.html?note=tile-rectify-seam-quad-resampling)

## 合规

- 仅代理你有权访问的公开资源；不要做成开放代理。
- 默认禁止代访内网；公网部署请开限流。
- GCJ 算法源自 QGIS OffsetWGS84Core（GPLv2+），见 `domains/tiles/rectify/common/transform.py` 与 [LICENSE](LICENSE)。
- 不要把本服务部署在禁止第三方内容中转的托管平台（如 HF Spaces），用自己的 VPS。

## License

GPL-2.0-or-later，见 [LICENSE](LICENSE)。
