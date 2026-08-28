# 四维策略 — Makefile
# ====================
# 常用命令快捷方式
#
# 用法:
#   make help              # 查看所有可用命令
#
# ── 测试 ────────────────────────────────────────────────────────────
#   make test              # 跑全部单元测试
#   make smoke             # 冒烟测试（快速验证）
#   make unit              # 只跑单元测试
#   make integration       # 只跑集成测试
#   make advanced          # 属性+基准+性能
#   make perf              # 性能基准测试
#   make all               # 跑全部测试（含属性/基准/性能）
#
# ── 分析工具 ────────────────────────────────────────────────────────
#   make coverage          # 覆盖率报告
#   make slow              # 列出慢测试（>500ms）
#   make junit             # 生成 JUnit XML 报告
#   make random            # 随机顺序运行（发现测试依赖）
#   make flake             # 检测不稳定测试（重跑3次）
#
# ── 代码质量 ────────────────────────────────────────────────────────
#   make lint              # 跑全部 lint（阻塞级 + 建议级）
#   make lint-critical     # 只跑阻塞级 lint
#   make lint-style        # 跑建议级风格 lint
#   make format            # 自动格式化代码
#   make format-check      # 检查格式是否规范（不修改）
#   make typecheck         # Mypy 静态类型检查
#   make quality           # 一次性跑所有质量检查（lint + format + type）
#
# ── 管理 ────────────────────────────────────────────────────────────
#   make list              # 列出所有测试模块
#   make discover          # 自动发现新测试模块
#   make watch             # 监听文件变化自动重跑
#   make hooks             # 安装 Git 钩子
#   make deps              # 安装开发依赖
#   make deps-update       # 更新开发依赖
#   make clean             # 清理缓存和临时文件

.PHONY: help test smoke unit integration advanced perf all \
        coverage slow junit random flake \
        lint lint-critical lint-style format format-check typecheck quality \
        list discover watch hooks deps deps-update clean \
        bench bench-strict bench-update

# Python 命令（优先 python3）
PYTHON := python3
ifeq (, $(shell which python3 2>/dev/null))
PYTHON := python
endif

# ── 帮助 ──────────────────────────────────────────────────────────────

help:
	@echo "╔══════════════════════════════════════════════════════════════╗"
	@echo "║  四维策略 · 开发命令手册                                    ║"
	@echo "╚══════════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "── 测试 ──────────────────────────────────────────────────────"
	@echo "  make test           跑全部单元测试（默认）"
	@echo "  make smoke          冒烟测试（<1s 快速验证）"
	@echo "  make unit           只跑单元测试"
	@echo "  make integration    只跑集成测试"
	@echo "  make advanced       属性测试 + 基准回归 + 性能测试"
	@echo "  make perf           性能基准测试"
	@echo "  make all            跑全部测试（含属性/基准/性能）"
	@echo ""
	@echo "── 分析工具 ──────────────────────────────────────────────────"
	@echo "  make coverage       生成覆盖率 HTML 报告"
	@echo "  make slow           列出慢测试（>500ms）"
	@echo "  make junit          生成 JUnit XML 报告"
	@echo "  make random         随机顺序运行（发现测试依赖）"
	@echo "  make flake          检测不稳定测试（重跑 3 次）"
	@echo ""
	@echo "── 代码质量 ──────────────────────────────────────────────────"
	@echo "  make lint           跑全部 lint（阻塞级 + 建议级）"
	@echo "  make lint-critical  只跑阻塞级 lint（CI 同款）"
	@echo "  make lint-style     跑建议级风格 lint"
	@echo "  make format         自动格式化代码（ruff format）"
	@echo "  make format-check   检查格式是否规范（不修改）"
	@echo "  make typecheck      Mypy 静态类型检查"
	@echo "  make quality        全套质量检查（lint + format + type）"
	@echo ""
	@echo "── 管理 ──────────────────────────────────────────────────────"
	@echo "  make list           列出所有测试模块"
	@echo "  make discover       自动发现新测试模块"
	@echo "  make watch          监听文件变化自动重跑冒烟测试"
	@echo "  make hooks          安装 Git 钩子（pre-commit / pre-push）"
	@echo "  make deps           安装开发依赖"
	@echo "  make deps-update    更新开发依赖到最新"
	@echo "  make clean          清理缓存和临时文件"

# ── 测试 ──────────────────────────────────────────────────────────────

test:
	@$(PYTHON) run_tests.py --py-only

smoke:
	@$(PYTHON) run_tests.py smoke --py-only

unit:
	@$(PYTHON) run_tests.py unit --py-only

integration:
	@$(PYTHON) run_tests.py integration --py-only

advanced:
	@$(PYTHON) run_tests.py advanced --py-only

perf:
	@$(PYTHON) run_tests.py performance --py-only

bench:
	@$(PYTHON) run_tests.py performance --py-only

bench-strict:
	@PERF_CHECK=1 $(PYTHON) run_tests.py performance --py-only

