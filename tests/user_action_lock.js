/**
 * 用户交互锁 — 核心逻辑模块
 * ============================
 *
 * 从 four_dim_live.html 中提取的纯状态机逻辑，便于单元测试。
 * 3 层安全保障：
 *   1. 引用计数 — 支持嵌套调用，防止提前释放
 *   2. 防抖释放 — 最后一次操作后 delayMs 才真正释放
 *   3. 看门狗 — 锁被持有超过 maxHoldMs 时强制释放
 *
 * 对应历史 bug（决策 25：锁机制重构）：
 *   - 原机制（_userActionInProgress 布尔值）存在竞态条件
 *   - 快速多次点击信号 → 锁提前释放或永久阻塞
 *   - 重构为 3 层安全保障机制
 */

'use strict';

class UserActionLock {
  /**
   * @param {object} options
   * @param {number} options.releaseDelayMs  防抖释放延迟（默认 5000ms = 5s）
   * @param {number} options.maxHoldMs       看门狗最大持有时间（默认 15000ms = 15s）
   * @param {object} options.timeSource      时间源（注入依赖，便于测试）
   * @param {function} options.timeSource.now       返回当前时间戳(ms)
   * @param {function} options.timeSource.setTimeout  设置定时器
   * @param {function} options.timeSource.clearTimeout 清除定时器
   * @param {function} options.timeSource.setInterval  设置间隔定时器
   * @param {function} options.timeSource.clearInterval 清除间隔定时器
   */
  constructor(options = {}) {
    const {
      releaseDelayMs = 5000,
      maxHoldMs = 15000,
      timeSource = null,
    } = options;

    this.releaseDelayMs = releaseDelayMs;
    this.maxHoldMs = maxHoldMs;

    // 时间源（默认使用全局 setTimeout/setInterval/Date.now）
    this._time = timeSource || {
      now: () => Date.now(),
      setTimeout: (fn, ms) => setTimeout(fn, ms),
      clearTimeout: (id) => clearTimeout(id),
      setInterval: (fn, ms) => setInterval(fn, ms),
      clearInterval: (id) => clearInterval(id),
    };

    // 状态
    this._inProgress = false;
    this._refCount = 0;
    this._lockTime = null;       // 最近一次加锁的时间戳（null=未设置）
    this._releaseTimer = null;   // 防抖释放定时器
    this._watchdogTimer = null;  // 看门狗定时器
    this._watchdogInterval = 10000; // 看门狗检查间隔（10s）

    // DOM 冻结计数（与锁联动）
    this._domFrozen = false;
    this._domFrozenRefCount = 0;

    // 事件回调（用于测试和日志）
    this.onLockStart = null;
    this.onLockEnd = null;
    this.onWatchdogForceRelease = null;
    this.onDomFreeze = null;
    this.onDomUnfreeze = null;

    // 启动看门狗
    this._startWatchdog();
  }

  // ── 公共 API ──────────────────────────────────────────────────────────

  /**
   * 开始用户操作（加锁）。
   * 支持嵌套调用：多次调用会增加引用计数。
   */
  start() {
    this._refCount++;
    this._lockTime = this._time.now();

    const wasFirst = !this._inProgress;
    this._inProgress = true;

    // 清除之前的释放定时器（防抖）
    if (this._releaseTimer) {
      this._time.clearTimeout(this._releaseTimer);
      this._releaseTimer = null;
    }

    if (wasFirst && this.onLockStart) {
      this.onLockStart(this._refCount);
    }

    return this._refCount;
  }

  /**
   * 结束用户操作（减引用计数）。
   * 引用计数归零时，安排防抖释放。
   */
  end() {
    this._refCount = Math.max(0, this._refCount - 1);

    if (this._refCount <= 0) {
      // 引用归零 → 安排防抖释放
      this._scheduleRelease(this.releaseDelayMs);
    }

    return this._refCount;
  }

