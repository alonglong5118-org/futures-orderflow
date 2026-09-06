#!/usr/bin/env python3
"""一次性脚本：为 40 个 group-head 板块注入功能简介 chips（gh-sub → gh-intro）。"""

import re
from pathlib import Path

HTML = Path("/Users/ken/WorkBuddy/futures-orderflow/four_dim_live.html")

INTROS = {
    # ===== 账户交易（group-execution）=====
    "成交记录 · 跟单分析": '<i class="gi-chip"><b>记账</b>开仓/平仓表单录入 · 策略账户标签 · CSV 导入</i><i class="gi-chip"><b>跟单分析</b>跟单率/采纳率/胜率对比 · 成交 vs 信号明细</i>',
    "成交明细 · 备注": '<i class="gi-chip"><b>明细</b>逐笔成交列表 · 存 trade_journal.json</i><i class="gi-chip"><b>备注</b>点「备注」写离场逻辑/复盘要点</i>',
    "模拟盘 · 回测沙盒": '<i class="gi-chip"><b>沙盒</b>100 万虚拟资金 · 手动/按信号模拟开平仓</i><i class="gi-chip"><b>隔离</b>独立核算盈亏 · 不碰真实账户</i>',
    "模拟持仓 · 成交": '<i class="gi-chip"><b>虚拟持仓</b>逐合约持仓实时盯市</i><i class="gi-chip"><b>流水</b>模拟成交流水完整回看</i>',
    # ===== 消息推送（group-chat）=====
    "消息推送 · 信号 / 持仓触价": '<i class="gi-chip"><b>推送</b>信号/触价对话式弹送 · 非阻塞不打扰</i><i class="gi-chip"><b>回看</b>按天归类 · 选历史日期完整回看当日推送</i>',
    # ===== 市场扫描（group-scan）=====
    "信号卡片 · 四维 F→T→C 合成": '<i class="gi-chip"><b>信号</b>四维合成卡片 · 全市场实时扫描</i><i class="gi-chip"><b>联动</b>搜索品种 · 点名跳席位榜 · 持仓跳账户</i><i class="gi-chip"><b>辅助</b>板块热力 + 当日触发历史</i>',
    "席位态度榜 · 龙虎榜": '<i class="gi-chip"><b>龙虎榜</b>按 C_score 排名多空席位态度</i><i class="gi-chip"><b>联动</b>历史交易日可选 · 榜单与卡片互点定位</i>',
    "异动扫描 · 领涨领跌": '<i class="gi-chip"><b>异动</b>全市场涨跌幅领涨/领跌双榜</i><i class="gi-chip"><b>用途</b>快速捕捉情绪极端与板块轮动方向</i>',
    "近期信号（限时有效）": '<i class="gi-chip"><b>限时</b>信号默认 120 分钟有效 · 超时自动灰显</i><i class="gi-chip"><b>提示</b>仅供参考 · 不建议直接跟单</i>',
    # ===== 风控监控（group-risk）=====
    "组合预警规则引擎": '<i class="gi-chip"><b>收口</b>热度/状态机/回撤/熔断/日亏/连亏/集中度统一规则</i><i class="gi-chip"><b>分级</b>分级告警 + 动作建议 · trade_config.json 可覆盖</i>',
    "条件触发提醒": '<i class="gi-chip"><b>B1 到价</b>非持仓品种挂条件 · 弹窗+语音+消息</i><i class="gi-chip"><b>B2 换月</b>主力合约移仓预警</i><i class="gi-chip"><b>B3 跳空</b>隔夜跳空风险预警</i>',
    "分品种 Edge / 波动率 Regime": '<i class="gi-chip"><b>Edge</b>分品种优势衰退监控</i><i class="gi-chip"><b>Regime</b>波动率状态切换预警</i>',
    "报警历史": '<i class="gi-chip"><b>历史</b>触价/移动止损/风险超标全记录</i><i class="gi-chip"><b>筛选</b>按类型过滤 · 一键刷新</i>',
    "盘前作战清单": '<i class="gi-chip"><b>清单</b>动态权益/风险水位/硬熔断状态一站速览</i><i class="gi-chip"><b>价位</b>关注品种关键价位 + 开盘自检项</i>',
    "数据交叉校验 · 质量": '<i class="gi-chip"><b>校验</b>多源行情交叉比对 · 覆盖率与结论</i><i class="gi-chip"><b>健康度</b>数据质量监控 · 异常分布与最差品种</i>',
    "事件日历 · 纪律体检": '<i class="gi-chip"><b>日历</b>宏观数据/事件日历闸门建议</i><i class="gi-chip"><b>体检</b>纪律自动评分 · 违规分布列表</i>',
    "蒙特卡洛 · 回测可视化": '<i class="gi-chip"><b>MC</b>权益曲线 p5/p50/p95 置信区间 · 破产概率</i><i class="gi-chip"><b>回测</b>水下曲线 + 逐笔散点</i>',
    "推送 · 工具箱": '<i class="gi-chip"><b>推送</b>Telegram/Bark/企业微信测试通道</i><i class="gi-chip"><b>工具</b>真重校准/漂移检测/回测HTML · 后台跑不阻塞</i>',
    # ===== 市场洞察（group-account）=====
    "风控闸门 · 回撤水位": '<i class="gi-chip"><b>闸门</b>4 道硬约束 · 日亏达 5% 停机线全市场冻结</i><i class="gi-chip"><b>水位</b>回撤档位阶梯 · 新仓降险系数</i>',
    "市场情绪指数": '<i class="gi-chip"><b>情绪</b>7 因子加权 0–100 · 恐惧贪婪五档</i><i class="gi-chip"><b>联动</b>极端模式硬过滤 · 仓位缩放与 T 阈值自动调节</i>',
    "支撑压力位": '<i class="gi-chip"><b>SR</b>三重验证支撑压力位</i><i class="gi-chip"><b>分区</b>近位 ≤0.8% / 灰色地带 / 远位 ≥1.5%</i>',
    "板块热力图": '<i class="gi-chip"><b>热力</b>板块风险调整强度色阶</i><i class="gi-chip"><b>轮动</b>领涨/筑底/钝化/领跌四象限</i>',
    "全市场扫描": '<i class="gi-chip"><b>扫描</b>全品种并行扫描 · 信号强度排序</i><i class="gi-chip"><b>概览</b>上涨/下跌/中性计数 + 扫描耗时</i>',
    "品种筛选": '<i class="gi-chip"><b>筛选</b>5 维条件加权评分</i><i class="gi-chip"><b>输出</b>通过品种清单 + 通过率</i>',
    "信号瀑布流": '<i class="gi-chip"><b>瀑布</b>实时信号时间线 · 增量刷新</i><i class="gi-chip"><b>记录</b>最新信号时间与累计计数</i>',
    "账户只读同步": '<i class="gi-chip"><b>只读</b>minishare 盯市 · 不接券商 API 不代下单</i><i class="gi-chip"><b>对账</b>动态权益/浮盈/使用率 · 漂移一致性检查</i>',
    # ===== 复盘分析（group-analyze）=====
    "交互式历史回放": '<i class="gi-chip"><b>时间机器</b>逐日回放 · 播放/步进/拖动</i><i class="gi-chip"><b>联动</b>权益/持仓/事件/K线开平仓标记同步复盘</i>',
    "管住手 · 复盘体检": '<i class="gi-chip"><b>体检</b>日/周/月纪律评分环 + 等级</i><i class="gi-chip"><b>清单</b>开仓来源/采纳率/胜率 · 自动检查项</i>',
    "多策略 · 多账户表现": '<i class="gi-chip"><b>拆分</b>按策略或账户分组对比</i><i class="gi-chip"><b>定位</b>笔数/胜率/盈亏/手续费 · 找最赚与最拉胯</i>',
    "自动化日报 · 周报": '<i class="gi-chip"><b>报告</b>一键合成账户/交易/风控/纪律复盘 Markdown</i><i class="gi-chip"><b>导出</b>弹窗复制或下载 .md</i>',
    # ===== 复盘分析（pane-perf）=====
    "参数鲁棒性诊断": '<i class="gi-chip"><b>敏感性</b>滑点/阈值扰动地图</i><i class="gi-chip"><b>诊断</b>识别参数过拟合脆弱区</i>',
    "绩效总览 · 净值曲线": '<i class="gi-chip"><b>KPI</b>总盈亏/胜率/回撤/Sharpe/Calmar 等 11 项</i><i class="gi-chip"><b>曲线</b>净值曲线 + 今日盈亏双向条 · CSV 导出</i>',
    "日盘 / 夜盘时段分解": '<i class="gi-chip"><b>时段</b>按平仓时段拆盈亏/胜率/笔数</i><i class="gi-chip"><b>提示</b>鸡蛋生猪无夜盘 · 其余 21:00–23:00</i>',
    "组合 · 套利 · 压力测试": '<i class="gi-chip"><b>相关</b>持仓相关性矩阵 · 红集中绿分散</i><i class="gi-chip"><b>套利</b>价差 z 偏离监控</i><i class="gi-chip"><b>压力</b>不利冲击后权益与击穿止损数</i>',
    "概率校准 · 维度归因": '<i class="gi-chip"><b>校准</b>置信分层命中率 + Brier · 识别过度自信</i><i class="gi-chip"><b>归因</b>实盘盈亏拆 F/T/C 三维来源</i>',
    "风险度量 VaR / 危机趋同": '<i class="gi-chip"><b>VaR</b>95%/99% VaR/CVaR + 成分贡献</i><i class="gi-chip"><b>危机</b>相关性崩溃放大倍数 · GBM/GARCH 前向情景</i>',
    "板块轮动 · 波动率目标 · 流动性": '<i class="gi-chip"><b>轮动</b>RS 强弱排序 · 四象限动能分类</i><i class="gi-chip"><b>目标</b>波动率目标反推手数建议</i><i class="gi-chip"><b>LaR</b>清仓冲击成本与天数预警</i>',
    # ===== 市场洞察（pane-metrics）=====
    "利润 · 价差 · 比价": '<i class="gi-chip"><b>结构</b>加工/养殖利润 · 跨品种比价</i><i class="gi-chip"><b>信号</b>价差 z 偏离大 = 结构性机会</i>',
    # ===== 工具配置（group-tools）=====
    "我的品种盯盘板": '<i class="gi-chip"><b>关注度</b>信号30+波动20+持仓20+价差15+热度15</i><i class="gi-chip"><b>盯盘</b>0–100 评分排序 · 越高越值得盯</i>',
    "🎛 特性开关管理": '<i class="gi-chip"><b>开关</b>功能特性集中管理 · 运行时热切换</i><i class="gi-chip"><b>日志</b>变更留痕 · 面板即时生效</i>',
}

