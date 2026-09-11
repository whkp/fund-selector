# 通用容器镜像：Hugging Face Spaces 或任何支持 Docker 的托管平台。
#
# 本地验证：
#   docker build -t fund-compass-api .
#   docker run --rm -p 7860:7860 fund-compass-api
#   curl http://127.0.0.1:7860/health
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

# 依赖单独成层：只改业务代码时不会触发重新安装。
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 保持仓库原有的相对路径，使 backend/app/config.py 的 PROJECT_ROOT 解析到 /app，
# 从而正确找到 config/fund-compass.json。
COPY backend/ ./backend/
COPY config/ ./config/

# Hugging Face Spaces 只对外暴露 7860；其它平台可用 FUND_COMPASS_API_ADDR 覆盖。
ENV FUND_COMPASS_API_ADDR=0.0.0.0:7860 \
    FUND_COMPASS_MODE=REFERENCE \
    FUND_COMPASS_DATABASE_URL=sqlite+aiosqlite:///./data/fund-compass.db

EXPOSE 7860

# backend/run.py 读取 FUND_COMPASS_API_ADDR 决定监听地址。
CMD ["python", "backend/run.py"]
