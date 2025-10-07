# 使用轻量级的 Python 官方镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN apt-get update && apt-get install -y curl ca-certificates --no-install-recommends \
	&& pip install --no-cache-dir -r requirements.txt \
	&& apt-get purge -y --auto-remove \
	&& rm -rf /var/lib/apt/lists/*

# 复制应用代码
COPY . .

# 容器启动时运行 app.py，并开启非缓冲模式，以便日志能实时输出
CMD ["python", "-u", "app.py"]