  /**
   * 强制立即释放锁（包括 DOM 冻结）。
   * 用于看门狗和紧急情况。
   */
  forceRelease() {
    const wasLocked = this._inProgress;

    this._refCount = 0;
    this._inProgress = false;
    this._lockTime = null;

    // 清除防抖定时器
    if (this._releaseTimer) {
      this._time.clearTimeout(this._releaseTimer);
      this._releaseTimer = null;
    }

    // 重置 DOM 冻结
    if (this._domFrozenRefCount > 0) {
      this._domFrozenRefCount = 0;
      this._domFrozen = false;
      if (this.onDomUnfreeze) {
        this.onDomUnfreeze(0);
      }
    }

    if (wasLocked && this.onLockEnd) {
      this.onLockEnd('force');
    }

    return wasLocked;
  }

  /**
   * 冻结 DOM（引用计数式）。
   */
  freezeDom() {
    this._domFrozenRefCount++;
    const wasFirst = !this._domFrozen;
    this._domFrozen = true;

    if (wasFirst && this.onDomFreeze) {
      this.onDomFreeze(this._domFrozenRefCount);
    }

    return this._domFrozenRefCount;
  }

  /**
   * 解冻 DOM（引用计数式）。
   */
  unfreezeDom() {
    this._domFrozenRefCount = Math.max(0, this._domFrozenRefCount - 1);

    if (this._domFrozenRefCount <= 0 && this._domFrozen) {
      this._domFrozen = false;
      if (this.onDomUnfreeze) {
        this.onDomUnfreeze(0);
      }
    }

    return this._domFrozenRefCount;
  }

  // ── 状态查询 ──────────────────────────────────────────────────────────

  get isLocked() {
    return this._inProgress;
  }

  get refCount() {
    return this._refCount;
  }

  get isDomFrozen() {
    return this._domFrozen;
  }

  get domFrozenRefCount() {
    return this._domFrozenRefCount;
  }

  /** 锁已持有的时间（ms）。没锁时返回 0。 */
  get heldMs() {
    if (!this._inProgress || this._lockTime === null) return 0;
    return this._time.now() - this._lockTime;
  }

  // ── 内部方法 ──────────────────────────────────────────────────────────

  /**
   * 防抖释放：最后一次操作后 delayMs 才真正释放锁。
   * 如果在延迟期间又有新操作，定时器被清除（在 start() 中），重新计时。
   */
  _scheduleRelease(delayMs) {
    // 先清除已有定时器
    if (this._releaseTimer) {
      this._time.clearTimeout(this._releaseTimer);
    }

    this._releaseTimer = this._time.setTimeout(() => {
      this._releaseTimer = null;

      if (this._refCount <= 0) {
        // 引用确实归零了 → 完全释放
        this._inProgress = false;
        this._lockTime = null;
        if (this.onLockEnd) {
          this.onLockEnd('debounce');
        }
      } else {
        // 引用又不为 0 了（期间有新操作）→ 继续等待
        this._scheduleRelease(delayMs);
      }
    }, delayMs);
  }

  /**
   * 启动看门狗：每 10s 检查一次，持有超过 maxHoldMs 则强制释放。
   */
  _startWatchdog() {
    this._watchdogTimer = this._time.setInterval(() => {
      if (this._inProgress && this._lockTime !== null) {
        const held = this._time.now() - this._lockTime;
        if (held > this.maxHoldMs) {
          // 超过最大持有时间 → 强制释放
          if (this.onWatchdogForceRelease) {
            this.onWatchdogForceRelease(held, this._refCount);
          }
          this.forceRelease();
        }
      }
    }, this._watchdogInterval);
  }

  /**
   * 销毁锁实例（清除所有定时器）。
   * 测试结束时必须调用，防止定时器挂住进程。
   */
  destroy() {
    if (this._releaseTimer) {
      this._time.clearTimeout(this._releaseTimer);
      this._releaseTimer = null;
    }
    if (this._watchdogTimer) {
      this._time.clearInterval(this._watchdogTimer);
      this._watchdogTimer = null;
    }
  }
}

// ── 导出 ────────────────────────────────────────────────────────────────
if (typeof module !== 'undefined' && module.exports) {
  module.exports = UserActionLock;
}
