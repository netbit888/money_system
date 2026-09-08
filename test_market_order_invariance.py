"""市场出清「顺序不变量」测试

背景
----
单趟扫描时，买家的支付能力是**当刻**读取的：

    a_pay = float(attrs[a_idx].get(pay_res, 0))

而资源市场按 `sorted(资源名)`（即 Unicode 码点序）依次开市。于是
「卖家场排在买家场之前」的个体才能成交 —— 资源改个名字、码点序一变，
生死就翻转。实测同一份 config 仅翻转顺序，100 回合后人口差 **14.1%**
（A 2466 / B 2119），各组结构也不同。

改为**多轮迭代出清**后，未满足的需求进入下一轮重试，资金到位后仍能成交，
顺序只影响成交先后、不再决定能否成交。

本测试锁定这个不变量：用 ASCII 前缀翻转市场处理顺序（ASCII < 所有汉字，
前缀直接决定 sorted 结果），两次 100 回合的演化必须一致。

运行：python test_market_order_invariance.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Simulation

PROJ = os.path.dirname(os.path.abspath(__file__))
ROUNDS = 100
_failures = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        _failures.append(name)


def load_config():
    with open(os.path.join(PROJ, "config/economy_persons.json"), encoding="utf-8") as f:
        persons = json.load(f)["persons"]
    with open(os.path.join(PROJ, "config/economy_defaults.json"), encoding="utf-8") as f:
        defaults = json.load(f)["defaultSettings"]
    return persons, defaults


def _r(m, n):
    return m.get(n, n)


def rename_person(p, m):
    p = json.loads(json.dumps(p))
    p["metabolism"]["res"] = _r(m, p["metabolism"]["res"])
    p["income"]["res"] = _r(m, p["income"]["res"])
    p["attrs"] = {_r(m, k): v for k, v in p["attrs"].items()}
    p["needs"] = [{"key": _r(m, n["key"]), "amount": n["amount"]} for n in p["needs"]]
    p["rules"] = [{"sell": _r(m, x["sell"]), "buy": _r(m, x["buy"]), "rate": x["rate"]}
                  for x in p.get("rules", [])]
    return p


def rename_defaults(d, m):
    d = json.loads(json.dumps(d))
    d["metabolism"]["res"] = _r(m, d["metabolism"]["res"])
    d["income"]["res"] = _r(m, d["income"]["res"])
    d["attrs"] = {_r(m, k): v for k, v in d.get("attrs", {}).items()}
    d["needs"] = [{"key": _r(m, n["key"]), "amount": n["amount"]} for n in d.get("needs", [])]
    d["rules"] = [{"sell": _r(m, x["sell"]), "buy": _r(m, x["buy"]), "rate": x["rate"]}
                  for x in d.get("rules", [])]
    d["perishable_resources"] = [_r(m, x) for x in d.get("perishable_resources", [])]
    if d.get("price_index_numeraire"):
        d["price_index_numeraire"] = _r(m, d["price_index_numeraire"])
    return d


def build(mapping, label):
    persons, defaults = load_config()
    d = tempfile.mkdtemp(prefix="mkt-%s-" % label)
    try:
        with open(os.path.join(d, "economy_persons.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "economy-persons", "version": 1,
                       "persons": [rename_person(p, mapping) for p in persons]},
                      f, ensure_ascii=False)
        with open(os.path.join(d, "economy_defaults.json"), "w", encoding="utf-8") as f:
            json.dump({"type": "economy-defaults", "version": 1,
                       "defaultSettings": rename_defaults(defaults, mapping),
                       "government": {"tax_rate": 0.1}}, f, ensure_ascii=False)
        sim = Simulation(history_dir=tempfile.mkdtemp(prefix="hist-%s-" % label))
        sim.reset_round(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return sim


def run(mapping, label):
    sim = build(mapping, label)
    series = []
    for _ in range(ROUNDS):
        if not sim.persons:
            break
        sim.next_round()
        s = sim.summary()
        series.append((s["round"], s["total"], dict(s["groups"])))
    return series


# A: 劳 → 钱 → 食（等同 config 原始的相对顺序）
# B: 食 → 钱 → 劳（完全翻转）
MAP_A = {"劳动力": "a劳动力", "钱": "b钱", "食物": "c食物"}
MAP_B = {"劳动力": "c劳动力", "钱": "b钱", "食物": "a食物"}

a = run(MAP_A, "A")
b = run(MAP_B, "B")

print("顺序 A（劳→钱→食） vs 顺序 B（食→钱→劳），各 %d 回合" % ROUNDS)
print("%6s | %8s | %8s" % ("回合", "A 人口", "B 人口"))
for i in range(0, min(len(a), len(b)), 20):
    print("%6d | %8d | %8d" % (a[i][0], a[i][1], b[i][1]))
print("%6s | %8d | %8d" % ("末", a[-1][1], b[-1][1]))
print()

check("两种顺序都跑满 %d 回合" % ROUNDS, len(a) == ROUNDS and len(b) == ROUNDS,
      "A=%d B=%d" % (len(a), len(b)))

fa, fb = a[-1][1], b[-1][1]
dev = abs(fb - fa) / fa * 100 if fa else 0.0
print("末回合人口 A=%d B=%d，偏差 %.2f%%" % (fa, fb, dev))
check("末回合人口偏差 < 1%（修复前为 14.1%）", dev < 1.0, "实际 %.2f%%" % dev)

# 逐回合最大相对偏差（杜绝中途分叉、末回合偶然重合）
max_dev = 0.0
for i in range(min(len(a), len(b))):
    ta = a[i][1]
    if ta:
        max_dev = max(max_dev, abs(b[i][1] - ta) / ta * 100)
print("逐回合最大相对偏差 %.2f%%" % max_dev)
check("全程偏差 < 1%", max_dev < 1.0, "实际 %.2f%%" % max_dev)

print("A 末回合分组:", a[-1][2])
print("B 末回合分组:", b[-1][2])
check("分组结构一致", a[-1][2] == b[-1][2])

# 关键定性断言：不能出现「某个群体因开市顺序而被饿死」
extinct = [g for g in a[-1][2] if b[-1][2].get(g, 0) == 0]
check("无群体因顺序差异而灭绝", not extinct, "%s" % extinct)

print()
if _failures:
    print("[FAIL] 失败 %d 项：%s" % (len(_failures), ", ".join(_failures)))
    raise SystemExit(1)
print("[OK] 全部通过")
