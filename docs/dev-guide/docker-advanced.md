# Docker 高级部署

本文档介绍 Docker 部署的高级主题，包括 CI/CD 自动构建、多架构镜像、
安全扫描、开发环境、生产环境最佳实践等。

## CI/CD 自动构建

项目内置 GitHub Actions 工作流（`.github/workflows/docker.yml`），
自动构建并发布 Docker 镜像到 GitHub Container Registry (GHCR)。

### 触发规则

| 事件 | 行为 |
|------|------|
| push 到 `main` | 构建 + 推送 `latest` 标签 |
| 打 tag `v*` | 构建 + 推送版本标签（`v1.2.3`、`1.2`、`1`） |
| PR 到 `main` | 仅构建验证，不推送 |
| 手动触发 | 可指定是否推送和构建架构 |

### 镜像地址

```
ghcr.io/alonglong5118-org/futures-orderflow:latest
ghcr.io/alonglong5118-org/futures-orderflow:v3.0.0
ghcr.io/alonglong5118-org/futures-orderflow:sha-abc123def
```

### 拉取镜像

```bash
# 登录 GHCR（需要 GitHub Token，有 packages:read 权限）
echo $GITHUB_TOKEN | docker login ghcr.io -u $GITHUB_USERNAME --password-stdin

# 拉取最新版
docker pull ghcr.io/alonglong5118-org/futures-orderflow:latest
```

---

## 多架构支持

CI 自动构建 `linux/amd64` 和 `linux/arm64` 两种架构的镜像。

### 本地多架构构建

```bash
# 安装 QEMU 支持
docker run --privileged --rm tonistiigi/binfmt --install all

# 创建 buildx builder
docker buildx create --name multiarch --use

# 构建多架构镜像
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t futures-orderflow:latest \
  --push \
  .
```

### 支持的架构

| 架构 | 说明 |
|------|------|
| `linux/amd64` | x86_64 服务器 / PC |
| `linux/arm64` | Apple Silicon / ARM 服务器 / 树莓派 4+ |

---

## 镜像安全扫描

CI 工作流集成了 **Trivy** 漏洞扫描，扫描结果自动上传到 GitHub Security 面板。

### 本地扫描

```bash
# 使用 Trivy 扫描本地镜像
docker run --rm aquasec/trivy image \
  --severity CRITICAL,HIGH \
  --ignore-unfixed \
  futures-orderflow:latest

# 输出 JSON 格式
docker run --rm -v /tmp/trivy:/output aquasec/trivy image \
  --format json \
  --output /output/results.json \
  futures-orderflow:latest
```

### 漏洞级别

| 级别 | 说明 | 处理策略 |
|------|------|---------|
| `CRITICAL` | 严重漏洞 | 立即修复 |
| `HIGH` | 高危漏洞 | 尽快修复 |
| `MEDIUM` | 中危漏洞 | 计划修复 |
| `LOW` | 低危漏洞 | 按需修复 |

### SBOM（软件物料清单）

CI 自动生成镜像的 SBOM（SPDX 格式），包含所有 Python 包及其版本信息。
发版时 SBOM 会作为 release asset 上传。

---

## 开发环境

开发环境使用 `docker-compose.dev.yml`，提供代码热挂载、调试端口、详细日志。

### 启动开发环境

```bash
# 使用 make
make docker-dev

# 或手动
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

### 开发环境差异

| 特性 | 生产 | 开发 |
|------|------|------|
| 代码 | 打包进镜像 | 挂载到容器（热更新） |
| 运行模式 | watchdog（自动重启） | direct（崩溃停止） |
| 语音 | 可配置 | 默认关闭 |
| 日志级别 | INFO | DEBUG |
| 资源限制 | 2G / 2 核 | 4G / 4 核 |
| 用户 | appuser（非 root） | root（便于调试） |
| 自动重启 | unless-stopped | no |
| 调试端口 | 无 | 5678（debugpy） |

### Python 远程调试

开发镜像开放 5678 端口用于 debugpy 远程调试：

```python
# 在代码中加入断点
import debugpy
debugpy.listen(("0.0.0.0", 5678))
debugpy.wait_for_client()  # 阻塞直到调试器连接
```

然后在 VS Code 中配置 launch.json：

```json
{
  "name": "Attach to Docker",
  "type": "python",
  "request": "attach",
  "connect": {
    "host": "localhost",
    "port": 5678
  },
  "pathMappings": [
    {
      "localRoot": "${workspaceFolder}",
      "remoteRoot": "/app"
    }
  ]
}
```

---

## 生产环境最佳实践

### 资源限制

根据部署环境调整资源限制：

```yaml
services:
  futures-orderflow:
    deploy:
      resources:
        limits:
          cpus: "2.0"
          memory: 2G
        reservations:
          cpus: "0.5"
          memory: 512M
