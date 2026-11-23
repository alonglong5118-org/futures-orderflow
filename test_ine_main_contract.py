"""负样本测试：INE 主力口径（2026-10-15）

缺陷：INE 结算价表 futures_settle_ine 无成交量/持仓量（实测 volume/OI 全 0），
      旧实现「近月优先」(YYMM 最小) 在换月期会选错：
      SC2611 未摘牌 → YYMM 最小 → 被选成主力，而真实主力是 OI 更大的 SC2612。
      结果面板持续弹「系统 SC2612 vs 真主力 SC2611」的**反向假警**（把对的当错的）。

修法：INE 主力改以 minishare_hot_contracts.json（实时 OI 排名）为权威源，
      结算价近月口径降级为 OI 源缺失时的兜底。
"""
import json
import os
import shutil
import sys
import tempfile

D = "/Users/a123/WorkBuddy/2026-09-04-07-56-14/fourd_run"
sys.path.insert(0, D)

import refresh_main_contracts as R  # noqa: E402

PASS, FAIL = [], []


def chk(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {extra}" if extra else ""))


# ---- 1) INE 白名单是 module 级常量（跨函数共用）----
print("=== 1. 常量定义 ===")
chk("_INE_ONLY_SET 已定义", hasattr(R, "_INE_ONLY_SET") and R._INE_ONLY_SET == {"sc"},
    str(getattr(R, "_INE_ONLY_SET", None)))

# ---- 2) 真实数据：OI 源给出的 SC 主力 ----
print("=== 2. 真实 OI 源核实 ===")
hot = json.load(open(os.path.join(D, "minishare_hot_contracts.json"), encoding="utf-8"))
sc_rec = hot.get("SC") or hot.get("sc") or {}
print(f"  minishare SC 记录: main={sc_rec.get('main')} secondary={sc_rec.get('secondary')}")
chk("OI 源给出 SC2612（真主力）", str(sc_rec.get("main", "")).upper() == "SC2612",
    str(sc_rec.get("main")))

# ---- 3) _ine_main_from_hot 返回正确结果 ----
print("=== 3. OI 权威源读取 ===")
got = R._ine_main_from_hot()
chk("读出 sc → SC2612", got.get("sc") == "SC2612", str(got))
chk("只含 INE 独有品种（无 lu/nr/bc/ec 等 SHFE 品种）",
    set(got.keys()) <= {"sc"}, str(sorted(got.keys())))

# ---- 4) 实跑端到端：akshare 主力映射里 sc 已是真主力 ----
print("=== 4. 端到端 ak_main_all（实跑，需 akshare）===")
try:
    m, failed = R.ak_main_all()
    chk("ak_main_all 的 sc = SC2612", str(m.get("sc", "")).upper() == "SC2612",
        f"{m.get('sc')} failed={failed}")
    chk("总品种数合理（>50）", len(m) > 50, f"{len(m)}")
    chk("ine 未被标记失败", "ine" not in failed, str(failed))
    # 交叉验证：OI 源与结算价源对非 INE 品种应一致
    mism = [k for k in ("zn", "cu", "rb", "i", "ag")
            if m.get(k) and str(m[k]).upper() != str(
                (hot.get(k.upper()) or hot.get(k) or {}).get("main", "")).upper()
            and (hot.get(k.upper()) or hot.get(k) or {}).get("main")]
    chk("非 INE 品种与 minishare OI 一致（无副作用）", len(mism) == 0, f"不一致 {mism}")
except ImportError:
    chk("akshare 可用", False, "未安装 akshare，跳过")

# ---- 5) OI 源缺失时降级到结算价兜底（不崩、不返回空）----
print("=== 5. OI 源缺失降级 ===")
bak = R.CACHE
missing = os.path.join(tempfile.mkdtemp(), "no_such_hot.json")
try:
    R.CACHE = missing
    got2 = R._ine_main_from_hot()
    chk("OI 源缺失 → 返回空 dict（触发结算价兜底）", got2 == {}, str(got2))
except Exception as e:
    chk("OI 源缺失 → 返回空 dict", False, f"抛异常 {e}")
finally:
    R.CACHE = bak

# ---- 6) OI 源文件损坏时不得让整条链路挂掉 ----
print("=== 6. OI 源损坏容错 ===")
tmp_bad = os.path.join(tempfile.mkdtemp(), "bad.json")
try:
    R.CACHE = tmp_bad
    open(tmp_bad, "w").write("{ this is not json ")
    got3 = R._ine_main_from_hot()
    chk("OI 源损坏 → 返回空且不抛异常", got3 == {}, str(got3))
except Exception as e:
    chk("OI 源损坏 → 返回空且不抛异常", False, f"抛异常 {e}")
finally:
    R.CACHE = bak

# ---- 7) 非 INE 品种被正确排除 ----
print("=== 7. INE 白名单过滤 ===")
tmp_mix = os.path.join(tempfile.mkdtemp(), "mix.json")
try:
    json.dump({"sc": {"main": "SC2612"}, "lu": {"main": "LU2611"},
               "nr": {"main": "NR2611"}, "cu": {"main": "CU2611"}},
              open(tmp_mix, "w", encoding="utf-8"))
    R.CACHE = tmp_mix
    got4 = R._ine_main_from_hot()
    chk("只保留 sc（lu/nr/cu 被过滤）", set(got4.keys()) == {"sc"}, str(got4))
finally:
    R.CACHE = bak

# ---- 8) 记录字典缺 main 时退回 secondary ----
print("=== 8. 缺 main 时用 secondary ===")
tmp_nm = os.path.join(tempfile.mkdtemp(), "nomain.json")
try:
    json.dump({"sc": {"secondary": "SC2611"}}, open(tmp_nm, "w", encoding="utf-8"))
    R.CACHE = tmp_nm
    got5 = R._ine_main_from_hot()
    chk("缺 main → 退回 secondary", got5.get("sc") == "SC2611", str(got5))
finally:
    R.CACHE = bak

print()
print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    print("失败项:", FAIL)
    sys.exit(1)