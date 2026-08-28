#!/usr/bin/env node
/**
 * 用户交互锁 — 单元测试
 * ========================
 *
 * 覆盖场景：
 * 1. 基础加锁/解锁
 * 2. 引用计数（嵌套调用）
 * 3. 防抖释放
 * 4. 看门狗强制释放
 * 5. DOM 冻结引用计数
 * 6. 历史 bug 回归：快速多次点击 → 引用计数不会错
 * 7. 历史 bug 回归：异常退出 → 看门狗兜底
 *
 * 对应历史 bug（决策 25：锁机制重构）：
 *   - 问题1：布尔锁 + 快速多次点击 → 竞态条件，锁提前释放
 *   - 问题2：操作中途异常退出 → 锁永久阻塞
 *   - 修复：引用计数 + 防抖释放 + 看门狗 三层保障
 */

'use strict';

const UserActionLock = require('./user_action_lock.js');

// ── 假时间（Fake Timer）工具 ──────────────────────────────────────────────
// 用一个简单的假时间系统来模拟 setTimeout/setInterval/Date.now
// 避免真实等待，测试又快又精确

class FakeTimer {
  constructor() {
    this._now = 0;           // 当前时间（ms）
    this._timers = new Map(); // 定时器 ID → { fireAt, callback, interval }
    this._nextId = 1;
  }

  now() {
    return this._now;
  }

  setTimeout(callback, delayMs) {
    const id = this._nextId++;
    this._timers.set(id, {
      fireAt: this._now + delayMs,
      callback,
      interval: 0,
    });
    return id;
  }

  clearTimeout(id) {
    this._timers.delete(id);
  }

  setInterval(callback, intervalMs) {
    const id = this._nextId++;
    this._timers.set(id, {
      fireAt: this._now + intervalMs,
      callback,
      interval: intervalMs,
    });
    return id;
  }

  clearInterval(id) {
    this._timers.delete(id);
  }

  /**
   * 推进时间 ms 毫秒，期间触发所有到期的定时器。
   */
  tick(ms) {
    const target = this._now + ms;

    // 循环触发所有到期的定时器（可能连环触发）
    let maxIter = 1000; // 安全上限
    while (maxIter-- > 0) {
      // 找到最早到期的定时器
      let earliestId = null;
      let earliestTime = Infinity;

      for (const [id, t] of this._timers) {
        if (t.fireAt <= target && t.fireAt < earliestTime) {
          earliestId = id;
          earliestTime = t.fireAt;
        }
      }

      if (earliestId === null) break; // 没有到期的了

      // 推进到那个时间点
      this._now = earliestTime;

      const timer = this._timers.get(earliestId);
      if (!timer) continue; // 可能已被清除

      // 如果是 interval，先设置下一次触发（再执行回调，
      // 与真实 setInterval 行为一致：回调执行期间已经排好了下一次）
      if (timer.interval > 0) {
        timer.fireAt = this._now + timer.interval;
      } else {
        // setTimeout 执行后删除
        this._timers.delete(earliestId);
      }

      // 执行回调
      try {
        timer.callback();
      } catch (e) {
        // 测试中不吞错误
        throw e;
      }
    }

    // 推进到目标时间
    this._now = target;
  }

  /** 当前活跃的定时器数量 */
  get activeTimerCount() {
    return this._timers.size;
  }
}

// ── 测试辅助 ────────────────────────────────────────────────────────────

function createLockWithFakeTimer(options = {}) {
  const fake = new FakeTimer();
  const lock = new UserActionLock({
    releaseDelayMs: 5000,   // 5s 防抖释放
    maxHoldMs: 15000,       // 15s 看门狗
    watchdogInterval: 10000, // 10s 检查
    timeSource: {
      now: () => fake.now(),
      setTimeout: (fn, ms) => fake.setTimeout(fn, ms),
      clearTimeout: (id) => fake.clearTimeout(id),
      setInterval: (fn, ms) => fake.setInterval(fn, ms),
      clearInterval: (id) => fake.clearInterval(id),
    },
    ...options,
  });
  return { lock, fake };
}

// 简单的断言工具
let passed = 0;
let failed = 0;
const failures = [];

function assert(condition, msg) {
  if (condition) {
    passed++;
  } else {
    failed++;
    failures.push(msg);
    console.error(`  ❌ ${msg}`);
  }
}

function assertEq(actual, expected, msg) {
  if (actual === expected) {
    passed++;
  } else {
    failed++;
    failures.push(`${msg}: expected ${expected}, got ${actual}`);
    console.error(`  ❌ ${msg}: expected ${expected}, got ${actual}`);
  }
}

