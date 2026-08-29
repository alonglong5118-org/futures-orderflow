# 安装配置指南

本指南详细介绍 Futures OrderFlow 的系统要求、多种安装方式、配置说明以及安装验证方法。

---

## 系统要求

### 硬件要求

| 组件 | 最低配置 | 推荐配置 | 说明 |
|---|---|---|---|
| **CPU** | 2 核心 | 4 核心及以上 | 回测与遗传算法优化会充分利用多核 |
| **内存** | 4 GB | 8 GB+ | 大数据量回测需要更多内存 |
| **磁盘** | 1 GB 可用空间 | 10 GB+ | 缓存行情数据和回测结果 |
| **网络** | 稳定宽带 | 光纤 | 实盘交易对网络延迟敏感 |

### 操作系统

项目支持全平台运行，以下为经过测试的系统版本：

- **Linux**: Ubuntu 20.04+, CentOS 8+, Debian 11+
- **macOS**: 11 (Big Sur) 及以上，支持 Intel 和 Apple Silicon
- **Windows**: 10 及以上，推荐使用 WSL2

---

## Python 版本

项目要求 **Python 3.10+**，推荐使用 **Python 3.11** 或 **3.12** 以获得最佳性能。

### 检查 Python 版本

```bash
python3 --version
```

### 多版本管理

如果你需要管理多个 Python 版本，推荐使用以下工具：

=== "pyenv"

    ```bash
    # 安装 pyenv（macOS/Linux）
    curl https://pyenv.run | bash

    # 安装指定 Python 版本
    pyenv install 3.11.9

    # 在项目目录设置本地版本
    cd futures-orderflow
    pyenv local 3.11.9
    ```

=== "conda"

    ```bash
    # 创建指定版本的环境
    conda create -n futures-orderflow python=3.11
    conda activate futures-orderflow
    ```

