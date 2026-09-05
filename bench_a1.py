"""A1 抚养索引优化 micro-benchmark

直接合成大量带 parentId 的 Person，单独测"抚养阶段"两种实现耗时对比：
- 旧版 O(N²)：每父一次全表扫描找子代
- 新版 O(N)：预建 parentId → children 字典

不依赖生态平衡，直接控制 N 和亲子关系密度。
"""
import time
import random
from engine import Simulation, Person, Metabolism, Need, Rule


def make_synthetic_sim(n: int, child_ratio: float = 0.5) -> Simulation:
    """合成 N 个 Person，其中 child_ratio 比例有 parentId 指向前面随机 parent。"""
    sim = Simulation()
    sim.persons = []
    sim.next_id = 1
    sim.round = 1

    random.seed(42)
    base_parents = max(10, n // 10)  # 10% 作为初始 parent 池

    for i in range(n):
        parent_id = None
        if i >= base_parents and random.random() < child_ratio:
            parent_id = random.randint(1, base_parents)
        p = Person(
            id=i + 1,
            parentId=parent_id,
            name=f"打工人#{i+1}" if parent_id is None else f"子代#{i+1}",
            metabolism=Metabolism(res="食物", amount=5.0),
            income=Metabolism(res="劳动力", amount=10.0),
            canReproduce=(parent_id is None),
            attrs={"食物": 100.0, "劳动力": 50.0, "钱": 30.0},
            needs=[Need("食物", 5.0)],
            rules=[Rule(sell="劳动力", buy="钱", rate=2.0)],
        )
        sim.persons.append(p)
    return sim


def step_nurture_old(sim: Simulation) -> None:
    """旧版 O(N²)：每父一次全表扫描找子代。"""
    transfers = []
    for parent in sim.persons:
        children = [c for c in sim.persons if c.parentId == parent.id]
        if not children:
            continue
        pm = sim._norm_person_metabolism(parent)
        remaining = float(parent.attrs.get(pm.res, 0))
        for child in children:
            cm = sim._norm_person_metabolism(child)
            need = cm.amount
            if remaining >= need:
                parent.attrs[pm.res] = float(parent.attrs.get(pm.res, 0)) - need
                child.attrs[cm.res] = float(child.attrs.get(cm.res, 0)) + need
                remaining -= need
                transfers.append((parent, child, cm.res, need, False))
            elif remaining > 0:
                partial = remaining
                parent.attrs[pm.res] = float(parent.attrs.get(pm.res, 0)) - partial
                child.attrs[cm.res] = float(child.attrs.get(cm.res, 0)) + partial
                remaining = 0
                transfers.append((parent, child, cm.res, partial, True))


def step_nurture_new(sim: Simulation) -> None:
    """新版 O(N)：预建 parentId → children 字典。"""
    children_by_parent = {}
    for c in sim.persons:
        if c.parentId is not None:
            children_by_parent.setdefault(c.parentId, []).append(c)
    transfers = []
    for parent in sim.persons:
        children = children_by_parent.get(parent.id)
        if not children:
            continue
        pm = sim._norm_person_metabolism(parent)
        remaining = float(parent.attrs.get(pm.res, 0))
        for child in children:
            cm = sim._norm_person_metabolism(child)
            need = cm.amount
            if remaining >= need:
                parent.attrs[pm.res] = float(parent.attrs.get(pm.res, 0)) - need
                child.attrs[cm.res] = float(child.attrs.get(cm.res, 0)) + need
                remaining -= need
                transfers.append((parent, child, cm.res, need, False))
            elif remaining > 0:
                partial = remaining
                parent.attrs[pm.res] = float(parent.attrs.get(pm.res, 0)) - partial
                child.attrs[cm.res] = float(child.attrs.get(cm.res, 0)) + partial
                remaining = 0
                transfers.append((parent, child, cm.res, partial, True))


def bench(fn, sim, runs=3):
    times = []
    for _ in range(runs):
        # 重置 attrs 避免累计影响
        for p in sim.persons:
            p.attrs = {"食物": 100.0, "劳动力": 50.0, "钱": 30.0}
        t0 = time.time()
        fn(sim)
        t1 = time.time()
        times.append((t1 - t0) * 1000)
    return times


def main():
    print("=" * 70)
    print("A1 抚养索引优化 micro-benchmark")
    print("=" * 70)
    print(f"{'N':>8} | {'含亲子':>8} | {'旧版 O(N²)':>15} | {'新版 O(N)':>15} | {'加速比':>8}")
    print("-" * 70)

    for n in [1000, 5000, 10000, 30000, 100000]:
        sim = make_synthetic_sim(n)
        with_parent = sum(1 for p in sim.persons if p.parentId is not None)

        # 旧版只测小规模（大 N 太慢）
        if n <= 10000:
            old_times = bench(step_nurture_old, sim, runs=3)
            old_avg = sum(old_times) / len(old_times)
            old_str = f"{old_avg:.1f}ms"
        else:
            old_str = "(太慢跳过)"

        new_times = bench(step_nurture_new, sim, runs=3)
        new_avg = sum(new_times) / len(new_times)
        new_str = f"{new_avg:.1f}ms"

        if n <= 10000:
            speedup = old_avg / new_avg if new_avg > 0 else 0
            speed_str = f"{speedup:.1f}x"
        else:
            speed_str = "-"

        print(f"{n:>8} | {with_parent:>8} | {old_str:>15} | {new_str:>15} | {speed_str:>8}")

    print()
    print("结论：")
    print("  - 新版 O(N) 应该呈线性增长")
    print("  - 旧版 O(N²) 在 N=10000 应该明显慢（秒级）")


if __name__ == "__main__":
    main()
