FROM python:3.10-slim

# 安装必要工具、Ookla CLI 以及 Matplotlib 运行所需的底层图形库 (libfreetype6-dev, libpng-dev)
RUN apt-get update && apt-get install -y curl jq libfreetype6-dev libpng-dev \
    && curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash \
    && apt-get install -y speedtest \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 不需要 COPY 代码，使用 Compose 映射以支持热更新

EXPOSE 5000

CMD ["python", "app.py"]