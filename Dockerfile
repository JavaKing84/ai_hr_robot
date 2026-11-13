FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Shanghai

WORKDIR /app

# curl 用于容器健康检查；libmagic/poppler 供 PDF/DOCX 解析兜底使用
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libmagic1 poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# 依赖只由 pyproject.toml + uv.lock 决定，与本地开发同一套
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

RUN mkdir -p var/resumes var/packages var/outbox/mail var/mock_inbox

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uv", "run", "--no-dev", "python", "-m", "core.main"]
