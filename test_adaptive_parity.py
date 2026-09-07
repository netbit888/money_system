"""P0 回归测试：自适应调价索引化 + 基础设置字段对齐

覆盖三件事：
  1. 行为等价：调价结果必须与「循环内线性查找 find()」的旧实现逐位一致
     （基线摘要取自改造前的代码，证明本次是纯性能改动、零行为变化）
  2. 字段对齐：export_defaults / import_defaults 往返不丢
     adaptive_pricing / price_adjust_alpha / weanMinRounds / weanMaxRounds
  3. 复杂度守卫：规模翻 3 倍，耗时不应呈平方增长（防止 O(N²) 回归）

运行：python test_adaptive_parity.py
"""
import hashlib
import os
import random
import tempfile
import time

from engine import Simulation, Person, Metabolism, Need, Rule

RES = ["食物", "钱", "劳动力"]

# 改造前（循环内 self.find(pid)）实测基线：N=2000 / 20 回合 / seed=42 / α=0.3
BASELINE_DIGEST = "33a3495739ed6b8edbfc5531fd59919cff8039c77e543ee9f1cc263b20e6d37c"

_failures = []


def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  " + detail) if detail else ""))
    if not ok:
        _failures.append(name)


def build(n, seed=42, alpha=0.3):
    """确定性构造：不依赖全局 random，保证可复现。"""
    # 隔离持久化：测试不得读写真实的 snapshots/
    sim = Simulation(history_dir=tempfile.mkdtemp(prefix="econ-test-"))
    sim.persons = []
    sim.next_id = 1
    sim.round = 0
    sim.defaults.adaptive_pricing = True
    sim.defaults.price_adjust_alpha = alpha
    rnd = random.Random(seed)
    for i in range(n):
        sell = RES[i % len(RES)]
        buy = RES[(i + 1) % len(RES)]
        sim.persons.append(Person(
            id=i + 1, parentId=None, name="G%d#%d" % (i % 8 + 1, i + 1),
            metabolism=Metabolism(sell, 1.0),
            income=Metabolism(buy, 5.0),
            canReproduce=False,
            attrs={r: 100.0 for r in RES},
            needs=[Need(buy, 2.0)],
            rules=[Rule(sell, buy, float(rnd.randint(1, 5)))],
        ))
    sim.next_id = n + 1
    return sim


def snapshot(sim):
    parts = []
    for p in sorted(sim.persons, key=lambda x: x.id):
        for idx, r in enumerate(p.rules):
            parts.append("%d:%d:%.12f" % (p.id, idx, r.rate))
    return "|".join(parts)


def run_rounds(n, rounds=20, seed=42, alpha=0.3):
    sim = build(n, seed=seed, alpha=alpha)
    h = hashlib.sha256()
    t0 = time.time()
    for _ in range(rounds):
        sim.next_round()
        h.update(("%d;%s" % (sim.round, snapshot(sim))).encode())
    return h.hexdigest(), (time.time() - t0) * 1000.0, len(sim.persons)


# ============ 1. 行为等价 ============
print("=== 1. 调价结果等价性（对比改造前基线）===")
digest, elapsed, survivors = run_rounds(2000)
print(f"  摘要 = {digest}")
print(f"  耗时 = {elapsed:.1f} ms / 20 回合，存活 {survivors}")
check("与改造前逐位一致", digest == BASELINE_DIGEST,
      f"期望 {BASELINE_DIGEST[:16]}… 实际 {digest[:16]}…")

# 换一个种子/强度再跑一次，确保不是偶然对齐
d2, _, _ = run_rounds(2000, rounds=10, seed=7, alpha=0.5)
check("不同种子/α 下可复现", d2 == run_rounds(2000, rounds=10, seed=7, alpha=0.5)[0])
check("不同参数产生不同轨迹", d2 != BASELINE_DIGEST)

# ============ 2. 字段对齐 ============
print()
print("=== 2. 基础设置导出/导入往返不丢字段 ===")
sim = Simulation(history_dir=tempfile.mkdtemp(prefix="econ-test-"))
sim.defaults.adaptive_pricing = True
sim.defaults.price_adjust_alpha = 0.42
sim.defaults.weanMinRounds = 4
sim.defaults.weanMaxRounds = 11
sim.defaults.price_index_numeraire = "钱"

fd, tmp = tempfile.mkstemp(suffix=".json")
os.close(fd)
try:
    from engine import export_defaults, import_defaults
    export_defaults(tmp, sim.defaults, sim.government)
    loaded = import_defaults(tmp)
    check("adaptive_pricing 保留", loaded.adaptive_pricing is True,
          f"实际 {loaded.adaptive_pricing}")
    check("price_adjust_alpha 保留", abs(loaded.price_adjust_alpha - 0.42) < 1e-9,
          f"实际 {loaded.price_adjust_alpha}")
    check("weanMinRounds 保留", loaded.weanMinRounds == 4, f"实际 {loaded.weanMinRounds}")
    check("weanMaxRounds 保留", loaded.weanMaxRounds == 11, f"实际 {loaded.weanMaxRounds}")
    check("price_index_numeraire 保留", loaded.price_index_numeraire == "钱")
finally:
    if os.path.exists(tmp):
        os.remove(tmp)

# 缺失字段时回落到默认（旧配置文件兼容）
fd, tmp2 = tempfile.mkstemp(suffix=".json")
os.close(fd)
try:
    with open(tmp2, "w", encoding="utf-8") as f:
        f.write('{"defaultSettings": {"metabolism": {"res": "食物", "amount": 1}}}')
    legacy = import_defaults(tmp2)
    check("旧配置回落默认值",
          legacy.adaptive_pricing is False and legacy.weanMinRounds == 3
          and legacy.weanMaxRounds == 8 and abs(legacy.price_adjust_alpha - 0.1) < 1e-9)
finally:
    if os.path.exists(tmp2):
        os.remove(tmp2)

# ============ 3. 复杂度守卫 ============
print()
print("=== 3. 复杂度守卫（防止 O(N²) 回归）===")
_, t_small, _ = run_rounds(2000, rounds=10)
_, t_large, _ = run_rounds(6000, rounds=10)
ratio = t_large / t_small if t_small > 0 else 999.0
print(f"  N=2000: {t_small:.1f} ms   N=6000: {t_large:.1f} ms   比值 {ratio:.2f}x")
print("  线性应为 ~3x；若为平方则 ~9x（阈值放宽到 6x 以吸收噪声）")
check("规模扩大 3 倍未呈平方增长", ratio < 6.0, f"实际 {ratio:.2f}x")

print()
if _failures:
    print(f"❌ 失败 {len(_failures)} 项：{', '.join(_failures)}")
    raise SystemExit(1)
print("✅ 全部通过")