=== "Windows"

    从 [Python 官网](https://www.python.org/downloads/) 下载安装包，安装时勾选 "Add Python to PATH"。

---

## 依赖安装

### 方式一：pip 安装（推荐）

这是最常用的安装方式，适合大多数用户。

#### 1. 获取源码

```bash
git clone https://github.com/alonglong5118-org/futures-orderflow.git
cd futures-orderflow
```

#### 2. 创建虚拟环境

```bash
python3 -m venv .venv
```

=== "macOS / Linux"

    ```bash
    source .venv/bin/activate
    ```

=== "Windows (PowerShell)"

    ```powershell
    .venv\Scripts\Activate.ps1
    ```

=== "Windows (CMD)"

    ```cmd
    .venv\Scripts\activate.bat
    ```

#### 3. 安装依赖

```bash
# 核心运行时依赖（必需）
pip install -r requirements.txt

# 开发依赖（测试、lint、类型检查等）
pip install -r requirements-dev.txt
```

!!! tip "使用国内镜像源"
    如果你在中国大陆，可以使用清华镜像源加速下载：
    ```bash
    pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    ```

### 方式二：从源码安装

如果你需要修改代码或贡献开发，可以采用源码安装方式。

```bash
# 克隆你的 fork
git clone https://github.com/your-username/futures-orderflow.git
cd futures-orderflow

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装开发依赖
make deps
# 或手动安装: pip install -r requirements-dev.txt

# 安装 Git 钩子（可选但推荐）
make hooks
```

!!! info "Git 钩子说明"
    - `pre-commit`: 提交前运行单元测试和格式检查
    - `pre-push`: 推送前运行回归测试和质量检查

### 方式三：虚拟环境方案对比

| 方案 | 优点 | 缺点 | 适用场景 |
|---|---|---|---|
| **venv** | Python 内置，无需额外安装 | 功能基础 | 大多数用户 |
| **conda** | 科学计算库支持好，跨平台 | 体积较大 | 数据科学场景 |
| **poetry** | 依赖锁定，发布方便 | 需要额外学习 | 包发布场景 |
| **docker** | 环境完全一致 | 资源占用大 | 部署与 CI |

---

## 依赖包说明

### 核心依赖（requirements.txt）

| 包名 | 版本要求 | 用途 |
|---|---|---|
| **numpy** | >= 1.21 | 数值计算基础库 |
| **pandas** | >= 1.3 | 数据分析与处理 |
| **scipy** | >= 1.7 | 科学计算与统计 |
| **hmmlearn** | >= 0.2.7 | 隐马尔可夫模型（市场状态识别） |
| **plotly** | >= 5.0 | 交互式可视化图表 |
| **tqdm** | >= 4.60 | 进度条显示 |

### 开发依赖（requirements-dev.txt）

| 包名 | 版本要求 | 用途 |
|---|---|---|
| **coverage** | >= 7.0 | 代码覆盖率统计 |
| **hypothesis** | >= 6.0 | 属性测试（Property-based testing） |
| **deap** | >= 1.3 | 遗传算法框架 |
| **ruff** | >= 0.6 | 代码 Lint 与格式化 |
| **mypy** | >= 1.11 | 静态类型检查 |
| **bandit** | >= 1.7 | 代码安全扫描 |
| **pip-audit** | >= 2.7 | 依赖漏洞扫描 |

### 可选依赖

以下依赖按需安装，不包含在默认 requirements 中：

| 包名 | 用途 | 安装命令 |
|---|---|---|
| **akshare** | AkShare 财经数据源 | `pip install akshare` |
| **tqsdk** | TqSdk 期货交易接口 | `pip install tqsdk` |
| **deap** | 遗传算法优化框架 | `pip install deap` |

!!! note "关于 CI 环境"
    CI 环境中默认不安装可选依赖（akshare、tqsdk），以加快测试速度和减少依赖问题。遗传算法相关的测试依赖 deap，已包含在 `requirements-dev.txt` 中。

---

## 配置说明

### 环境变量配置

项目支持通过环境变量进行配置。你可以在项目根目录创建 `.env` 文件：

```bash
# 复制示例文件
cp .env.example .env

# 编辑配置
vim .env
```

### 主要配置项

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `TQSDK_ACCOUNT` | TqSdk 账户名 | - |
| `TQSDK_PASSWORD` | TqSdk 密码 | - |
| `TUSHARE_TOKEN` | Tushare API Token | - |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `DATA_CACHE_DIR` | 数据缓存目录 | `./cache` |

!!! warning "敏感信息"
    不要将包含密码和 API Token 的 `.env` 文件提交到版本控制系统。项目的 `.gitignore` 已默认排除该文件。

---

## 验证安装

完成安装后，通过以下步骤验证安装是否成功。

### 第一步：检查 Python 环境

```bash
python3 -c "import sys; print(f'Python {sys.version}')"
```

### 第二步：验证核心依赖

```bash
python3 -c "import numpy; import pandas; import scipy; import hmmlearn; print('核心依赖导入成功')"
```

预期输出：
```
核心依赖导入成功
```

### 第三步：运行冒烟测试

```bash
make smoke
```

或使用 Python 脚本：

```bash
python run_tests.py smoke --py-only
```

!!! success "通过标准"
    - 所有测试用例通过（OK）
    - 没有 ImportError 或 ModuleNotFoundError
    - 测试总时长通常在 1 秒以内

### 第四步：运行完整单元测试（可选）

```bash
make test
```

如果全部 272+ 测试用例通过，说明安装完全正确。

---

## 常见安装问题

### 问题：`ModuleNotFoundError: No module named 'xxx'`

**原因：** 依赖未正确安装，或虚拟环境未激活。

**解决：**

```bash
# 确认虚拟环境已激活
which python3

# 重新安装依赖
pip install -r requirements.txt
```

### 问题：安装 numpy/scipy 时编译失败

**原因：** 缺少编译工具链或系统库。

**解决：**

=== "macOS"

    ```bash
    xcode-select --install
    pip install --upgrade pip setuptools wheel
    ```

=== "Ubuntu/Debian"

    ```bash
    sudo apt-get install build-essential python3-dev
    pip install --upgrade pip setuptools wheel
    ```

=== "Windows"

    推荐使用 Conda 或直接下载预编译的 wheel 包：
    ```bash
    pip install numpy scipy --only-binary=:all:
    ```

### 问题：hmmlearn 安装失败

**原因：** hmmlearn 依赖 scikit-learn，可能存在版本兼容问题。

**解决：**

```bash
# 先安装 numpy 和 scipy
pip install numpy scipy

# 再安装 hmmlearn
pip install hmmlearn
```

### 问题：pip 安装速度慢

**解决：** 使用国内镜像源

```bash
# 清华镜像源
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 阿里镜像源
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 问题：`make` 命令不可用（Windows）

**解决：** 直接使用 Python 脚本运行

```bash
# 运行测试
python run_tests.py unit --py-only

# 代码检查
python -m ruff check .

# 格式化
python -m ruff format .
```

---

## 卸载

如需完全卸载，删除项目目录和虚拟环境即可：

```bash
# 取消虚拟环境激活
deactivate

# 删除项目目录（含虚拟环境）
rm -rf futures-orderflow
```

如果使用 conda：

```bash
conda deactivate
conda remove -n futures-orderflow --all
```

---

## 下一步

- 阅读 [快速上手指南](quickstart.md)，运行第一个策略示例
- 浏览 [架构概览](../architecture/overview.md)，了解系统整体设计
- 查看 [开发指南](../dev-guide/contributing.md)，参与项目贡献
