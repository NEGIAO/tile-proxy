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

浏览器打开 `http://127.0.0.1:9002/`（内置首页），点「高德 gcj2wgs」。

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

从源码构建时：在 `docker-compose.yml` 里改回 `build: .` 后 `docker compose up -d --build`，或：

```bash
docker build -t tile-proxy:1.1.0 . && docker run --rm -p 127.0.0.1:9002:9002 tile-proxy:1.1.0
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
| `negiao/tile-proxy:1.1.0` | 管理控制台（31 项运行配置）+ 内置联调页；修复纠偏输出永久白斑、统一 Cache-Control、移除 ships66 |
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

从源码构建（`--build-arg PIP_INDEX_URL=https://pypi.org/simple` 可切回官方源）：

```bash
docker build -t tile-proxy:1.1.0 .
docker run -d --name tile-proxy -p 127.0.0.1:9002:9002 tile-proxy:1.1.0
docker inspect --format '{{.State.Health.Status}}' tile-proxy   # 镜像自带 HEALTHCHECK
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
# 二级瓦片缓存（可选）。1.1.0 起不合格瓦片带 X-Tile-Blank: 1，
# 必须让 nginx 跳过缓存，否则白斑会在本地缓存里留一整个 TTL。
proxy_cache_path /var/cache/nginx/tiles levels=1:2 keys_zone=tiles:32m
                 max_size=2g inactive=7d use_temp_path=off;

location /proxy/ {
    proxy_pass http://127.0.0.1:9002;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;

    proxy_cache tiles;
    proxy_cache_valid 200 7d;
    proxy_cache_use_stale error timeout updating http_500 http_502 http_503 http_504;
    proxy_cache_background_update on;
    # 两个标记都跳过缓存：X-Tile-Blank = 空白/不合格瓦片，X-Tile-NoCache = 限流提示图
    proxy_no_cache $upstream_http_x_tile_blank $upstream_http_x_tile_no_cache;
    proxy_cache_bypass $upstream_http_x_tile_blank $upstream_http_x_tile_no_cache;
    add_header X-Cache-Status $upstream_cache_status;
}
```

CORS 由应用返回（默认 `Access-Control-Allow-Origin: *`），nginx 原样传递即可。

### C.4 CDN 与提示瓦片：`no-store` 不一定被认（2026-10-29 实测）

限流时服务端默认返回 **203 + `error1.png`**（`Cache-Control: no-store` + `X-Tile-Blank: 1` +
`X-Tile-NoCache: 1`）；可在首页把 `PROXY_RATE_LIMIT_MODE` 切成 `image-200` 或 `json-429`。
203 的好处是「能渲染但不在默认可缓存状态码里」：nginx `proxy_cache_valid 200` 和 CF 的
默认可缓存状态码（200/206/301/302/303/404/410）都不含 203，等于多了一层天然保险。nginx 这一层一定能拦住，但 **CDN 未必**——如果 CDN 的缓存规则
「忽略源站缓存头 + 用固定 TTL」，那么这张提示图会被按 URL 缓存整个 TTL（可能 7 天），
**限流早就解除了，用户还在看「429 速率受限」**。

实测（某 Cloudflare 配置，规则把 `/proxy/*` 设为 Cache Everything + 固定 Edge TTL）：

| 变体 | 第 1 次 | 第 2 次 | 结论 |
|---|---|---|---|
| `no-store` | MISS | **HIT** | 被缓存 |
| `no-store, private` | MISS | **HIT** | 被缓存 |
| `no-store` + `Set-Cookie` | MISS | **HIT** | 被缓存 |

也就是说：**源站加什么头都拦不住**，只能在 CDN 侧改规则。两种改法（任选一）：

1. **推荐**：缓存规则里把 **Edge TTL 改成「Use origin cache-control」**（旧版 Page Rules 则是
   Caching → Configuration → 打开 **Origin Cache Control**，并把 Edge Cache TTL 设为
   "Use origin cache-control"）。瓦片仍按源站的 `s-maxage=604800` 缓存 7 天，
   `no-store` 的提示图则不会被缓存。
2. **最稳**：给 `/proxy/*` 设 **Bypass cache**。瓦片全部回源，由 nginx 的 7 天本地缓存兜住，
   带宽略增但语义最干净。

**验证方法**（改完 CDN 规则后跑一遍，两次用同一个 `_cb`）：

```bash
U="https://你的域名/proxy/gcj2wgs/https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x=53956&y=24830&z=16&__cache_probe=1&_cb=test1"
curl -sD - -o /dev/null "$U" | grep -i cf-cache-status   # 第 1 次：MISS / DYNAMIC
curl -sD - -o /dev/null "$U" | grep -i cf-cache-status   # 第 2 次：仍应 MISS/DYNAMIC（不是 HIT）
```

`__cache_probe=1` 是内置的**缓存诊断探针**：返回一张 `200 + no-store` 的提示图，
专门用来验证 CDN 到底认不认 `no-store`。不带这个参数时完全不可达，正常请求不受影响。


---

## D. 升级与运维

> **备份**：`data/admin.db` 里存着管理密码哈希和所有运行时配置（限流速率、TTL、开关…），
> 升级/迁移前请一并备份；Docker 部署它在 `/data/admin` 卷上（compose 名 `tile-proxy-admin`）。


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
