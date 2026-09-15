# tile-proxy

独立开源的 **地图瓦片纠偏 / 直通代理**（FastAPI）。

从 WebGIS 项目抽出，可单独部署到任意 VPS；**不依赖** WebGIS 后端。

## 功能

| 路由 | 说明 |
|---|---|
| `GET /proxy/gcj2wgs/{url}` | GCJ-02 瓦片 → WGS84 对齐 |
| `GET /proxy/wgs2gcj/{url}` | WGS84 → GCJ-02 对齐 |
| `GET /proxy/bd2wgs/{url}` | BD-09 → WGS84（跨网格重采样） |
| `GET /proxy/wgs2bd/{url}` | WGS84 → BD-09 网格 |
| `GET /proxy/{host+path}` | 通用直通代理（浏览器兼容头 / Referer） |
| `GET /tiles/ships66/{z}/{x}/{y}.png` | 专用海图模板（可选） |
| `GET /health` | 健康检查 |

CORS 默认 `*`（任意前端源）。

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 9002 --workers 1
```

示例：

```text
http://127.0.0.1:9002/proxy/gcj2wgs/https://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=6920&y=3077&z=13
```

## 目录

```
app.py                 FastAPI 入口（CORS + tiles_router + cache cleanup）
config.py              薄 env 配置
domains/tiles/         纠偏库 + 路由 + SSRF/出站头（不含 download）
requirements.txt       fastapi / uvicorn / httpx / pillow
```

## 配置（节选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY_TILE_CACHE_MAX_SIZE` | 100000 | 内存缓存条目；**小内存机器请改 3000** |
| `PROXY_RATE_LIMIT` | 0 | 每 IP 每分钟请求；公网建议 180 |
| `GCJRE_CACHE` | `backend/data/gcj_rectify_cache` | 磁盘缓存目录 |
| `GCJRE_CACHE_MAX_MB` | 2048 | 磁盘容量上限 |
| `GCJRE_CACHE_MAX_AGE_DAYS` | 7 | 按龄清理 |
| `PROXY_ALLOW_PRIVATE_HOSTS` | false | 禁止代访内网（保持 false） |

## 生产部署提示

- 单 worker；内存紧张时设 `MemoryMax` 并压低内存缓存
- 反代示例见 `deploy/nginx-location.conf`
- systemd 示例见 `deploy/tile-proxy.service`

## 许可

纠偏坐标算法源自 QGIS OffsetWGS84Core（GPLv2+），见 `domains/tiles/rectify/common/transform.py` 文件头。其余代码按仓库根 LICENSE。
