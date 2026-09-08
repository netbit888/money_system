"""P3 引擎数据布局：摘要基线 + 复杂度守卫 + 内存验收。

对固定种子/参数的 N 回合演化，对每个体 (id, attrs, rates) 取 SHA256，
作为 3.1~3.6 全部性能改动的「逐位一致」验收基线。

关键设计：snapshot 依赖 _person_to_dict（序列化契约），
3.3/3.4 改动内部表示（SoA / 整数索引）后，只要该契约输出不变，摘要即逐位一致。

运行：python test_p3_parity.py
"""
import gc
import hashlib
import random
import tempfile
import time
import tracemalloc

from engine import Simulation, Person, Metabolism, Need, Rule, _person_to_dict

RES = ["食物", "钱", "劳动力"]

# 基线：N=600 / 20 回合 / seed=42 / α=0.3（覆盖繁殖/死亡/抚养/交换/调价全路径）
#
# 摘要沿革：
#   dd4904da…  P3 基线（单趟扫描出清）
#   72b9543f…  多轮迭代出清后 —— **有意的行为变更**。
#              旧摘要受「市场出清顺序敏感」污染：资源名的 Unicode 码点序会
#              决定谁能成交。修复后顺序只影响成交先后，不再决定能否成交
#              （顺序不变量由 test_market_order_invariance.py 守卫）
# 内存基线（N=10000 单回合峰值分配）：21.75 MB → 目标 < 6 MB
BASELINE_DIGEST = "72b9543f73a08b8f964d1715d2d30f331fb996b173058ad6fecc7f229050e4c5"

_failures = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        _failures.append(name)


def build(n, seed=42, alpha=0.3, reproduce=True):
    """确定性构造：不依赖全局 random，保证可复现。隔离持久化到临时目录。

    三资源循环经济：每个体卖出「收入资源」换取「代谢资源」，收入 > 代谢，
    参考 config/economy_persons.json 的稳定闭环（thr=12 / inherit=3 / met0=60），
    覆盖繁殖/死亡/抚养/交换/调价全路径且 20 回合不灭绝。
    """
    sim = Simulation(history_dir=tempfile.mkdtemp(prefix="econ-p3-"))
    sim.persons = []
    sim.next_id = 1
    sim.round = 0
    sim.defaults.adaptive_pricing = True
    sim.defaults.price_adjust_alpha = alpha
    rnd = random.Random(seed)
    for i in range(n):
        met = RES[i % 3]            # 代谢资源（生存必需）
        inc = RES[(i + 1) % 3]      # 收入资源（产出并卖出换代谢资源）
        sim.persons.append(Person(
            id=i + 1, parentId=None, name="G%d#%d" % (i % 8 + 1, i + 1),
            metabolism=Metabolism(met, 1.0),
            income=Metabolism(inc, 5.0),
            canReproduce=reproduce,
            reproThresholdMult=12.0,
            reproInheritMult=3.0,
            reproInheritRatio=0.5,
            attrs={met: 60.0, inc: 25.0, RES[(i + 2) % 3]: 25.0},
            needs=[Need(met, 2.0)],
            rules=[Rule(inc, met, float(rnd.randint(1, 5)))],
        ))
    sim.next_id = n + 1
    return sim


def snapshot(sim):
    """对 (id, attrs, rates) 的规范逻辑视图取摘要。"""
    parts = []
    for p in sorted(sim.persons, key=lambda x: x.id):
        d = _person_to_dict(p)
        attrs = "".join("%s=%.12f;" % (k, d["attrs"][k]) for k in sorted(d["attrs"]))
        rules = ";".join(
            "%d:%s:%s:%.12f" % (i, r["sell"], r["buy"], r["rate"])
            for i, r in enumerate(d["rules"])
        )
        parts.append("%d|%s|%s" % (d["id"], attrs, rules))
    return "|".join(parts)


def run_rounds(n, rounds=20, seed=42, alpha=0.3, reproduce=True):
    sim = build(n, seed=seed, alpha=alpha, reproduce=reproduce)
    h = hashlib.sha256()
    t0 = time.time()
    for _ in range(rounds):
        sim.next_round()
        h.update(("%d;%s" % (sim.round, snapshot(sim))).encode())
    return h.hexdigest(), (time.time() - t0) * 1000.0, len(sim.persons)


def measure_peak(n, reproduce=True):
    """tracemalloc 测单回合峰值分配（字节）。"""
    sim = build(n, reproduce=reproduce)
    sim.next_round()  # 预热，让惰性结构/缓存就位
    gc.collect()
    tracemalloc.start()
    sim.next_round()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def main():
    # 1. 摘要基线
    digest, elapsed, survivors = run_rounds(600)
    print(f"摘要 = {digest}")
    print(f"耗时 = {elapsed:.1f} ms / 20 回合，存活 {survivors}")
    if BASELINE_DIGEST != "PENDING":
        check("与改动前逐位一致", digest == BASELINE_DIGEST,
              f"期望 {BASELINE_DIGEST[:16]}… 实际 {digest[:16]}…")
    else:
        print("  [INFO] 尚未填入基线，请把上面 digest 写入 BASELINE_DIGEST")

    # 2. 复杂度守卫
    _, t_small, _ = run_rounds(300, rounds=10)
    _, t_large, _ = run_rounds(900, rounds=10)
    ratio = t_large / t_small if t_small > 0 else 999.0
    print(f"\n复杂度：N=300 {t_small:.1f}ms  N=900 {t_large:.1f}ms  比值 {ratio:.2f}x（线性≈3x，阈值6x）")
    check("规模扩大 3 倍未呈平方增长", ratio < 6.0, f"实际 {ratio:.2f}x")

    # 3. 内存：单回合峰值分配（纯交换场景对齐方案 < 4 MB 目标；繁殖全路径更重，仅参考）
    peak_exchange = measure_peak(10000, reproduce=False)
    print(f"\n内存（纯交换）：N=10000 单回合峰值 {peak_exchange/1024/1024:.2f} MB（目标 < 6 MB）")
    check("纯交换单回合峰值 < 6 MB", peak_exchange < 6 * 1024 * 1024,
          f"实际 {peak_exchange/1024/1024:.2f} MB")
    peak_full = measure_peak(10000, reproduce=True)
    print(f"内存（繁殖全路径）：N=10000 单回合峰值 {peak_full/1024/1024:.2f} MB")

    print()
    if _failures:
        print(f"[FAIL] 失败 {len(_failures)} 项：{', '.join(_failures)}")
        raise SystemExit(1)
    print("[OK] 全部通过")


if __name__ == "__main__":
    main()