function assertClose(actual, expected, delta, msg) {
  if (Math.abs(actual - expected) <= delta) {
    passed++;
  } else {
    failed++;
    failures.push(`${msg}: expected ~${expected} (±${delta}), got ${actual}`);
    console.error(`  ❌ ${msg}: expected ~${expected} (±${delta}), got ${actual}`);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 1：基础加锁/解锁
// ═══════════════════════════════════════════════════════════════════════════

function test_basic_lock_unlock() {
  console.log('\n📋 基础加锁/解锁');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    // 初始状态：未锁
    assertEq(lock.isLocked, false, '初始状态：未锁定');
    assertEq(lock.refCount, 0, '初始状态：引用计数为 0');

    // 加锁
    lock.start();
    assertEq(lock.isLocked, true, 'start() 后：已锁定');
    assertEq(lock.refCount, 1, 'start() 后：引用计数 = 1');
    assertEq(lock.heldMs, 0, '刚加锁：持有时间 = 0');

    // 时间过了 1s
    fake.tick(1000);
    assertClose(lock.heldMs, 1000, 1, '1s 后：持有时间 ≈ 1000ms');

    // 解锁 → 引用归零，安排防抖释放
    lock.end();
    assertEq(lock.refCount, 0, 'end() 后：引用计数 = 0');
    // 注意：防抖释放前，isLocked 可能还是 true（5s 后才真正释放）
    // 等等——让我看看逻辑：end() 里 refCount 归零后，调用 _scheduleRelease
    // 但 isLocked 还没变成 false，要等防抖定时器触发才变
    assertEq(lock.isLocked, true, '刚 end() 后：防抖期间仍是锁定状态');

    // 时间过了 4999ms（还差 1ms 到 5s）
    fake.tick(4999);
    assertEq(lock.isLocked, true, '4999ms 后：防抖未到期，仍锁定');

    // 再推进 2ms → 超过 5s
    fake.tick(2);
    assertEq(lock.isLocked, false, '5001ms 后：防抖释放，已解锁');
    assertEq(lock.refCount, 0, '释放后：引用计数 = 0');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 2：引用计数（嵌套调用）
// ═══════════════════════════════════════════════════════════════════════════

function test_ref_count_nesting() {
  console.log('\n📋 引用计数 & 嵌套调用');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    // 嵌套加锁 3 次
    lock.start(); // ref=1
    lock.start(); // ref=2
    lock.start(); // ref=3

    assertEq(lock.refCount, 3, '3 次 start 后：refCount = 3');
    assertEq(lock.isLocked, true, '3 次 start 后：已锁定');

    // 解 1 次 → ref=2，仍锁定
    lock.end();
    assertEq(lock.refCount, 2, '1 次 end 后：refCount = 2');
    assertEq(lock.isLocked, true, 'ref=2 时：仍锁定');

    // 再解 1 次 → ref=1
    lock.end();
    assertEq(lock.refCount, 1, '2 次 end 后：refCount = 1');

    // 再解 1 次 → ref=0，安排防抖释放
    lock.end();
    assertEq(lock.refCount, 0, '3 次 end 后：refCount = 0');
    assertEq(lock.isLocked, true, '刚归零：防抖期间仍锁定');

    // 5s 后释放
    fake.tick(5001);
    assertEq(lock.isLocked, false, '防抖释放后：已解锁');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 3：防抖释放 — 新操作重置计时器
// ═══════════════════════════════════════════════════════════════════════════

function test_debounce_reset() {
  console.log('\n📋 防抖释放 — 新操作重置计时器');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    lock.start();
    lock.end();  // ref=0，安排 5s 后释放

    // 过了 3s（还没释放）
    fake.tick(3000);
    assertEq(lock.isLocked, true, '3s 后：防抖未到期，仍锁定');

    // 又来了一次操作！
    lock.start(); // ref=1，清除防抖定时器
    assertEq(lock.isLocked, true, '新操作：仍锁定');
    assertEq(lock.refCount, 1, '新操作：refCount = 1');

    // 立即结束
    lock.end(); // ref=0，重新安排 5s 防抖

    // 再等 3s（从新的 start 算起是 3s，从上一次算起是 6s）
    fake.tick(3000);
    assertEq(lock.isLocked, true, '新操作后 3s：仍在防抖期内');

    // 再等 2001ms → 总共 5001ms（从第二次 end 算起）
    fake.tick(2001);
    assertEq(lock.isLocked, false, '第二次 end 后 5s+：防抖释放');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 4：看门狗强制释放
// ═══════════════════════════════════════════════════════════════════════════

function test_watchdog_force_release() {
  console.log('\n📋 看门狗 — 超时强制释放');
  const { lock, fake } = createLockWithFakeTimer();

  let watchdogFired = false;
  let watchdogHeldMs = 0;
  lock.onWatchdogForceRelease = (heldMs, refCount) => {
    watchdogFired = true;
    watchdogHeldMs = heldMs;
  };

  try {
    lock.start();
    assertEq(lock.isLocked, true, '初始：已锁定');

    // 看门狗每 10s 检查一次，阈值 15s
    // 10s 时第一次检查 → 10s < 15s → 不释放
    fake.tick(10000);
    assertEq(lock.isLocked, true, '10s 后：看门狗第一次检查，未超时，仍锁定');
    assertEq(watchdogFired, false, '10s 时：看门狗未触发');

    // 再走 6s → 总共 16s，超过 15s 阈值
    // 但看门狗要等到下一次检查（20s 时）才会发现...
    // 等等，让我重新算：看门狗是 setInterval 10s，所以检查点在 10s, 20s, 30s...
    // 10s 时检查：held = 10s < 15s → 不释放
    // 20s 时检查：held = 20s > 15s → 强制释放

    fake.tick(10000); // 到 20s
    assertEq(watchdogFired, true, '20s 时：看门狗触发强制释放');
    assertEq(lock.isLocked, false, '看门狗触发后：已释放');
    assertEq(lock.refCount, 0, '看门狗触发后：引用计数归零');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 5：DOM 冻结引用计数
// ═══════════════════════════════════════════════════════════════════════════

function test_dom_freeze() {
  console.log('\n📋 DOM 冻结 — 引用计数');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    assertEq(lock.isDomFrozen, false, '初始：DOM 未冻结');
    assertEq(lock.domFrozenRefCount, 0, '初始：DOM 冻结计数 = 0');

    // 冻结 3 次
    lock.freezeDom();
    lock.freezeDom();
    lock.freezeDom();

    assertEq(lock.domFrozenRefCount, 3, '3 次 freeze：计数 = 3');
    assertEq(lock.isDomFrozen, true, '3 次 freeze：已冻结');

    // 解冻 1 次
    lock.unfreezeDom();
    assertEq(lock.domFrozenRefCount, 2, '1 次 unfreeze：计数 = 2');
    assertEq(lock.isDomFrozen, true, '计数 2：仍冻结');

    // 再解冻 2 次 → 完全解冻
    lock.unfreezeDom();
    lock.unfreezeDom();
    assertEq(lock.domFrozenRefCount, 0, '全部 unfreeze：计数 = 0');
    assertEq(lock.isDomFrozen, false, '全部 unfreeze：已解冻');

    // 继续解冻 → 不会变负
    lock.unfreezeDom();
    assertEq(lock.domFrozenRefCount, 0, '过度 unfreeze：计数不低于 0');
    assertEq(lock.isDomFrozen, false, '过度 unfreeze：仍未冻结');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 6：历史 bug 回归 — 快速多次点击
// ═══════════════════════════════════════════════════════════════════════════

function test_regression_rapid_clicks() {
  console.log('\n📋 历史 bug 回归：快速多次点击（原布尔锁竞态）');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    // 模拟用户快速连续点击 5 次（每次点击都 start + 异步 end）
    // 旧的布尔锁会因为竞态提前释放，但引用计数不会

    // 5 次快速 start
    for (let i = 0; i < 5; i++) {
      lock.start();
    }
    assertEq(lock.refCount, 5, '5 次快速 start：refCount = 5');
    assertEq(lock.isLocked, true, '5 次快速 start：已锁定');

    // 5 次快速 end
    for (let i = 0; i < 5; i++) {
      lock.end();
    }
    assertEq(lock.refCount, 0, '5 次快速 end：refCount = 0');
    // 防抖期间仍锁定
    assertEq(lock.isLocked, true, '快速 end 后：防抖期间仍锁定');

    // 5s 后释放
    fake.tick(5001);
    assertEq(lock.isLocked, false, '防抖释放后：已解锁');

    // 关键验证：没有出现"负引用计数"或"提前释放"
    assert(lock.refCount >= 0, '引用计数始终 >= 0');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 7：历史 bug 回归 — 异常退出导致永久阻塞
// ═══════════════════════════════════════════════════════════════════════════

function test_regression_crash_while_locked() {
  console.log('\n📋 历史 bug 回归：操作中途异常 → 看门狗兜底');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    // 模拟：start 了但代码异常，没调用 end
    lock.start();
    // 假设这里崩了，end() 永远不会被调用...

    assertEq(lock.isLocked, true, '异常后：锁被持有');
    assertEq(lock.refCount, 1, '异常后：refCount = 1（没有机会 end）');

    // 10s → 看门狗第一次检查，10s < 15s → 不释放
    fake.tick(10000);
    assertEq(lock.isLocked, true, '10s：看门狗检查，未超时');

    // 到 20s → 看门狗第二次检查，20s > 15s → 强制释放
    fake.tick(10000);
    assertEq(lock.isLocked, false, '20s：看门狗强制释放，不再永久阻塞');
    assertEq(lock.refCount, 0, '强制释放后：引用计数归零');
    assertEq(lock.isDomFrozen, false, '强制释放后：DOM 也解冻了');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 8：forceRelease 手动强制释放
// ═══════════════════════════════════════════════════════════════════════════

function test_force_release() {
  console.log('\n📋 forceRelease 手动强制释放');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    // 加锁 + DOM 冻结
    lock.start();
    lock.start(); // ref=2
    lock.freezeDom();
    lock.freezeDom(); // domRef=2

    assertEq(lock.isLocked, true, '加锁后：已锁定');
    assertEq(lock.isDomFrozen, true, '冻结后：DOM 已冻结');

    // 强制释放
    const wasLocked = lock.forceRelease();
    assertEq(wasLocked, true, 'forceRelease 返回 true（之前确实锁着）');
    assertEq(lock.isLocked, false, '强制释放后：已解锁');
    assertEq(lock.refCount, 0, '强制释放后：引用计数归零');
    assertEq(lock.isDomFrozen, false, '强制释放后：DOM 已解冻');
    assertEq(lock.domFrozenRefCount, 0, '强制释放后：DOM 计数归零');

    // 没锁的时候调用 forceRelease
    const wasLocked2 = lock.forceRelease();
    assertEq(wasLocked2, false, '未锁定时 forceRelease 返回 false');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 9：防抖期间新操作 → 重新计时
// ═══════════════════════════════════════════════════════════════════════════

function test_debounce_renew_by_new_action() {
  console.log('\n📋 防抖期间新操作 → 重新计时');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    // 第一次操作
    lock.start();
    lock.end(); // ref=0，5s 防抖

    // 3s 后（还有 2s 释放），用户又来了一次操作
    fake.tick(3000);
    lock.start(); // 清除防抖定时器
    lock.end();   // ref=0，重新开始 5s 防抖

    // 从第二次 end 算起，3s 后（总时间 6s）
    fake.tick(3000);
    assertEq(lock.isLocked, true, '第二次 end 后 3s：仍锁定（防抖重置了）');

    // 再 2001ms → 第二次 end 后 5001ms → 释放
    fake.tick(2001);
    assertEq(lock.isLocked, false, '第二次 end 后 5s+：释放');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  测试 10：heldMs 准确性
// ═══════════════════════════════════════════════════════════════════════════

function test_held_ms_accuracy() {
  console.log('\n📋 heldMs 持有时间准确性');
  const { lock, fake } = createLockWithFakeTimer();

  try {
    assertEq(lock.heldMs, 0, '未锁定时：heldMs = 0');

    lock.start();
    assertEq(lock.heldMs, 0, '刚加锁：heldMs = 0');

    fake.tick(1234);
    assertClose(lock.heldMs, 1234, 1, '1234ms 后：heldMs ≈ 1234');

    fake.tick(8766);
    assertClose(lock.heldMs, 10000, 1, '10000ms 后：heldMs ≈ 10000');

    // 释放后 heldMs 归零
    lock.end();
    fake.tick(5001); // 防抖释放
    assertEq(lock.heldMs, 0, '释放后：heldMs = 0');

  } finally {
    lock.destroy();
  }
}

// ═══════════════════════════════════════════════════════════════════════════
//  main
// ═══════════════════════════════════════════════════════════════════════════

function main() {
  console.log('═══════════════════════════════════════════════');
  console.log('  用户交互锁 — 单元测试');
  console.log('═══════════════════════════════════════════════');

  test_basic_lock_unlock();
  test_ref_count_nesting();
  test_debounce_reset();
  test_watchdog_force_release();
  test_dom_freeze();
  test_regression_rapid_clicks();
  test_regression_crash_while_locked();
  test_force_release();
  test_debounce_renew_by_new_action();
  test_held_ms_accuracy();

  console.log('\n═══════════════════════════════════════════════');
  console.log(`  结果：${passed} 通过，${failed} 失败`);
  console.log('═══════════════════════════════════════════════');

  if (failed > 0) {
    console.log('\n❌ 失败的测试：');
    failures.forEach((f, i) => console.log(`  ${i + 1}. ${f}`));
    process.exit(1);
  } else {
    console.log('\n✅ 全部通过！');
    process.exit(0);
  }
}

main();
