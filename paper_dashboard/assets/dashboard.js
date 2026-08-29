// ============================================================
// Paper Trading Dashboard - Main Logic
// ============================================================

(function() {
  'use strict';

  // --- Configuration ---
  var API_URL = '/api/paper-trading';
  var REFRESH_INTERVAL = 3000; // 默认 3 秒刷新
  var REFRESH_PAUSED_ON_HIDDEN = true; // 页面隐藏时暂停刷新
  var DEMO_RETRY_INTERVAL = 30000; // Demo 模式下每 30 秒尝试重连真实 API

  // 从 URL 参数读取刷新间隔配置（?refresh=5000）
  (function() {
    var params = new URLSearchParams(window.location.search);
    var customInterval = parseInt(params.get('refresh'));
    if (customInterval && customInterval >= 1000) {
      REFRESH_INTERVAL = customInterval;
    }
  })();

  // --- State ---
  var currentData = null;
  var lastDataHash = null; // 用于数据变化检测
  var equityChart = null;
  var refreshTimer = null;
  var isRefreshing = false;
  var consecutiveErrors = 0;
  var isEditingConfig = false;
  var editingConfigCount = 0; // 计数器方式，支持多输入框切换不丢失状态
  var isPageVisible = true;
  var isDemoMode = false;
  var demoRetryTimer = null;
  var lastErrorMessage = '';

  // --- CSS Variables ---
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim() || '#3b82f6';
  var accent2 = style.getPropertyValue('--accent2').trim() || '#8b5cf6';
  var success = style.getPropertyValue('--success').trim() || '#10b981';
  var danger = style.getPropertyValue('--danger').trim() || '#ef4444';
  var warning = style.getPropertyValue('--warning').trim() || '#f59e0b';
  var ink = style.getPropertyValue('--ink').trim() || '#e6edf7';
  var muted = style.getPropertyValue('--muted').trim() || '#8896b0';
  var rule = style.getPropertyValue('--rule').trim() || '#2a3a5c';
  var bg2 = style.getPropertyValue('--bg2').trim() || '#131a2e';
  var bg3 = style.getPropertyValue('--bg3').trim() || '#1e2a45';

  // --- Utility: 简单的数据哈希，用于检测变化 ---
  function dataHash(obj) {
    try {
      return JSON.stringify(obj);
    } catch (e) {
      return String(Math.random());
    }
  }

  // --- Data Adapter: 统一 API 数据格式 ---
  function normalizeData(raw) {
    if (!raw) return raw;

    var stats = raw.stats || {};
    var positions = raw.positions || [];
    var recentTrades = raw.recent_trades || [];

    // positions: array → dict (symbol 作为 key)
    var posDict = {};
    positions.forEach(function(p) {
      var dir = p.direction_en || (p.direction === '多' ? 'long' : 'short');
      posDict[p.symbol] = {
        symbol: p.symbol,
        name: p.name,
        direction: dir,
        lots: p.lots || p.remaining_lots || 0,
        entry_price: p.entry_price,
        current_price: p.current_price || p.entry_price,
        stop_loss: p.stop_price,
        take_profit: p.target_price,
        unrealized_pnl: p.mtm != null ? p.mtm : 0,
        r_pnl: p.mtm_R != null ? p.mtm_R : 0,
        opened_at: p.open_ts,
        trailing_active: p.trailing_active,
        trailing_stop: p.trailing_stop,
        t1_filled: p.t1_filled,
        remaining_lots: p.remaining_lots != null ? p.remaining_lots : p.lots,
        source: p.source
      };
    });

    // trade_history: 从 recent_trades 转换
    var tradeHistory = recentTrades.map(function(t) {
      var dir = t.direction === '多' ? 'long' : 'short';
      return {
        timestamp: t.time ? new Date(t.time).getTime() / 1000 : 0,
        symbol: t.symbol,
        name: t.name,
        direction: dir,
        type: t.type, // open / close
        lots: t.lots,
        entry_price: t.entry_price || t.price,
        exit_price: t.exit_price || null,
        pnl: t.pnl != null ? t.pnl : null,
        r_pnl: t.pnl_R != null ? t.pnl_R : null,
        reason: t.reason || (t.source === 'auto_signal' ? '信号建仓' : (t.source || '--'))
      };
    });

    // 权益曲线：如果引擎没有提供，用历史交易记录 + 当前权益生成
    var equityCurve = raw.equity_curve || [];
    if (equityCurve.length === 0 && raw.equity && raw.init_cash) {
      // 从已平仓交易反推权益曲线（按时间排序）
      var closedTrades = recentTrades.filter(function(t) { return t.type === 'close' && t.pnl != null; })
        .sort(function(a, b) { return new Date(a.time) - new Date(b.time); });

      var eq = raw.init_cash;
      var curve = [];

      // 起点：用第一笔交易的时间往前推一点作为起点（而不是 Date.now()-86400，避免每次刷新起点都变）
      if (closedTrades.length > 0) {
        var firstTs = new Date(closedTrades[0].time).getTime() / 1000;
        curve.push([firstTs - 3600, raw.init_cash]); // 起点比第一笔交易早 1 小时
      } else {
        // 没有交易记录时，用当前时间往前 1 小时作为起点（相对稳定）
        curve.push([Math.floor(Date.now() / 1000 / 3600) * 3600 - 3600, raw.init_cash]);
      }

      closedTrades.forEach(function(t) {
        eq += t.pnl;
        var ts = new Date(t.time).getTime() / 1000;
        curve.push([ts, Math.round(eq * 100) / 100]);
      });

      // 当前权益点：时间戳对齐到刷新间隔，避免每帧微秒级漂移导致曲线抖动
      var alignedNow = Math.floor(Date.now() / 1000 / (REFRESH_INTERVAL / 1000)) * (REFRESH_INTERVAL / 1000);
      curve.push([alignedNow, raw.equity]);
      equityCurve = curve;
    }

    // 计算 stats 缺失字段的兜底值
    var totalTrades = stats.total_trades || raw.total_trades || 0;
    var totalPnl = stats.total_pnl != null ? stats.total_pnl : (raw.realized_pnl || 0);

    // 配置：优先用 API 返回的真实配置，没有才用默认值
    var rawConfig = raw.config || {};
    var config = {
      max_positions: rawConfig.max_positions != null ? rawConfig.max_positions : 8,
      max_lots_per_trade: rawConfig.max_lots_per_trade != null ? rawConfig.max_lots_per_trade : 5,
      default_lots: rawConfig.default_lots != null ? rawConfig.default_lots : 1,
      cooldown_minutes: rawConfig.cooldown_minutes != null ? rawConfig.cooldown_minutes : 30,
      enable_trailing: rawConfig.enable_trailing != null ? rawConfig.enable_trailing : true,
      trailing_lock_r: rawConfig.trailing_lock_r != null ? rawConfig.trailing_lock_r : 0.5
    };

    return {
      enabled: raw.enabled !== false,
      config: config,
      positions: posDict,
      stats: {
        equity: raw.equity || 0,
        initial_equity: raw.init_cash || 1000000,
        total_pnl: totalPnl,
        total_trades: totalTrades,
        wins: stats.wins || 0,
        losses: stats.losses || 0,
        win_rate: stats.win_rate || 0,
        profit_factor: stats.profit_factor || 0,
        max_drawdown: stats.max_drawdown || 0,
        expR: stats.avg_R || 0,
        avg_hold_time: (stats.avg_holding_hours || 0) * 3600,
        max_win_streak: 0,
        max_loss_streak: 0,
        avg_win: stats.avg_win || 0,
        avg_loss: stats.avg_loss || 0,
        sharpe: 0,
        calmar: 0
      },
      equity_curve: equityCurve,
      trade_history: tradeHistory
    };
  }

  // --- Utility Functions ---
  function fmtNum(n, digits) {
    if (n == null || isNaN(n)) return '--';
    digits = digits != null ? digits : 2;
    return Number(n).toLocaleString('zh-CN', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
  }

  function fmtPnl(n) {
    if (n == null || isNaN(n)) return '--';
    var sign = n > 0 ? '+' : '';
    return sign + fmtNum(n, 2);
  }

  function fmtTime(ts) {
    if (!ts) return '--';
    var d = new Date(ts * 1000);
    return d.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    });
  }

  function fmtTimeOnly(ts) {
    if (!ts) return '--';
    var d = new Date(ts * 1000);
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  // --- 保存/恢复滚动位置 ---
  function saveScrollPosition(containerId) {
    var el = document.getElementById(containerId);
    return el ? el.scrollTop : 0;
  }

  function restoreScrollPosition(containerId, scrollTop) {
    var el = document.getElementById(containerId);
    if (el) el.scrollTop = scrollTop;
  }

  // --- API Functions ---
  function apiGet() {
    return fetch(API_URL, { cache: 'no-store' })
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(data) { return normalizeData(data); })
      .catch(function(e) {
        console.error('[Dashboard] API 请求失败:', e);
        lastErrorMessage = e.message;
        throw e;
      });
  }

  function apiPost(body) {
    return fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    })
      .then(function(r) { return r.json(); })
      .catch(function(e) {
        console.error('[Dashboard] API POST 失败:', e);
        return { ok: false, error: e.message };
      });
  }

  // --- Render: Metric Cards ---
  function renderMetrics(data) {
    var grid = document.getElementById('metrics-grid');
    var stats = data.stats || {};
    var positions = data.positions || {};
    var posCount = Object.keys(positions).length;

    var cards = [
      {
        label: '账户权益',
        value: '¥' + fmtNum(stats.equity || 0, 2),
        sub: '初始资金 ¥' + fmtNum(stats.initial_equity || 1000000, 0),
        cls: stats.equity >= (stats.initial_equity || 1000000) ? 'success' : 'danger',
        valCls: stats.equity >= (stats.initial_equity || 1000000) ? 'positive' : 'negative'
      },
      {
        label: '总盈亏',
        value: fmtPnl(stats.total_pnl || 0),
        sub: '收益率 ' + fmtNum((stats.total_pnl || 0) / (stats.initial_equity || 1000000) * 100, 2) + '%',
        cls: (stats.total_pnl || 0) >= 0 ? 'success' : 'danger',
        valCls: (stats.total_pnl || 0) >= 0 ? 'positive' : 'negative'
      },
      {
        label: '胜率',
        value: fmtNum((stats.win_rate || 0) * 100, 1) + '%',
        sub: '盈亏比 ' + fmtNum(stats.profit_factor || 0, 2),
        cls: (stats.win_rate || 0) >= 0.5 ? 'success' : 'warning'
      },
      {
        label: '总交易次数',
        value: fmtNum(stats.total_trades || 0, 0),
        sub: '胜 ' + (stats.wins || 0) + ' / 负 ' + (stats.losses || 0),
        cls: 'accent'
      },
      {
        label: '当前持仓',
        value: posCount + ' 个',
        sub: '最大持仓 ' + (data.config ? data.config.max_positions : '--') + ' 个',
        cls: posCount > 0 ? 'warning' : ''
      },
      {
        label: '最大回撤',
        value: fmtNum(Math.abs(stats.max_drawdown || 0) / (stats.initial_equity || 1000000) * 100, 2) + '%',
        sub: '金额 ¥' + fmtNum(Math.abs(stats.max_drawdown || 0), 0),
        cls: 'danger'
      }
    ];

    grid.innerHTML = cards.map(function(c) {
      return '<div class="metric-card ' + (c.cls || '') + '">' +
        '<div class="label">' + c.label + '</div>' +
        '<div class="value ' + (c.valCls || '') + '">' + c.value + '</div>' +
        '<div class="sub">' + c.sub + '</div>' +
      '</div>';
    }).join('');
  }

  // --- Render: Equity Chart ---
  var lastChartRange = 'all';
  var lastEquityCurveHash = '';

  function renderEquityChart(data) {
    var container = document.getElementById('chart-equity');
    if (!equityChart) {
      equityChart = echarts.init(container, null, { renderer: 'svg' });
      window.addEventListener('resize', function() { equityChart.resize(); });
    }

    var equityCurve = data.equity_curve || [];
    var range = document.getElementById('chart-range').value;

    // 数据没变且 range 没变，跳过渲染（优化性能）
    var curveHash = equityCurve.map(function(p) { return p[0] + '_' + p[1]; }).join('|');
    if (curveHash === lastEquityCurveHash && range === lastChartRange) {
      return;
    }
    lastEquityCurveHash = curveHash;
    lastChartRange = range;

    // Filter by range - 用曲线最后一个点作为基准（而不是 Date.now()，避免边界漂移）
    var filtered = equityCurve;
    if (range !== 'all' && equityCurve.length > 0) {
      var days = parseInt(range);
      var lastTs = equityCurve[equityCurve.length - 1][0];
      var cutoff = lastTs - days * 86400;
      filtered = equityCurve.filter(function(p) { return p[0] >= cutoff; });
    }

    var dates = filtered.map(function(p) {
      var d = new Date(p[0] * 1000);
      return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
    });
    var values = filtered.map(function(p) { return p[1]; });

    // Calculate drawdown
    var peak = -Infinity;
    var drawdowns = filtered.map(function(p) {
      peak = Math.max(peak, p[1]);
      return peak - p[1];
    });

    var option = {
      tooltip: {
        trigger: 'axis',
        appendToBody: true,
        backgroundColor: bg3,
        borderColor: rule,
        textStyle: { color: ink, fontSize: 12 },
        formatter: function(params) {
          var eq = params[0];
          var dd = params[1];
          return '<div style="font-family: JetBrainsMono, monospace;">' +
            eq.axisValueLabel + '<br>' +
            '权益: <b style="color:' + success + '">¥' + fmtNum(eq.value, 0) + '</b><br>' +
            '回撤: <b style="color:' + danger + '">¥' + fmtNum(dd.value, 0) + '</b>' +
          '</div>';
        }
      },
      grid: {
        left: '3%',
        right: '3%',
        top: '10%',
        bottom: '12%',
        containLabel: true
      },
      legend: {
        data: ['权益曲线', '回撤'],
        textStyle: { color: muted, fontSize: 11 },
        top: 5,
        right: 10
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: rule } },
        axisLabel: { color: muted, fontSize: 10 }
      },
      yAxis: [
        {
          type: 'value',
          axisLine: { show: false },
          splitLine: { lineStyle: { color: rule, type: 'dashed' } },
          axisLabel: {
            color: muted,
            fontSize: 10,
            formatter: function(v) { return (v / 10000).toFixed(0) + '万'; }
          }
        },
        {
          type: 'value',
          axisLine: { show: false },
          splitLine: { show: false },
          axisLabel: {
            color: muted,
            fontSize: 10,
            formatter: function(v) { return (v / 10000).toFixed(0) + '万'; }
          }
        }
      ],
      series: [
        {
          name: '权益曲线',
          type: 'line',
          data: values,
          smooth: true,
          symbol: 'none',
          lineStyle: { color: accent, width: 2 },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: accent + '33' },
                { offset: 1, color: accent + '05' }
              ]
            }
          },
          yAxisIndex: 0
        },
        {
          name: '回撤',
          type: 'line',
          data: drawdowns,
          smooth: true,
          symbol: 'none',
          lineStyle: { color: danger, width: 1, type: 'dashed' },
          areaStyle: {
            color: {
              type: 'linear',
              x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [
                { offset: 0, color: danger + '22' },
                { offset: 1, color: danger + '05' }
              ]
            }
          },
          yAxisIndex: 1
        }
      ]
    };

    // 增量更新（notMerge=false），避免图表闪烁
    equityChart.setOption(option, false);
  }

  // --- Render: Stats Panel ---
  function renderStats(data) {
    var panel = document.getElementById('stats-panel');
    var stats = data.stats || {};
    var items = [
      { label: '预期收益 (expR)', value: fmtNum(stats.expR || 0, 3) },
      { label: '平均持仓时间', value: stats.avg_hold_time ? (stats.avg_hold_time / 60).toFixed(1) + ' 分钟' : '--' },
      { label: '最大连续盈利', value: (stats.max_win_streak || 0) + ' 笔' },
      { label: '最大连续亏损', value: (stats.max_loss_streak || 0) + ' 笔' },
      { label: '平均盈利', value: '¥' + fmtNum(stats.avg_win || 0, 0) },
      { label: '平均亏损', value: '¥' + fmtNum(stats.avg_loss || 0, 0) },
      { label: '夏普比率', value: fmtNum(stats.sharpe || 0, 2) },
      { label: '卡玛比率', value: fmtNum(stats.calmar || 0, 2) }
    ];

    panel.innerHTML = items.map(function(item) {
      return '<div class="config-item">' +
        '<label>' + item.label + '</label>' +
        '<span style="font-family: JetBrainsMono, monospace; font-size: 0.85rem; font-weight: 600;">' + item.value + '</span>' +
      '</div>';
    }).join('');
  }

  // --- Render: Positions Table ---
  function renderPositions(data) {
    // 保存滚动位置
    var scrollTop = saveScrollPosition('positions-wrap');

    var tbody = document.getElementById('positions-tbody');
    var positions = data.positions || {};
    var symbols = Object.keys(positions);

    document.getElementById('position-count').textContent = symbols.length + ' 个持仓';

    if (symbols.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10"><div class="empty-state"><div class="icon">📭</div><p>暂无持仓</p></div></td></tr>';
      restoreScrollPosition('positions-wrap', scrollTop);
      return;
    }

    tbody.innerHTML = symbols.map(function(sym) {
      var pos = positions[sym];
      var dir = pos.direction || 'long';
      var dirLabel = dir === 'long' ? '多' : '空';
      var dirCls = dir === 'long' ? 'dir-long' : 'dir-short';
      var entry = pos.entry_price || 0;
      var cur = pos.current_price || entry;
      var sl = pos.stop_loss || 0;
      var tp = pos.take_profit || 0;
      var lots = pos.lots || 0;
      var pnl = pos.unrealized_pnl || 0;
      var rPnl = pos.r_pnl || 0;

      // Progress bar: how far between SL and TP
      var progressPct = 50;
      if (sl && tp && entry) {
        var totalRange = Math.abs(tp - sl);
        var curOffset = Math.abs(cur - sl);
        if (totalRange > 0) {
          progressPct = (curOffset / totalRange) * 100;
          progressPct = Math.max(0, Math.min(100, progressPct));
        }
      }

      var pnlCls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
      var rCls = rPnl >= 0 ? 'pnl-pos' : 'pnl-neg';

      return '<tr>' +
        '<td><span class="sym">' + sym + '</span></td>' +
        '<td><span class="' + dirCls + '">' + dirLabel + '</span></td>' +
        '<td class="mono">' + lots + '</td>' +
        '<td class="mono">' + fmtNum(entry, 2) + '</td>' +
        '<td class="mono"><b>' + fmtNum(cur, 2) + '</b></td>' +
        '<td class="mono" style="color:' + danger + '">' + fmtNum(sl, 2) + '</td>' +
        '<td class="mono" style="color:' + success + '">' + fmtNum(tp, 2) + '</td>' +
        '<td class="' + pnlCls + ' mono">' + fmtPnl(pnl) + '</td>' +
        '<td class="' + rCls + ' mono">' + fmtPnl(rPnl) + 'R</td>' +
        '<td>' +
          '<div style="width:60px;height:6px;background:' + bg3 + ';border-radius:3px;overflow:hidden;">' +
            '<div style="height:100%;width:' + progressPct.toFixed(0) + '%;background:' + (pnl >= 0 ? success : danger) + ';border-radius:3px;"></div>' +
          '</div>' +
        '</td>' +
      '</tr>';
    }).join('');

    // 恢复滚动位置
    restoreScrollPosition('positions-wrap', scrollTop);
  }

  // --- Render: Trades Table ---
  function renderTrades(data) {
    // 保存滚动位置（仅在已有数据时，避免首次渲染就记录）
    var scrollTop = saveScrollPosition('trades-wrap');

    var tbody = document.getElementById('trades-tbody');
    var trades = data.trade_history || [];
    var filter = document.getElementById('trade-filter').value;

    var filtered = trades;
    if (filter === 'close') {
      filtered = trades.filter(function(t) { return t.type === 'close'; });
    } else if (filter === 'open') {
      filtered = trades.filter(function(t) { return t.type === 'open'; });
    }

    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="10"><div class="empty-state"><div class="icon">📝</div><p>暂无交易记录</p></div></td></tr>';
      restoreScrollPosition('trades-wrap', scrollTop);
      return;
    }

    tbody.innerHTML = filtered.slice(0, 100).map(function(t) {
      var dirLabel = t.direction === 'long' ? '多' : '空';
      var dirCls = t.direction === 'long' ? 'dir-long' : 'dir-short';
      var typeLabel = t.type === 'open' ? '开仓' : '平仓';
      var pnl = t.pnl != null ? t.pnl : 0;
      var rPnl = t.r_pnl != null ? t.r_pnl : 0;
      var pnlCls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
      var reason = t.reason || '--';
      var reasonCls = pnl >= 0 ? 'reason-win' : 'reason-loss';

      return '<tr>' +
        '<td class="time mono">' + fmtTime(t.timestamp) + '</td>' +
        '<td><span class="sym" style="font-size: 0.8rem;">' + (t.symbol || '--') + '</span></td>' +
        '<td><span class="' + dirCls + '">' + dirLabel + '</span></td>' +
        '<td>' + typeLabel + '</td>' +
        '<td class="mono">' + (t.lots || 0) + '</td>' +
        '<td class="mono">' + fmtNum(t.entry_price || 0, 2) + '</td>' +
        '<td class="mono">' + (t.exit_price ? fmtNum(t.exit_price, 2) : '--') + '</td>' +
        '<td class="' + pnlCls + ' mono">' + (t.type === 'close' ? fmtPnl(pnl) : '--') + '</td>' +
        '<td class="' + pnlCls + ' mono">' + (t.type === 'close' ? fmtPnl(rPnl) + 'R' : '--') + '</td>' +
        '<td><span class="reason-tag ' + reasonCls + '">' + reason + '</span></td>' +
      '</tr>';
    }).join('');

    // 恢复滚动位置
    restoreScrollPosition('trades-wrap', scrollTop);
  }

  // --- Render: Config Panel ---
  function renderConfig(data) {
    // 如果用户正在编辑配置，不刷新输入框，防止覆盖用户输入
    if (isEditingConfig || editingConfigCount > 0) return;

    var cfg = data.config || {};
    var el = function(id) { return document.getElementById(id); };

    if (cfg.max_positions != null) el('cfg-max-pos').value = cfg.max_positions;
    if (cfg.max_lots_per_trade != null) el('cfg-max-lots').value = cfg.max_lots_per_trade;
    if (cfg.default_lots != null) el('cfg-default-lots').value = cfg.default_lots;
    if (cfg.cooldown_minutes != null) el('cfg-cooldown').value = cfg.cooldown_minutes;
    if (cfg.enable_trailing != null) {
      var t = el('cfg-trailing');
      if (cfg.enable_trailing) t.classList.add('active'); else t.classList.remove('active');
    }
    if (cfg.trailing_lock_r != null) el('cfg-trailing-lock').value = cfg.trailing_lock_r;
  }

  // --- Toggle & Status ---
  function renderToggle(enabled) {
    var toggle = document.getElementById('auto-toggle');
    var dot = document.getElementById('status-dot');
    if (enabled) {
      toggle.classList.add('active');
    } else {
      toggle.classList.remove('active');
    }
  }

  function updateConnectionStatus(state, message) {
    // state: 'connected' | 'error' | 'loading'
    var dot = document.getElementById('status-dot');
    var timeLabel = document.getElementById('updated-time');

    if (state === 'connected') {
      dot.style.background = success;
      dot.style.animation = 'pulse 2s infinite';
      dot.title = '已连接';
      if (timeLabel && !message) {
        // 正常更新时间
      }
    } else if (state === 'error') {
      dot.style.background = danger;
      dot.style.animation = 'none';
      dot.title = '连接失败: ' + (message || lastErrorMessage || '未知错误');
      if (timeLabel) {
        timeLabel.textContent = '连接中断';
        timeLabel.title = '错误: ' + (message || lastErrorMessage || '未知错误');
        timeLabel.style.color = danger;
      }
    } else if (state === 'loading') {
      dot.style.background = warning;
      dot.style.animation = 'pulse 1s infinite';
      dot.title = '连接中...';
    }

    // 如果恢复连接，重置时间标签颜色
    if (state === 'connected' && timeLabel) {
      timeLabel.style.color = '';
      timeLabel.title = '';
    }
  }

  // --- Main Render ---
  function renderAll(data) {
    if (!data) return;

    // 数据变化检测：数据没变就跳过渲染（节省性能）
    var newHash = dataHash(data);
    if (newHash === lastDataHash && lastDataHash !== null) {
      // 只更新时间戳
      document.getElementById('updated-time').textContent = '更新于 ' + fmtTimeOnly(Date.now() / 1000);
      return;
    }
    lastDataHash = newHash;
    currentData = data;

    renderMetrics(data);
    renderEquityChart(data);
    renderStats(data);
    renderPositions(data);
    renderTrades(data);
    renderConfig(data);
    renderToggle(data.enabled);

    document.getElementById('updated-time').textContent = '更新于 ' + fmtTimeOnly(Date.now() / 1000);
  }

  // --- Smart Refresh: 完成后再调度，避免请求堆积 ---
  function refresh() {
    // 页面隐藏时不刷新（节省资源）
    if (!isPageVisible && REFRESH_PAUSED_ON_HIDDEN) {
      scheduleNextRefresh();
      return;
    }

    // 防止并发请求
    if (isRefreshing) return;
    isRefreshing = true;

    // 更新按钮状态
    var refreshBtn = document.getElementById('refresh-btn');
    if (refreshBtn) {
      refreshBtn.textContent = '刷新中...';
      refreshBtn.disabled = true;
    }

    apiGet()
      .then(function(data) {
        consecutiveErrors = 0;
        lastErrorMessage = '';
        updateConnectionStatus('connected');
        renderAll(data);
      })
      .catch(function(e) {
        consecutiveErrors++;
        lastErrorMessage = e.message;
        console.warn('[Dashboard] 连续错误次数:', consecutiveErrors, e.message);
        if (consecutiveErrors >= 3) {
          updateConnectionStatus('error', e.message);
        }
      })
      .finally(function() {
        isRefreshing = false;
        // 恢复按钮
        var btn = document.getElementById('refresh-btn');
        if (btn) {
          btn.textContent = '刷新';
          btn.disabled = false;
        }
        scheduleNextRefresh();
      });
  }

  function scheduleNextRefresh() {
    if (refreshTimer) {
      clearTimeout(refreshTimer);
      refreshTimer = null;
    }
    // 连续错误时指数退避：3s → 6s → 12s → 30s（封顶）
    var interval = REFRESH_INTERVAL;
    if (consecutiveErrors >= 3) {
      interval = Math.min(REFRESH_INTERVAL * Math.pow(2, consecutiveErrors - 2), 30000);
    }
    refreshTimer = setTimeout(refresh, interval);
  }

  function startAutoRefresh() {
    if (refreshTimer) {
      clearTimeout(refreshTimer);
    }
    consecutiveErrors = 0;
    refresh(); // 立即执行一次
  }

  function stopAutoRefresh() {
    if (refreshTimer) {
      clearTimeout(refreshTimer);
      refreshTimer = null;
    }
  }

  // --- Demo Mode: 定期尝试重连真实 API ---
  function startDemoRetry() {
    if (demoRetryTimer) return;
    demoRetryTimer = setInterval(function() {
      if (!isDemoMode) return;
      console.log('[Dashboard] 尝试重连真实 API...');
      apiGet().then(function(data) {
        if (data) {
          console.log('[Dashboard] 重连成功，切换到真实数据');
          isDemoMode = false;
          clearInterval(demoRetryTimer);
          demoRetryTimer = null;
          renderAll(data);
          startAutoRefresh();
          updateConnectionStatus('connected');
        }
      }).catch(function() {
        // 仍不可用，继续等待
      });
    }, DEMO_RETRY_INTERVAL);
  }

  // --- Page Visibility: 后台标签页暂停刷新 ---
  function handleVisibilityChange() {
    isPageVisible = !document.hidden;
    if (isPageVisible) {
      // 回到前台，立即刷新一次（先清理旧的定时器，避免重复调度）
      if (refreshTimer) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
      }
      if (!isDemoMode) {
        refresh();
      }
    }
  }

  // --- Event Handlers ---
  function initEvents() {
    // Auto toggle
    document.getElementById('auto-toggle').addEventListener('click', function() {
      var willEnable = !this.classList.contains('active');
      apiPost({ action: 'toggle', enabled: willEnable }).then(function(result) {
        if (result.ok !== false) {
          // 用返回的 state 更新，省一次请求
          if (result.state) {
            renderAll(normalizeData(result.state));
          } else {
            renderToggle(willEnable);
          }
        }
      });
    });

    // Refresh button
    document.getElementById('refresh-btn').addEventListener('click', function() {
      if (isRefreshing) return;
      // 手动刷新时重置错误计数
      consecutiveErrors = 0;
      refresh();
    });

    // Chart range
    document.getElementById('chart-range').addEventListener('change', function() {
      if (currentData) {
        // 强制重新渲染图表（重置缓存 hash）
        lastEquityCurveHash = '';
        renderEquityChart(currentData);
      }
    });

    // Trade filter
    document.getElementById('trade-filter').addEventListener('change', function() {
      if (currentData) renderTrades(currentData);
    });

    // Config inputs: 用计数器方式标记编辑状态，支持 Tab 切换不丢失
    var configInputs = [
      'cfg-max-pos', 'cfg-max-lots', 'cfg-default-lots',
      'cfg-cooldown', 'cfg-trailing-lock'
    ];
    configInputs.forEach(function(id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.addEventListener('focus', function() {
        editingConfigCount++;
        isEditingConfig = true;
      });
      el.addEventListener('blur', function() {
        // 延迟一小会，防止点击保存按钮时瞬间失去焦点就恢复了
        var self = this;
        setTimeout(function() {
          editingConfigCount = Math.max(0, editingConfigCount - 1);
          if (editingConfigCount === 0) {
            isEditingConfig = false;
          }
        }, 200);
      });
    });

    // Trailing toggle in config
    document.getElementById('cfg-trailing').addEventListener('click', function() {
      editingConfigCount++;
      isEditingConfig = true;
      this.classList.toggle('active');
      // 点击后也延迟重置编辑状态
      var self = this;
      setTimeout(function() {
        editingConfigCount = Math.max(0, editingConfigCount - 1);
        if (editingConfigCount === 0) {
          isEditingConfig = false;
        }
      }, 500);
    });

    // Save config
    document.getElementById('save-config-btn').addEventListener('click', function() {
      isEditingConfig = false;
      editingConfigCount = 0;
      var btn = this;
      var originalText = btn.textContent;
      btn.textContent = '保存中...';
      btn.disabled = true;

      var trailingEl = document.getElementById('cfg-trailing');
      var config = {
        max_positions: parseInt(document.getElementById('cfg-max-pos').value),
        max_lots_per_trade: parseInt(document.getElementById('cfg-max-lots').value),
        default_lots: parseInt(document.getElementById('cfg-default-lots').value),
        cooldown_minutes: parseInt(document.getElementById('cfg-cooldown').value),
        enable_trailing: trailingEl.classList.contains('active'),
        trailing_lock_r: parseFloat(document.getElementById('cfg-trailing-lock').value)
      };

      apiPost({ action: 'config', config: config }).then(function(result) {
        btn.textContent = originalText;
        btn.disabled = false;
        if (result.ok !== false) {
          // 成功提示
          btn.textContent = '✓ 已保存';
          setTimeout(function() { btn.textContent = originalText; }, 1500);
          // 用返回的 state 更新
          if (result.state) {
            renderAll(normalizeData(result.state));
          } else {
            refresh();
          }
        } else {
          alert('保存失败: ' + (result.msg || result.error || '未知错误'));
        }
      });
    });

    // Reset button
    document.getElementById('reset-btn').addEventListener('click', function() {
      if (confirm('确定要重置模拟账户吗？所有持仓和历史记录将被清空。')) {
        apiPost({ action: 'reset' }).then(function(result) {
          if (result.ok !== false) {
            alert('账户已重置');
            if (result.state) {
              renderAll(normalizeData(result.state));
            } else {
              refresh();
            }
          } else {
            alert('重置失败: ' + (result.msg || result.error || '未知错误'));
          }
        });
      }
    });

    // Page visibility
    document.addEventListener('visibilitychange', handleVisibilityChange);
  }

  // --- Demo Mode (when API not available) ---
  function loadDemoData() {
    var now = Date.now() / 1000;
    var equity = 1000000;
    var curve = [];
    for (var i = 30; i >= 0; i--) {
      equity += (Math.random() - 0.4) * 15000;
      equity = Math.max(950000, equity);
      curve.push([now - i * 86400, Math.round(equity)]);
    }

    return {
      enabled: true,
      config: {
        max_positions: 8,
        max_lots_per_trade: 5,
        default_lots: 1,
        cooldown_minutes: 30,
        enable_trailing: true,
        trailing_lock_r: 0.5
      },
      positions: {
        'IF2409': {
          symbol: 'IF2409',
          direction: 'long',
          lots: 2,
          entry_price: 3950.0,
          current_price: 3985.6,
          stop_loss: 3920.0,
          take_profit: 4010.0,
          unrealized_pnl: 21300,
          r_pnl: 1.18
        },
        'IC2409': {
          symbol: 'IC2409',
          direction: 'short',
          lots: 1,
          entry_price: 5620.0,
          current_price: 5598.4,
          stop_loss: 5660.0,
          take_profit: 5560.0,
          unrealized_pnl: 4320,
          r_pnl: 0.54
        },
        'RB2410': {
          symbol: 'RB2410',
          direction: 'long',
          lots: 3,
          entry_price: 3480.0,
          current_price: 3462.0,
          stop_loss: 3450.0,
          take_profit: 3540.0,
          unrealized_pnl: -5400,
          r_pnl: -0.3
        }
      },
      stats: {
        equity: 1038750,
        initial_equity: 1000000,
        total_pnl: 38750,
        total_trades: 27,
        wins: 16,
        losses: 11,
        win_rate: 16 / 27,
        profit_factor: 1.85,
        max_drawdown: 28500,
        expR: 0.42,
        avg_hold_time: 2450,
        max_win_streak: 4,
        max_loss_streak: 3,
        avg_win: 5200,
        avg_loss: -2800,
        sharpe: 1.24,
        calmar: 2.15
      },
      equity_curve: curve,
      trade_history: [
        { timestamp: now - 1800, symbol: 'IF2409', direction: 'long', type: 'open', lots: 2, entry_price: 3950, reason: '信号建仓' },
        { timestamp: now - 3600, symbol: 'IC2409', direction: 'short', type: 'open', lots: 1, entry_price: 5620, reason: '信号建仓' },
        { timestamp: now - 7200, symbol: 'RB2410', direction: 'long', type: 'open', lots: 3, entry_price: 3480, reason: '信号建仓' },
        { timestamp: now - 10800, symbol: 'IH2409', direction: 'long', type: 'close', lots: 2, entry_price: 2720, exit_price: 2765, pnl: 18000, r_pnl: 2.0, reason: '止盈T2' },
        { timestamp: now - 14400, symbol: 'M2409', direction: 'short', type: 'close', lots: 2, entry_price: 3280, exit_price: 3256, pnl: 4800, r_pnl: 1.2, reason: '止盈T1' },
        { timestamp: now - 18000, symbol: 'CU2409', direction: 'long', type: 'close', lots: 1, entry_price: 68500, exit_price: 68200, pnl: -15000, r_pnl: -1.0, reason: '止损' },
        { timestamp: now - 21600, symbol: 'AU2410', direction: 'long', type: 'close', lots: 2, entry_price: 568, exit_price: 575, pnl: 14000, r_pnl: 1.75, reason: '移动止损' },
        { timestamp: now - 25200, symbol: 'AG2409', direction: 'short', type: 'close', lots: 3, entry_price: 6200, exit_price: 6280, pnl: -24000, r_pnl: -1.0, reason: '止损' },
        { timestamp: now - 28800, symbol: 'MA2409', direction: 'long', type: 'close', lots: 2, entry_price: 2350, exit_price: 2400, pnl: 10000, r_pnl: 2.0, reason: '止盈T2' }
      ]
    };
  }

  // --- Init ---
  function init() {
    // 给交易记录容器加 id，用于滚动位置保存
    var tradesWrap = document.querySelector('.panel.col-full .table-wrap');
    if (tradesWrap && !tradesWrap.id) {
      tradesWrap.id = 'trades-wrap';
    }

    initEvents();
    updateConnectionStatus('loading');

    // Try real API first
    apiGet().then(function(data) {
      if (data) {
        renderAll(data);
        startAutoRefresh();
      } else {
        throw new Error('数据为空');
      }
    }).catch(function(e) {
      // Fallback to demo mode
      console.log('[Dashboard] API 不可用，使用演示数据:', e.message);
      isDemoMode = true;
      lastErrorMessage = e.message;
      updateConnectionStatus('error', e.message);
      var demoData = loadDemoData();
      renderAll(demoData);
      // Demo 模式下定期尝试重连真实 API
      startDemoRetry();
    });
  }

  // Start
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