```

| 场景 | CPU | 内存 |
|------|-----|------|
| 轻量测试（3-5 品种） | 0.5 核 | 512 MB |
| 标准实盘（10-20 品种） | 2 核 | 2 GB |
| 全品种实盘（40+ 品种） | 4 核 | 4 GB |

### 日志管理

生产环境建议配置日志轮转，避免日志文件无限增长：

```bash
# 使用 logrotate
cat /etc/logrotate.d/futures-orderflow
/app/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

或使用 Docker 日志驱动：

```yaml
services:
  futures-orderflow:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
```

### 健康检查与自动恢复

容器内置健康检查，配合 `restart: unless-stopped` 实现自动恢复：

```yaml
healthcheck:
  test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8741/api/health"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 60s
```

### 数据备份

定期备份数据卷：

```bash
# 备份 data 和 config 目录
tar czf "backup_$(date +%Y%m%d).tar.gz" data/ config/

# 备份到远程存储
aws s3 cp backup_*.tar.gz s3://my-bucket/backups/
```

---

## 镜像优化

### 减小镜像体积

当前 Dockerfile 采用多阶段构建，最终镜像仅包含运行时依赖。

| 优化手段 | 效果 |
|---------|------|
| 多阶段构建（builder + runtime） | 去掉编译工具链 |
| Python slim 基础镜像 | 比完整版小 ~50% |
| 清理 .pyc / .pyo | 减小 Python 包体积 |
| `--no-install-recommends` | 减少 apt 依赖 |
| `rm -rf /var/lib/apt/lists/*` | 清理 apt 缓存 |

### 构建缓存优化

Dockerfile 按变更频率分层，确保依赖层缓存命中：

```
Layer 1: 基础系统（apt 依赖）        ← 几乎不变
Layer 2: Python 依赖（requirements）  ← 偶尔变更
Layer 3: 工具模块                    ← 较少变更
Layer 4: 分析模块                    ← 偶尔变更
Layer 5: 核心策略模块                 ← 经常变更
Layer 6: 运行器                      ← 最常变更
```

---

## 常见问题

### 多架构构建失败

确保已启用 QEMU 和 buildx：

```bash
docker run --privileged --rm tonistiigi/binfmt --install all
docker buildx create --name multiarch --use
```

### ARM 镜像运行慢

Apple Silicon 上运行 amd64 镜像会通过 QEMU 模拟，性能较差。
建议直接使用 arm64 架构镜像（CI 会自动构建）。

### 镜像体积太大

检查是否有不必要的文件被打包进镜像：

```bash
# 查看镜像各层大小
docker history futures-orderflow:latest

# 运行时检查容器内大文件
docker exec futures-orderflow du -sh /opt/venv/* | sort -rh | head -10
```

### GHCR 拉取失败

确保已登录并具有 packages:read 权限：

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u $USERNAME --password-stdin
```

---

## 相关文件

- `Dockerfile` — 主镜像 Dockerfile
- `Dockerfile.docs` — 文档站 Dockerfile
- `docker-compose.yml` — 生产环境编排
- `docker-compose.dev.yml` — 开发环境编排
- `.dockerignore` — 构建排除列表
- `docker-entrypoint.sh` — 入口脚本
- `docker/nginx-docs.conf` — 文档站 Nginx 配置
- `.github/workflows/docker.yml` — CI 构建发布工作流
