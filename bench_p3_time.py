"""P3 时间基准：测单回合耗时（纯交换 vs 繁殖全路径），判断是否达标。"""
import time

from test_p3_parity import build


def time_round(n, reproduce, rounds=5):
    sim = build(n, reproduce=reproduce)
    sim.next_round()  # 预热
    t0 = time.time()
    for _ in range(rounds):
        sim.next_round()
    dt = (time.time() - t0) / rounds
    return dt * 1000.0  # ms/回合


if __name__ == "__main__":
    print("纯交换场景（reproduce=False）：")
    for n in [10000, 30000, 100000]:
        t = time_round(n, reproduce=False)
        print(f"  N={n:>7}  {t:8.1f} ms/回合")

    print("繁殖全路径（reproduce=True）：")
    for n in [10000, 30000]:
        t = time_round(n, reproduce=True, rounds=3)
        print(f"  N={n:>7}  {t:8.1f} ms/回合")
