"""冒烟入口：python -m engine

等价于旧版 `python engine.py` 的冒烟测试：加载 config（若有），
跑一回合 + 一次交换，打印前后摘要。
"""
from __future__ import annotations

import os

from .config_io import import_defaults, import_persons
from .core import Simulation


def main() -> None:
    sim = Simulation()
    if os.path.exists("config/economy_defaults.json"):
        sim.defaults = import_defaults("config/economy_defaults.json")
    if os.path.exists("config/economy_persons.json"):
        sim.persons = import_persons("config/economy_persons.json", sim.defaults)
        sim.next_id = max((p.id for p in sim.persons), default=0) + 1
    else:
        sim.add_person("测试个体A")
        sim.add_person("测试个体B")

    print("【初始摘要】", sim.summary())
    print()
    print(sim.next_round())
    print()
    print("═" * 40)
    print()
    print(sim.calculate())
    print()
    print("【回合后摘要】", sim.summary())


if __name__ == "__main__":
    main()
