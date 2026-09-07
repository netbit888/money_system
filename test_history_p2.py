"""P2 回归测试：历史分层降采样 + 追加写

覆盖四件事：
  1. 聚合正确性：计数类求和 / 比率类取均值 / 存量类取均值（按字段语义分派）
  2. 规模有界：长跑后内存行数与磁盘字节都不随回合数线性增长
  3. 重载一致性：重新加载后回合严格升序、无重复、末回合一致
  4. 路径隔离：history_dir 注入临时目录，不污染项目 snapshots/

运行：python test_history_p2.py
"""
import json
import os
import shutil
import tempfile
import time

from engine import (
    Simulation, Person, Metabolism, Need, Rule,
    aggregate_rows, _FOLD, _L0_CAPACITY,
)

_failures = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        _failures.append(name)


def make_rows(n, start_round=1, born=1, dead=0, gini=0.30, total=100):
    rows = []
    for i in range(n):
        rows.append({
            "round": start_round + i,
            "span": 1,
            "total": total,
            "groups": {"A": 50, "B": 50},
            "resource_totals": {"食物": 10.0, "钱": 20.0},
            "metrics": {
                "price_index": 2.0, "volume": 5.0, "met_rate": 0.8,
                "gini": gini, "hhi_wealth": 0.5, "hhi_pop": 0.5,
                "unemployment": 0.1, "born": born, "dead": dead,
            },
        })
    return rows


# ============ 1. 聚合正确性 ============
print("=== 1. 聚合按字段语义分派 ===")
rows = make_rows(_FOLD, born=2, dead=1, gini=0.30, total=100)
agg = aggregate_rows(rows)
m = agg["metrics"]
check("计数类求和 born", abs(m["born"] - 2 * _FOLD) < 1e-9, "实际 %s 期望 %s" % (m["born"], 2 * _FOLD))
check("计数类求和 dead", abs(m["dead"] - 1 * _FOLD) < 1e-9, "实际 %s" % m["dead"])
check("计数类求和 volume", abs(m["volume"] - 5.0 * _FOLD) < 1e-9, "实际 %s" % m["volume"])
check("比率类取均值 gini", abs(m["gini"] - 0.30) < 1e-9, "实际 %s" % m["gini"])
check("比率类取均值 unemployment", abs(m["unemployment"] - 0.1) < 1e-9)
check("存量类取均值 total", abs(agg["total"] - 100) < 1e-9, "实际 %s" % agg["total"])
check("分组取均值", abs(agg["groups"]["A"] - 50) < 1e-9)
check("资源取均值", abs(agg["resource_totals"]["钱"] - 20.0) < 1e-9)
check("span = 折叠行数", agg["span"] == _FOLD, "实际 %s" % agg["span"])
check("round = 区间起点", agg["round"] == rows[0]["round"])

# 若误用「一律取平均」，born 会变成 2 而不是 20 —— 这是静默错误，必须挡住
check("未退化为全字段取平均", abs(m["born"] - 2.0) > 1e-9,
      "born=%s（若为 2.0 说明求和规则失效）" % m["born"])

# 混合值
mixed = make_rows(4, born=0, dead=0, gini=0.2, total=10)
mixed[1]["metrics"]["gini"] = 0.4
mixed[1]["metrics"]["born"] = 3
mixed[2]["metrics"]["gini"] = None
a2 = aggregate_rows(mixed)
# None 表示「该回合无此数据」，应跳过后再平均：(0.2 + 0.4 + 0.2) / 3
check("比率类取均值（跳过 None）", abs(a2["metrics"]["gini"] - 0.8 / 3) < 1e-9,
      "实际 %.6f 期望 %.6f" % (a2["metrics"]["gini"], 0.8 / 3))
check("计数类求和（含 0）", abs(a2["metrics"]["born"] - 3) < 1e-9, "实际 %s" % a2["metrics"]["born"])

