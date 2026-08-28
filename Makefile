# 四维策略 — Makefile
# ====================
# 常用命令快捷方式
#
# 用法:
#   make test              # 跑全部单元测试
#   make smoke             # 冒烟测试（<3s 快速验证）
#   make unit              # 只跑单元测试
#   make integration       # 只跑集成测试
#   make advanced          # 属性+基准+性能
#   make perf              # 性能基准测试
#   make coverage          # 覆盖率报告
#   make slow              # 列出慢测试（>500ms）
#   make junit             # 生成 JUnit XML 报告
#   make random            # 随机顺序运行（发现测试依赖）
#   make watch             # 监听文件变化自动重跑
#   make flake             # 检测不稳定测试（重跑3次）
#   make list              # 列出所有测试模块
#   make discover          # 自动发现新测试模块

.PHONY: test smoke unit integration advanced perf coverage slow junit random flake list discover watch

# Python 命令（优先 python3）
PYTHON := python3
ifeq (, $(shell which python3 2>/dev/null))
PYTHON := python
endif

# ── 基础 ──────────────────────────────────────────────────────────────

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

all:
	@$(PYTHON) run_tests.py all --py-only

# ── 分析工具 ──────────────────────────────────────────────────────────

coverage:
	@$(PYTHON) run_tests.py --py-only --coverage

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
