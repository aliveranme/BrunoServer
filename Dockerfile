# 使用官方 Python 运行时
FROM python:3.11.15-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt requirements.txt

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY server.py server.py

# 暴露端口
EXPOSE 5000

# 默认许可证配置（可通过 docker run -e 覆盖）
ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=5000
ENV BRUNO_LICENSE_PLAN=ULTIMATE_EDITION
ENV BRUNO_LICENSE_TYPE=personal

# 启动应用
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "--access-logfile", "-", "server:app"]
