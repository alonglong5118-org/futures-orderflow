# Docker 部署

本文档介绍如何使用 Docker 和 Docker Compose 一键部署 Futures OrderFlow 策略系统。

## 快速开始

### 前置条件

| 工具 | 最低版本 | 说明 |
|------|---------|------|
| Docker | 20.10+ | 容器运行时 |
| Docker Compose | 2.0+ | 容器编排 |

!!! tip "安装 Docker"
    - macOS: 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)
    - Linux: `curl -fsSL https://get.docker.com | sh`
    - Windows: 安装 Docker Desktop（推荐 WSL2 后端）

### 一行命令启动

```bash
# 克隆项目
git clone https://github.com/alonglong5118-org/futures-orderflow.git
cd futures-orderflow

# 复制配置模板
cp .env.example .env

# 一键启动
docker compose up -d
```

启动后访问 `http://localhost:8741` 查看策略面板。

---

## 配置说明

### 环境变量

复制 `.env.example` 为 `.env`，根据需要修改以下配置：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PYTHON_VERSION` | `3.11` | Python 版本（构建时使用） |
| `TAG` | `latest` | 镜像标签 |
| `PORT` | `8741` | 面板端口（宿主机映射） |
| `MODE` | `watchdog` | 运行模式：`watchdog`（自动重启）/ `direct`（前台直连） |
| `NO_VOICE` | `0` | 关闭语音：`0`=开启，`1`=关闭 |
| `WATCHDOG_STALL` | `90` | 看门狗卡死判定秒数 |
| `WATCHDOG_RESTART` | `4` | 看门狗重启间隔（秒） |
| `TUSHARE_TOKEN` | _(空)_ | Tushare Pro Token（可选，不填则降级 akshare） |
| `DOCS_PORT` | `8742` | 文档站端口（启用 docs profile 时有效） |

### 配置文件挂载

将自定义配置文件放在 `config/` 目录下，容器启动时会自动软链接：

```
config/
├── tq_config.json          # 天勤配置（账户、合约等）
├── trade_config.json       # 交易配置
├── main_overrides.json     # 主策略覆盖配置
└── watchlist.json          # 自选品种列表
```

### 数据卷

| 宿主机路径 | 容器路径 | 说明 |
|-----------|---------|------|
| `./config` | `/app/config` | 配置文件（只读） |
| `./data` | `/app/data` | 运行时数据（状态、缓存） |
| `./logs` | `/app/logs` | 日志文件 |

---

## 常用操作

### 使用 Makefile（推荐）

项目 Makefile 提供了便捷的 Docker 命令：

```bash
make docker-build       # 构建镜像
make docker-up          # 启动服务
make docker-up-docs     # 启动服务 + 文档站
make docker-down        # 停止服务
make docker-logs        # 查看实时日志
make docker-logs-runner # 查看 runner 日志
make docker-restart     # 重启服务
make docker-shell       # 进入容器 shell
make docker-test        # 容器内跑冒烟测试
make docker-clean       # 清理 Docker 资源
```

### 使用 Docker Compose 命令

```bash
# 启动
docker compose up -d

# 停止
docker compose down

# 查看日志
docker compose logs -f
docker compose logs -f --tail=100

# 重启
docker compose restart

# 进入容器
docker compose exec futures-orderflow bash

# 查看状态
docker compose ps
```

### 使用纯 Docker 命令

```bash
# 构建镜像
docker build -t futures-orderflow .

# 运行容器
docker run -d \
  --name futures-orderflow \
  -p 8741:8741 \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  -e TUSHARE_TOKEN=your_token \
  futures-orderflow

# 查看日志
docker logs -f futures-orderflow

# 停止并删除
docker stop futures-orderflow && docker rm futures-orderflow
```

---

## 入口命令

通过 `command` 或 `docker run` 最后一个参数指定运行模式：

| 命令 | 说明 |
|------|------|
| `live` | 实盘模式（默认） |
| `once` | 单次评估后退出（测试用） |
| `test` | 运行全部测试 |
| `smoke` | 冒烟测试 |
| `shell` | 进入 bash shell |
| `python ...` | 运行任意 Python 命令 |

示例：

```bash
# 冒烟测试
docker run --rm futures-orderflow smoke

# 单次评估
docker run --rm -p 8741:8741 futures-orderflow once

# 进入 shell 调试
docker run --rm -it futures-orderflow shell
```

---

## 健康检查

容器内置健康检查，每 30 秒访问 `/api/health` 端点：

```bash
# 查看健康状态
docker inspect --format='{{json .State.Health}}' futures-orderflow | python -m json.tool

# 或使用 compose
docker compose ps
```

健康状态说明：

| 状态 | 说明 |
|------|------|
| `starting` | 启动中（前 60 秒） |
| `healthy` | 正常运行 |
| `unhealthy` | 连续 3 次健康检查失败 |

---

## 文档站部署

文档站使用独立的 Dockerfile（`Dockerfile.docs`），采用 MkDocs 构建 + Nginx 托管的两段式架构。

### 启用文档站

```bash
# 启动主服务 + 文档站
docker compose --profile docs up -d

# 或使用 make
make docker-up-docs
```

访问 `http://localhost:8742` 查看文档站。

### 单独构建文档站镜像

```bash
docker build -f Dockerfile.docs -t futures-orderflow-docs .
docker run -d -p 8080:80 futures-orderflow-docs
```

---

## 资源限制

默认资源限制（可在 `docker-compose.yml` 中调整）：

| 资源 | 限制 | 预留 |
|------|------|------|
| 内存 | 2 GB | 512 MB |
| CPU | 2 核 | - |

如需调整，修改 `docker-compose.yml` 中的 `deploy.resources` 配置。

---

## 安全加固

Docker 配置包含以下安全措施：

- **非 root 用户**：容器内以 `appuser` 用户运行
- **只读 rootfs**：可配置 `read_only: true`（需调整数据卷）
- **禁用全部 capabilities**：`cap_drop: ["ALL"]`
- **禁止提权**：`no-new-privileges: true`
- **健康检查**：自动检测服务状态

---

## 故障排查

### 容器启动后立即退出

查看日志定位原因：

```bash
docker compose logs futures-orderflow
```

### 健康检查失败

```bash
# 手动测试健康端点
docker compose exec futures-orderflow curl -v http://127.0.0.1:8741/api/health
```

### 数据源连接失败

检查 Tushare/Akshare 是否可用：

```bash
docker compose exec futures-orderflow python -c "import akshare; print(akshare.__version__)"
```

### 配置文件未生效

确认配置文件在 `config/` 目录下，且文件名正确：

```bash
ls -la config/
docker compose exec futures-orderflow ls -la /app/config/
```

---

## 升级镜像

```bash
# 拉取最新代码
git pull

# 重新构建并启动
docker compose build
docker compose up -d

# 清理旧镜像
docker image prune -f
```
