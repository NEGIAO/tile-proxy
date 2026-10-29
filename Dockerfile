# syntax=docker/dockerfile:1
FROM python:3.12-slim

# OCI 元数据（版本随 pyproject.toml / app.py / CHANGELOG 同步）
LABEL org.opencontainers.image.title="tile-proxy" \
      org.opencontainers.image.description="Map tile passthrough proxy and GCJ-02/BD-09 rectification (FastAPI)" \
      org.opencontainers.image.version="1.1.0" \
      org.opencontainers.image.licenses="GPL-2.0-or-later" \
      org.opencontainers.image.source="https://github.com/NEGIAO/tile-proxy"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=9002

# 国内默认走清华源；境外构建用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i "$PIP_INDEX_URL"

# 通配复制根目录模块：新增 app 级模块（如 cache_policy/origin_guard）无需改 Dockerfile
COPY *.py ./
COPY domains ./domains

# 生产护栏默认（可用 -e 覆盖）
ENV PROXY_TILE_CACHE_MAX_SIZE=3000 \
    PROXY_TILE_CACHE_TTL_SECONDS=180 \
    PROXY_RATE_LIMIT=180 \
    GCJRE_MAX_CONCURRENCY=4 \
    GCJRE_CACHE=/data/cache \
    GCJRE_CACHE_MAX_MB=512 \
    GCJRE_CACHE_MAX_AGE_DAYS=3 \
    PROXY_ALLOW_PRIVATE_HOSTS=false

RUN mkdir -p /data/cache
VOLUME ["/data/cache"]

EXPOSE 9002

# slim 镜像没有 curl，用 Python 标准库探活（避免为健康检查多装一层依赖）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9002/health', timeout=3).read()" || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9002", "--workers", "1"]
