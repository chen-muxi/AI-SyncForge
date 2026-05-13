# 使用轻量级 Python 基础镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（如 SQLite 等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 预先创建数据目录，防止 SQLite 因目录不存在而报错
RUN mkdir -p /app/data

# 暴露 MCP 端口（默认 8000）
EXPOSE 8000

# 启动命令
CMD ["python", "server.py"]
