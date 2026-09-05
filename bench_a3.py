"""A3 验证：正确性 + 性能"""
import time
import random
from engine import Simulation, Person, Metabolism, Need, Rule


def test_correctness():
    """验证 A3 输出和原版语义一致。"""
    print("=" * 60)
    print("1. 真实 config 汇总模式")
    print("=" * 60)
    sim = Simulation()
    sim.reset_round("config")
    print(sim.calculate())
    print()
    print("=" * 60)
    print("2. 真实 config 详细模式（A3 格式）")
    print("=" * 60)
    sim.reset_round("config")
    print(sim.calculate(verbose=True))
    print()
    print("=" * 60)
    print("3. 跑 next_round_and_calculate")
    print("=" * 60)
    sim.reset_round("config")
    print(sim.next_round_and_calculate())


def make_synthetic_sim(n: int, n_resources: int = 5) -> Simulation:
    sim = Simulation()
    sim.persons = []
    sim.next_id = 1
    sim.round = 1
    random.seed(42)
    resources = ["食物", "钱", "劳动力", "电力", "原料"][:n_resources]
    for i in range(n):
        sell = random.choice(resources)
        buy = random.choice([r for r in resources if r != sell])
        p = Person(
            id=i + 1, parentId=None, name=f"个体#{i+1}",
            metabolism=Metabolism(res=sell, amount=5.0),
            income=Metabolism(res=buy, amount=10.0),
            canReproduce=False,
            attrs={r: 100.0 for r in resources},
            needs=[Need(buy, 5.0)],
            rules=[Rule(sell=sell, buy=buy, rate=float(random.randint(1, 5)))],
        )
        sim.persons.append(p)
    return sim


def reset_attrs(sim):
    for p in sim.persons:
        for k in p.attrs:
            p.attrs[k] = 100.0


def bench(sim, runs=3):
    times = []
    for _ in range(runs):
        reset_attrs(sim)
        t0 = time.time()
        sim.calculate()
        t1 = time.time()
        times.append((t1 - t0) * 1000)
    return times


def main():
    test_correctness()
    print()
    print("=" * 70)
    print("A3 性能测试")
    print("=" * 70)
    print(f"{'N':>8} | {'A3 耗时':>15} | {'A2 耗时(之前)':>15} | {'加速比':>10}")
    print("-" * 70)
    # A2 之前测试结果（汇总模式）：
    a2_results = {100: 5.5, 300: 24.2, 500: 64.2, 1000: 219.4}
    for n in [100, 300, 500, 1000, 5000, 10000, 50000]:
        sim = make_synthetic_sim(n, n_resources=5)
        t = bench(sim, runs=3 if n <= 1000 else 2)
        avg = sum(t) / len(t)
        a2 = a2_results.get(n, "?")
        if isinstance(a2, float):
            speedup = f"{a2/avg:.1f}x"
        else:
            speedup = "-"
        print(f"{n:>8} | {avg:>12.1f}ms | {str(a2):>15} | {speedup:>10}")


if __name__ == "__main__":
    main()
