FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行；/data 用于存储用户生成的文件（compose 挂载命名卷）
RUN useradd -r -u 10001 -m appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# 单 worker：在途请求计数与限流为进程内状态。
# --proxy-headers + --forwarded-allow-ips=*：api 仅经内网 nginx 可达，
# 信任其转发的 X-Forwarded-*，保证域名反代下 request.base_url / 协议正确。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips=*"]