src = HTML.read_text(encoding="utf-8")
lines = src.split("\n")
done, skipped = [], []
for i, line in enumerate(lines):
    if 'class="group-head' not in line:
        continue
    title = next((t for t in INTROS if f'<span class="gh-title">{t}</span>' in line), None)
    if title is None:
        continue
    if 'gh-flow' in line:
        skipped.append(title)
        continue
    m = re.search(r'class="group-head (gh-\w+)"', line)
    if not m:
        raise SystemExit(f"无法匹配 group-head 类名: 行{i+1}")
    line = line.replace(f'class="group-head {m.group(1)}"', f'class="group-head {m.group(1)} gh-flow"', 1)
    line, n = re.subn(r'<span class="gh-sub">[^<]*</span>',
                      f'<span class="gh-intro">{INTROS[title]}</span>', line, count=1)
    if n != 1:
        raise SystemExit(f"gh-sub 替换失败: {title}（行{i+1}）")
    lines[i] = line
    done.append(title)

HTML.write_text("\n".join(lines), encoding="utf-8")
print(f"注入 {len(done)} 个板块")
for t in done:
    print(f"  ✓ {t}")
if skipped:
    print(f"跳过（已有简介）: {skipped}")
missing = [t for t in INTROS if t not in done and t not in skipped]
if missing:
    print(f"⚠️ 未匹配到: {missing}")