bench-update:
	@PERF_SAVE=1 $(PYTHON) -m tests.test_performance
	@echo "✅ 性能基线已更新: tests/_perf_baseline.json"

all:
	@$(PYTHON) run_tests.py all --py-only

# ── 分析工具 ──────────────────────────────────────────────────────────

coverage:
	@$(PYTHON) run_tests.py --py-only --coverage
	@coverage html -d coverage-html
	@echo ""
	@echo "✅ 覆盖率报告已生成: coverage-html/index.html"

slow:
	@$(PYTHON) run_tests.py --py-only --slow 500

junit:
	@$(PYTHON) run_tests.py --py-only --junit test-results.xml

random:
	@$(PYTHON) run_tests.py --py-only -r -f

flake:
	@echo "检测不稳定测试（重跑 3 次）..."
	@$(PYTHON) run_tests.py --py-only --junit /tmp/flake_run1.xml > /dev/null 2>&1 && \
	 $(PYTHON) run_tests.py --py-only --junit /tmp/flake_run2.xml > /dev/null 2>&1 && \
	 $(PYTHON) run_tests.py --py-only --junit /tmp/flake_run3.xml > /dev/null 2>&1 && \
	 echo "✅ 3 次全部通过，无不稳定测试" || \
	 echo "❌ 有失败，可能存在不稳定测试"

# ── 代码质量 ──────────────────────────────────────────────────────────

lint:
	@echo "=== Ruff 全量 lint 检查 ==="
	@ruff check . --statistics
	@echo ""
	@echo "✅ 全量 lint 检查完成（以上为统计）"

lint-critical:
	@echo "=== Ruff 阻塞级 lint 检查（CI 同款）==="
	@ruff check . \
		--select F401,F601,F701,F821,F823,I001,B023,B904,E722,E712
	@echo "✅ 阻塞级检查通过"

lint-style:
	@echo "=== Ruff 风格 lint 检查（建议级）==="
	@ruff check . --statistics --exit-zero
	@echo ""
	@echo "📊 以上为建议改进项"

format:
	@echo "=== Ruff 自动格式化 ==="
	@ruff format .
	@echo "✅ 格式化完成"

format-check:
	@echo "=== Ruff 格式检查（CI 同款）==="
	@ruff format --check .
	@echo "✅ 格式检查通过"

typecheck:
	@echo "=== Mypy 静态类型检查（建议级）==="
	@-mypy . --ignore-missing-imports 2>/dev/null || \
		(echo "⚠️  Mypy 未安装，运行 'make deps' 安装" && exit 1)
	@echo "✅ 类型检查完成"

quality: lint-critical format-check
	@echo ""
	@echo "══════════════════════════════════════════════════════════════"
	@echo "  ✅  所有质量检查通过"
	@echo "══════════════════════════════════════════════════════════════"

# ── 管理 ──────────────────────────────────────────────────────────────

list:
	@$(PYTHON) run_tests.py --list

discover:
	@$(PYTHON) scripts/discover_tests.py --update

watch:
	@echo "监听 .py 文件变化，自动重跑冒烟测试..."
	@echo "按 Ctrl+C 退出"
	@fswatch -o -e ".*" -i "\\.py$$" . 2>/dev/null | xargs -n1 -I{} \
		$(PYTHON) run_tests.py smoke --py-only -f || \
		(echo "⚠️  需要安装 fswatch: brew install fswatch" && exit 1)

hooks:
	@echo "安装 Git 钩子..."
	@bash scripts/install_hooks.sh
	@echo "✅ Git 钩子安装完成"

deps:
	@echo "安装开发依赖..."
	@$(PYTHON) -m pip install --upgrade pip
	@$(PYTHON) -m pip install -r requirements-dev.txt
	@echo "✅ 依赖安装完成"

deps-update:
	@echo "更新开发依赖到最新..."
	@$(PYTHON) -m pip install --upgrade pip
	@$(PYTHON) -m pip install --upgrade -r requirements-dev.txt
	@echo "✅ 依赖更新完成"

clean:
	@echo "清理缓存和临时文件..."
	@rm -rf __pycache__
	@rm -rf .pytest_cache
	@rm -rf .ruff_cache
	@rm -rf .mypy_cache
	@rm -rf coverage-html
	@rm -rf test-results
	@rm -f .coverage
	@rm -f .coverage.*
	@rm -f test-results.xml
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "✅ 清理完成"

# ── 单模块快捷方式（常用模块） ────────────────────────────────────────

t-kelly:
	@$(PYTHON) run_tests.py kelly_factor --py-only -v

t-risk:
	@$(PYTHON) run_tests.py risk_gate --py-only -v

t-gap:
	@$(PYTHON) run_tests.py gap_stop --py-only -v

t-pipeline:
	@$(PYTHON) run_tests.py integration_pipeline --py-only -v

t-backtest:
	@$(PYTHON) run_tests.py integration_backtest_dq --py-only -v
