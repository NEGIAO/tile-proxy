# 部署指南

按两类用户组织：

| 你是谁 | 直接看 |
|---|---|
| **要上线长期服务**（域名/站点/团队） | §B nginx+systemd 或 §C Docker |
| **个人本机试用/开发**（clone 即纠偏） | §A 本地 venv（或 §C 的 compose） |

通用建议（小内存 VPS）：

- **workers=1**
- `PROXY_TILE_CACHE_MAX_SIZE=3000`
- `PROXY_RATE_LIMIT>0`
- `PROXY_ALLOW_PRIVATE_HOSTS=false`
- 磁盘缓存目录放在代码树外（如 `/var/cache/tile-proxy`）

---

## A. 个人本机（clone → 立刻能纠偏）

目标：本机 `http://127.0.0.1:9002`，地图引擎直接连，得到 WGS84 对齐瓦片。

### A.1 Python venv

```bash
git clone <repo> tile-proxy
cd tile-proxy
python3 -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

uvicorn app:app --host 127.0.0.1 --port 9002
```

自检：

```bash
curl http://127.0.0.1:9002/health
curl -o g.png "http://127.0.0.1:9002/proxy/gcj2wgs/https://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=6920&y=3077&z=13"
```

浏览器打开 `examples/openlayers-demo.html`，基址填 `http://127.0.0.1:9002`，点「高德 gcj2wgs」。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 9002
```

本地磁盘缓存默认在 `./data/gcj_rectify_cache`，可整夹删除强制回源。

### A.2 本机 Docker（不装 Python 时）

**推荐：直接用 Docker Hub 镜像**

```bash
docker run -d --name tile-proxy -p 127.0.0.1:9002:9002 \
  -e PROXY_TILE_CACHE_MAX_SIZE=3000 -e PROXY_RATE_LIMIT=180 \
  negiao/tile-proxy:latest
```

或 compose（默认即 `negiao/tile-proxy:latest`）：

```bash
docker compose up -d
```

从源码构建时：在 `docker-compose.yml` 里改回 `build: .`，或：

```bash
docker build -t tile-proxy . && docker run --rm -p 127.0.0.1:9002:9002 tile-proxy
```

个人使用一般不必改 env；要压内存再抄 `.env.example`。

---

## B. 生产：nginx + systemd（推荐）

### B.1 安装代码与 venv

```bash
sudo mkdir -p /opt/tile-proxy /var/cache/tile-proxy
sudo chown "$USER":"$USER" /opt/tile-proxy /var/cache/tile-proxy
# 将仓库内容放到 /opt/tile-proxy

cd /opt/tile-proxy
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

或使用仓库内脚本：

```bash
sudo bash deploy/install.sh /opt/tile-proxy
```

### B.2 systemd

```bash
sudo cp deploy/tile-proxy.service /etc/systemd/system/
# 按需改 WorkingDirectory / ExecStart / Environment
sudo systemctl daemon-reload
sudo systemctl enable --now tile-proxy
systemctl status tile-proxy
curl -s http://127.0.0.1:9002/health
```

`deploy/tile-proxy.service` 要点：

- `User=www-data` 或专用用户
- `--workers 1`
- `MemoryMax=220M`（与同机其它服务共存时）
- Environment 写入缓存/限流护栏

### B.3 nginx

**方式 1：挂到已有站点**（推荐，TLS 已就绪）

把 `deploy/nginx-location.conf` 中的 `location /proxy/` 合入已有 `server` 块，然后：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**方式 2：独立子域**（示例见 `deploy/nginx-standalone.conf`）

1. DNS：`tiles.example.com` → VPS IP  
2. certbot 签证书  
3. 启用配置并 reload  

### B.4 防火墙

只需对公网开 **80/443**；**9002 仅监听 127.0.0.1**，不要对公网裸奔。

### B.5 验证公网

```bash
curl -D - -o /tmp/t.png "https://tiles.example.com/proxy/a.basemaps.cartocdn.com/rastertiles/voyager/13/6920/3077.png"
curl -s https://tiles.example.com/health   # 若配置了 /health 反代
```

---

## C. Docker

镜像仓库：[`negiao/tile-proxy`](https://hub.docker.com/r/negiao/tile-proxy)

| 标签 | 说明 |
|---|---|
| `negiao/tile-proxy:latest` | 最新稳定 |
| `negiao/tile-proxy:1.0.0` | 首个开源版本 |

### C.1 单容器（预构建镜像）

```bash
docker run -d --name tile-proxy \
  -p 127.0.0.1:9002:9002 \
  -e PROXY_TILE_CACHE_MAX_SIZE=3000 \
  -e PROXY_RATE_LIMIT=180 \
  -e GCJRE_CACHE=/data/cache \
  -v tile-proxy-cache:/data/cache \
  --memory=256m \
  negiao/tile-proxy:latest
```

从源码构建：

```bash
docker build -t tile-proxy .
docker run -d --name tile-proxy -p 127.0.0.1:9002:9002 tile-proxy
```

### C.2 compose

```bash
docker compose up -d
curl http://127.0.0.1:9002/health
```

`docker-compose.yml` 默认 `image: negiao/tile-proxy:latest`。

生产仍建议前面加 nginx/Caddy 做 TLS 与域名；compose 里端口绑 `127.0.0.1`。

### C.3 与宿主机 nginx 组合

```nginx
location /proxy/ {
    proxy_pass http://127.0.0.1:9002;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}
```

CORS 由应用返回 `Access-Control-Allow-Origin: *`，nginx 原样传递即可。

---

## D. 升级与运维

```bash
cd /opt/tile-proxy
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart tile-proxy
```

日志：

```bash
journalctl -u tile-proxy -f
```

磁盘缓存：

```bash
du -sh /var/cache/tile-proxy
# 清空
sudo rm -rf /var/cache/tile-proxy/*
```

监控建议：`/health` 探活 + 磁盘水位 + 出口流量。

---

## E. 安全清单

- [ ] 9002 不对公网监听  
- [ ] `PROXY_RATE_LIMIT` > 0  
- [ ] `PROXY_ALLOW_PRIVATE_HOSTS=false`  
- [ ] 如需收紧，可设 `PROXY_ALLOWED_HOSTS` 白名单  
- [ ] 定期看访问日志，防止被当成开放代理滥用  
- [ ] 遵守上游图源 ToS  

---

## F. 平台说明（Hugging Face 等）

在 **HF Spaces 等托管平台上跑本服务可能违反其 Content Policy**（禁止第三方内容中转）。请将本服务部署在 **你自己的 VPS/服务器**；业务前端/纯 API 可留在托管平台，瓦片基址指向你的 VPS。
