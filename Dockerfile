FROM python:3.10-slim

# 时区 + matplotlib 运行所需底层图形库
ENV TZ=Asia/Shanghai DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg libfreetype6 libpng16-16 \
    && rm -rf /var/lib/apt/lists/*

# 安装 Ookla speedtest CLI（带重试，构建不再因网络抖动失败）
RUN for i in 1 2 3; do \
        curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh -o /tmp/ookla.sh \
        && bash /tmp/ookla.sh \
        && apt-get install -y --no-install-recommends speedtest \
        && break || sleep 5; \
    done \
    && speedtest --version \
    && rm -rf /var/lib/apt/lists/* /tmp/ookla.sh

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py backends.py ./
COPY templates/ ./templates/
COPY static/ ./static/

EXPOSE 5000

HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=20s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/healthz',timeout=5)"

# 单 worker + 多线程：APScheduler 只能有一份，避免重复调度
# 注意：exec 形式 CMD 必须是单行，跨行会让 docker 解析失败
CMD ["gunicorn", "--workers", "1", "--threads", "8", "--timeout", "600", "--graceful-timeout", "30", "--bind", "0.0.0.0:5000", "app:app"]
