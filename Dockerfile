# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=9002

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY app.py config.py ./
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
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9002", "--workers", "1"]
