# ==============================================================================
#  Futures OrderFlow · Dockerfile
#  多阶段构建：deps 层编译依赖 → runtime 层精简镜像
#
#  构建参数：
#    PYTHON_VERSION  Python 版本（默认 3.11）
#    TUSHARE_TOKEN   Tushare Pro token（构建时测试用，不会留在镜像中）
#
#  构建：
#    docker build -t futures-orderflow .
#    docker buildx build --platform linux/amd64,linux/arm64 -t futures-orderflow .
#
#  运行：
#    docker run -d -p 8741:8741 --name futures-orderflow \
#      -v $(pwd)/config:/app/config \
#      -v $(pwd)/data:/app/data \
#      futures-orderflow
# ==============================================================================

# ── 可配置参数 ────────────────────────────────────────────────────────────────
ARG PYTHON_VERSION=3.11

# ── Base 层：共用基础（apt 依赖 + 时区）─────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS base

LABEL org.opencontainers.image.title="Futures OrderFlow"
LABEL org.opencontainers.image.description="期货订单流策略系统"
LABEL org.opencontainers.image.source="https://github.com/alonglong5118-org/futures-orderflow"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.base.name="python:${PYTHON_VERSION}-slim"

# 系统依赖（运行时必需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libopenblas0 \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

# Python 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

# 创建虚拟环境
RUN python -m venv /opt/venv

# ── Builder 层：编译 Python 依赖 ─────────────────────────────────────────────
FROM base AS builder

# 编译依赖（numpy/scipy/hmmlearn 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 升级基础工具
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# 1) 核心依赖（先装，体积大，缓存友好）
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 2) 可选数据源（默认安装）
RUN pip install --no-cache-dir akshare tushare

# 3) 清理 .pyc 和缓存，减小体积
RUN find /opt/venv -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true \
    && find /opt/venv -type f -name "*.pyc" -delete 2>/dev/null || true \
    && find /opt/venv -type f -name "*.pyo" -delete 2>/dev/null || true \
    && find /opt/venv -type d -name "*.dist-info" | while read d; do \
         [ -f "$d/RECORD" ] || continue; \
       done; true

# ── Runtime 层：最终运行镜像 ────────────────────────────────────────────────
FROM base AS runtime

# 从 builder 拷贝虚拟环境（仅运行时需要的二进制和包）
COPY --from=builder /opt/venv /opt/venv

# 创建非 root 用户
RUN groupadd -r appuser && useradd -r -m -g appuser -d /app appuser

WORKDIR /app

# ── 复制应用代码（按变更频率分层，低频在前） ────────────────────────────────

# 工具模块（稳定，极少变更）
COPY kelly_utils.py gap_stop_utils.py take_profit_utils.py price_protection.py \
     corr_gate_utils.py signal_trigger_utils.py risk_gate_utils.py \
     anomaly_scan.py hidden_pivot.py t_score_utils.py ./

# 分析模块（较稳定）
COPY fundamental_feed.py fundamental_metrics.py macro_context.py \
     akshare_live.py tushare_live.py ./

# 核心策略模块（经常变更）
COPY consistency_watchdog.py direction_source_monitor.py event_calendar.py \
     info_dimension.py sr_analyzer.py sentiment_engine.py \
     risk_state_machine.py strategy_layer.py four_dim_strategy.py ./

# 运行器（最常变更）
COPY four_dim_live_runner.py ./

# 配置文件
COPY feature_flags.json calibration_params.json stop_rr_overrides.json ./
COPY tq_config.example.json ./tq_config.json

# 入口脚本
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 权限设置
RUN chown -R appuser:appuser /app
USER appuser

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8741}/api/health >/dev/null 2>&1 || exit 1

# 端口
EXPOSE 8741

# 数据卷
VOLUME ["/app/config", "/app/data", "/app/logs"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["live"]
