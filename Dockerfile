# 只从这个固定版本的官方镜像中取得uv程序。
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv_source

# 使用Python 3.12和Debian Bookworm精简环境。
FROM python:3.12-slim-bookworm

# 将uv复制到系统命令目录，不需要额外运行安装脚本。
COPY --from=uv_source /uv /usr/local/bin/uv

# 立即输出日志，不生成.pyc，并禁止uv额外下载Python。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# 与本机项目根目录保持相同的相对路径结构。
WORKDIR /app

# .dockerignore会排除Windows虚拟环境、测试和缓存。
COPY backend/ /app/backend/

# 使用镜像内的Python，按现有锁文件安装生产依赖和项目。
RUN uv sync --project backend --locked --no-dev --no-editable \
    --python /usr/local/bin/python \
    && uv cache clean

# 让python、uvicorn和alembic优先使用项目虚拟环境。
ENV PATH="/app/backend/.venv/bin:$PATH"

# 创建普通运行账号，后端进程不使用root身份。
RUN useradd --create-home --uid 10001 --user-group app

ENV HOME=/home/app
USER app

# 应用在容器内部监听8000端口。
EXPOSE 8000

# Linux环境启动一个后端进程，控制2GB服务器的基础资源占用。
CMD ["uvicorn", "ci_failure_intelligence.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
