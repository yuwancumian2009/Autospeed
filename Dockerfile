FROM python:3.10-slim

# 安装必要工具和底层图形库
RUN apt-get update && apt-get install -y curl jq libfreetype6-dev libpng-dev \
    && curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash \
    && apt-get install -y speedtest \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷贝 requirements.txt 并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 【核心修复部分】：把你的后端代码和前端网页正式打包进镜像内部！
COPY app.py .
COPY templates/ ./templates/

EXPOSE 5000

CMD ["python", "app.py"]