# ============ 2. 长跑：规模有界 ============
print("\n=== 2. 长跑后规模有界 ===")
tmp = tempfile.mkdtemp(prefix="econ-p2-")
ROUNDS = 5000
try:
    sim = Simulation(history_dir=tmp)
    for i in range(20):
        sell = ["食物", "钱", "劳动力"][i % 3]
        buy = ["钱", "劳动力", "食物"][i % 3]
        sim.persons.append(Person(
            id=i + 1, parentId=None, name="G%d#%d" % (i % 4 + 1, i + 1),
            # 代谢置 0：本测试只关心持久化，避免个体死亡干扰回合数
            metabolism=Metabolism(sell, 0.0), income=Metabolism(buy, 5.0),
            canReproduce=False, attrs={r: 100.0 for r in ["食物", "钱", "劳动力"]},
            needs=[Need(buy, 2.0)], rules=[Rule(sell, buy, 1.0)],
        ))
    sim.next_id = 21

    # 统计持久化真实耗时
    persist_ms = [0.0]
    _orig_persist = sim._maybe_persist

    def _timed_persist():
        t = time.time()
        _orig_persist()
        persist_ms[0] += (time.time() - t) * 1000

    sim._maybe_persist = _timed_persist

    t0 = time.time()
    for _ in range(ROUNDS):
        sim.next_round()
    el = (time.time() - t0) * 1000

    n_rows = len(sim.history)
    lvl_sizes = [len(x) for x in sim._levels]
    print("  %d 回合：总行数 %d，各层 %s，耗时 %.0f ms" % (ROUNDS, n_rows, lvl_sizes, el))
    check("总行数远小于回合数（已降采样）", n_rows < ROUNDS * 0.8, "%d < %d" % (n_rows, ROUNDS * 0.8))
    check("L0 不超容量", lvl_sizes[0] <= _L0_CAPACITY + _FOLD, "L0=%d" % lvl_sizes[0])
    check("无写盘错误", sim.persist_errors == 0, "persist_errors=%d" % sim.persist_errors)

    disk = sum(os.path.getsize(os.path.join(tmp, f)) for f in os.listdir(tmp)
               if os.path.isfile(os.path.join(tmp, f)))
    print("  磁盘占用 %.1f KB" % (disk / 1024.0))
    check("磁盘占用有界（< 2MB）", disk < 2 * 1024 * 1024, "%.1f KB" % (disk / 1024.0))

    # 对照：旧方案「每 10 回合全量重写整个数组」的推算成本
    probe = sim.history[-1:] * 2000
    t0 = time.time()
    json.dumps(probe, ensure_ascii=False)
    per_row_ms = (time.time() - t0) * 1000 / 2000.0
    # 旧方案总写入行数 = Σ(10k) for k=1..ROUNDS/10 ≈ ROUNDS²/20
    old_total_ms = per_row_ms * (ROUNDS * ROUNDS / 20.0)
    print("  持久化实际耗时 %.0f ms（占整轮 %.1f%%）" % (persist_ms[0], persist_ms[0] / el * 100))
    print("  旧方案推算：每 10 回合全量重写 → 累计写入 %d 行 ≈ %.1f s"
          % (ROUNDS * ROUNDS // 20, old_total_ms / 1000.0))
    check("持久化耗时远低于旧方案推算", persist_ms[0] < old_total_ms / 10,
          "%.0f ms vs %.0f ms" % (persist_ms[0], old_total_ms))
    # N=20 的微型场景下单回合仿真开销极小，占比会被放大；
    # 用「单回合持久化成本」这个与规模无关的指标更合适
    per_round = persist_ms[0] / ROUNDS
    print("  单回合持久化成本 %.3f ms" % per_round)
    check("单回合持久化成本 < 0.5 ms", per_round < 0.5, "%.3f ms" % per_round)

    # ============ 3. 重载一致性 ============
    print("\n=== 3. 重新加载后一致 ===")
    # 先让内存与磁盘完全对齐（归档落盘 + L0 压缩）
    sim._write_archive()
    sim._compact_l0()
    sim._folds_since_archive_write = 0
    before_rounds = [h["round"] for h in sim.history]
    before_len = len(before_rounds)

    sim2 = Simulation(history_dir=tmp)
    after_rounds = [h["round"] for h in sim2.history]
    check("行数一致", len(after_rounds) == before_len, "%d vs %d" % (len(after_rounds), before_len))
    check("末回合一致", after_rounds[-1] == before_rounds[-1],
          "%s vs %s" % (after_rounds[-1], before_rounds[-1]))
    check("回合严格升序", all(after_rounds[i] < after_rounds[i + 1]
                             for i in range(len(after_rounds) - 1)))
    check("无重复回合", len(set(after_rounds)) == len(after_rounds),
          "唯一 %d / 总数 %d" % (len(set(after_rounds)), len(after_rounds)))
    check("重载无读盘错误", sim2.persist_errors == 0)

    # get_history 切片跨层仍正确
    seg = sim2.get_history(ROUNDS - 50, ROUNDS)
    check("get_history 区间可用", len(seg) > 0 and seg[0]["round"] >= ROUNDS - 50,
          "返回 %d 行" % len(seg))

    # ============ 4. clear_history ============
    print("\n=== 4. clear_history 清理干净 ===")
    sim2.clear_history()
    check("内存清空", len(sim2.history) == 0)
    left = [f for f in os.listdir(tmp) if f.startswith("history-")]
    check("历史文件已删除", left == [], "残留 %s" % left)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ============ 5. 路径隔离 ============
print("\n=== 5. 路径隔离（不污染项目 snapshots/）===")
proj_snapshots = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
tmp2 = tempfile.mkdtemp(prefix="econ-p2-")
try:
    before = set(os.listdir(proj_snapshots)) if os.path.isdir(proj_snapshots) else set()
    s = Simulation(history_dir=tmp2)
    s.persons.append(Person(
    id=1, parentId=None, name="X", metabolism=Metabolism("食物", 0.0),
        income=Metabolism("钱", 5.0), canReproduce=False,
        attrs={"食物": 100.0, "钱": 100.0}, needs=[Need("钱", 1.0)],
        rules=[Rule("食物", "钱", 1.0)]))
    s.next_id = 2
    for _ in range(30):
        s.next_round()
    after = set(os.listdir(proj_snapshots)) if os.path.isdir(proj_snapshots) else set()
    check("项目 snapshots/ 未新增文件", after == before, "新增 %s" % (after - before))
    check("临时目录已写入", len(os.listdir(tmp2)) > 0, "%s" % os.listdir(tmp2))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print()
if _failures:
    print("❌ 失败 %d 项：%s" % (len(_failures), ", ".join(_failures)))
    raise SystemExit(1)
print("✅ 全部通过")
