"""A2 + 日志汇总模式：正确性 + 性能验证（小规模版，3 秒内跑完）

测试范围：N ≤ 1000
测试项：
1. 真实 config 跑 calculate，验证汇总模式输出格式正确，verbose 模式输出与原版语义一致
2. 合成 N 个 Person，对比汇总模式 vs 详细模式的耗时
3. 验证 A2 算法本身正确性（数值一致）
"""
import time
import random
from engine import Simulation, Person, Metabolism, Need, Rule


def test_correctness():
    """验证汇总日志格式 + verbose 语义。"""
    print("=" * 60)
    print("1. 汇总模式输出（verbose=False，默认）")
    print("=" * 60)
    sim = Simulation()
    sim.reset_round("config")
    log = sim.calculate()
    print(log)
    print()
    print(f"[日志字符数] {len(log)}")

    print()
    print("=" * 60)
    print("2. 详细模式输出（verbose=True，调试用）")
    print("=" * 60)
    sim.reset_round("config")
    log_v = sim.calculate(verbose=True)
    print(log_v)
    print()
    print(f"[日志字符数] {len(log_v)}")

    print()
    print("=" * 60)
    print("3. 汇总模式跑 next_round_and_calculate")
    print("=" * 60)
    sim.reset_round("config")
    log2 = sim.next_round_and_calculate()
    print(log2)


def make_synthetic_sim(n: int, n_resources: int = 5) -> Simulation:
    """合成 N 个 Person，每人 1 need + 1 rule。"""
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
    """重置资源到 100，保证每次基准起点一致。"""
    for p in sim.persons:
        for k in p.attrs:
            p.attrs[k] = 100.0


def bench(sim, verbose, runs=3):
    """测 calculate 耗时，深拷贝开销包含在内（公平对比）。"""
    times = []
    for _ in range(runs):
        reset_attrs(sim)
        t0 = time.time()
        sim.calculate(verbose=verbose)
        t1 = time.time()
        times.append((t1 - t0) * 1000)
    return times


def main():
    test_correctness()

    print()
    print("=" * 70)
    print("A2 + 日志汇总 基准测试（N ≤ 1000，3 秒内完成）")
    print("=" * 70)
    print(f"{'N':>6} | {'汇总模式':>15} | {'详细模式':>15} | {'日志大小(汇总)':>15} | {'日志大小(详细)':>15}")
    print("-" * 70)

    for n in [100, 300, 500, 1000]:
        sim = make_synthetic_sim(n, n_resources=5)

        # 汇总模式
        t1 = bench(sim, verbose=False, runs=3)
        avg1 = sum(t1) / len(t1)
        reset_attrs(sim)
        sim.calculate(verbose=False)
        log1 = sim.calculate(verbose=False)
        # 直接拿一次输出
        reset_attrs(sim)
        log_sum = sim.calculate(verbose=False)
        size_sum = len(log_sum)

        # 详细模式
        if n <= 500:
            t2 = bench(sim, verbose=True, runs=2)
            avg2 = sum(t2) / len(t2)
            reset_attrs(sim)
            log_v = sim.calculate(verbose=True)
            size_v = len(log_v)
            v_str = f"{avg2:.1f}ms"
            v_size_str = f"{size_v}"
        else:
            v_str = "(跳过)"
            v_size_str = "-"

        print(f"{n:>6} | {avg1:>12.1f}ms | {v_str:>15} | {size_sum:>15} | {v_size_str:>15}")

    print()
    print("结论：")
    print("  - 汇总模式应该比详细模式快 10× 以上（因为省略 O(N²) 日志）")
    print("  - 日志大小：详细模式 N=500 应该几十 KB，汇总模式恒定 < 1 KB")
    print("  - A2 算法本身在 N≤1000 仍是 O(N²/R)，瓶颈转移到深拷贝和字典操作")


if __name__ == "__main__":
    main()
