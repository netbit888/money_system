"""P3 3.1 验证：资源总量 delta 账本 == before/after 快照差值。

用「外部观察者」方式：在 next_round 前后自行汇总全部 attrs，
与引擎内部累加的 sim._last_res_delta 逐资源对比（浮点近似）。
覆盖繁殖/死亡/抚养/交换/税收/易腐全路径。
"""
import random
import tempfile

from engine import Simulation, Person, Metabolism, Need, Rule

RES = ["食物", "钱", "劳动力"]


def build(n, seed=42, alpha=0.3, perishable=False):
    sim = Simulation(history_dir=tempfile.mkdtemp(prefix="econ-p3d-"))
    sim.persons = []
    sim.next_id = 1
    sim.round = 0
    sim.defaults.adaptive_pricing = True
    sim.defaults.price_adjust_alpha = alpha
    if perishable:
        sim.defaults.perishable_resources = ["劳动力"]
    rnd = random.Random(seed)
    for i in range(n):
        met = RES[i % 3]
        inc = RES[(i + 1) % 3]
        sim.persons.append(Person(
            id=i + 1, parentId=None, name="G%d#%d" % (i % 8 + 1, i + 1),
            metabolism=Metabolism(met, 1.0),
            income=Metabolism(inc, 5.0),
            canReproduce=True,
            reproThresholdMult=12.0,
            reproInheritMult=3.0,
            reproInheritRatio=0.5,
            attrs={met: 60.0, inc: 25.0, RES[(i + 2) % 3]: 25.0},
            needs=[Need(met, 2.0)],
            rules=[Rule(inc, met, float(rnd.randint(1, 5)))],
        ))
    sim.next_id = n + 1
    return sim


def total_attrs(sim):
    total = {}
    for p in sim.persons:
        for k, v in p.attrs.items():
            total[k] = total.get(k, 0.0) + float(v)
    return total


def run(perishable):
    sim = build(200, perishable=perishable)
    worst = 0.0
    for _ in range(30):
        bt = total_attrs(sim)
        sim.next_round()
        at = total_attrs(sim)
        expected = {k: at.get(k, 0.0) - bt.get(k, 0.0) for k in set(bt) | set(at)}
        actual = sim._last_res_delta
        for k in set(expected) | set(actual):
            e = expected.get(k, 0.0)
            a = actual.get(k, 0.0)
            err = abs(e - a)
            worst = max(worst, err)
            if err > 1e-6:
                return False, k, e, a
    return True, None, worst, 0.0


if __name__ == "__main__":
    for perishable in (False, True):
        ok, k, e, a = run(perishable)
        tag = f"perishable={perishable}"
        if ok:
            print(f"[PASS] {tag}: delta 账本与 before/after 差值一致（最大误差 {a:.2e}）")
        else:
            print(f"[FAIL] {tag}: 资源 {k} 账本 {a} != 差值 {e}")
    print("done")
