# ==============================================================================
#  Futures OrderFlow · Dockerfile
#  多阶段构建：builder 层编译依赖 → runtime 层精简镜像
#
#  构建：
#    docker build -t futures-orderflow .
#    docker build --build-arg PYTHON_VERSION=3.11 -t futures-orderflow .
#
#  运行：
#    docker run -d -p 8741:8741 --name futures-orderflow \
#      -v $(pwd)/config:/app/config \
#      -v $(pwd)/data:/app/data \
#      futures-orderflow
# ==============================================================================

# ── Builder 阶段：编译依赖 ───────────────────────────────────────────────────
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim AS builder

LABEL org.opencontainers.image.title="Futures OrderFlow"
LABEL org.opencontainers.image.description="期货订单流策略系统"
LABEL org.opencontainers.image.source="https://github.com/alonglong5118-org/futures-orderflow"
LABEL org.opencontainers.image.licenses="MIT"

# 安装编译依赖（numpy/scipy/hmmlearn 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    && rm -rf /var/lib/apt/lists/*

# 创建虚拟环境并安装依赖
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 升级 pip
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# 先安装核心依赖（体积大，单独分层利用缓存）
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 可选数据源（默认安装 akshare，tqsdk 按需安装）
RUN pip install --no-cache-dir akshare tushare

# ── Runtime 阶段：精简运行镜像 ───────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

# 运行时依赖（OpenBLAS 等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopenblas0 \
    curl \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

# 从 builder 拷贝虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 创建非 root 用户
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

# 工作目录
WORKDIR /app

# 复制应用代码（先复制依赖清单，利用缓存）
COPY requirements.txt ./
COPY requirements-dev.txt ./

# 复制核心代码
COPY four_dim_strategy.py ./
COPY strategy_layer.py ./
COPY risk_state_machine.py ./
COPY consistency_watchdog.py ./
COPY direction_source_monitor.py ./
COPY event_calendar.py ./
COPY info_dimension.py ./
COPY sr_analyzer.py ./
COPY sentiment_engine.py ./

# 工具模块
COPY kelly_utils.py ./
COPY gap_stop_utils.py ./
COPY take_profit_utils.py ./
COPY price_protection.py ./
COPY corr_gate_utils.py ./
COPY signal_trigger_utils.py ./
COPY risk_gate_utils.py ./
COPY anomaly_scan.py ./
COPY hidden_pivot.py ./
COPY t_score_utils.py ./

# 数据源模块
COPY fundamental_feed.py ./
COPY fundamental_metrics.py ./
COPY macro_context.py ./
COPY akshare_live.py ./
COPY tushare_live.py ./

# 实盘运行器
COPY four_dim_live_runner.py ./

# 配置文件（必须入库的核心配置）
COPY feature_flags.json ./
COPY calibration_params.json ./
COPY stop_rr_overrides.json ./
COPY tq_config.example.json ./tq_config.json

# 入口脚本
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# 权限
RUN chown -R appuser:appuser /app
USER appuser

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8741}/api/health || exit 1

# 端口
EXPOSE 8741

# 数据卷
VOLUME ["/app/config", "/app/data", "/app/logs"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["live"]
