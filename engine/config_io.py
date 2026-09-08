"""F 层：个体与基础设置的 JSON 序列化 / 导入导出。

职责：Person ⇄ dict 的互转、config 目录的读写（economy_defaults.json /
economy_persons.json）、以及导出的文件包装（type/version/exportedAt）。

依赖：仅 A 层（model）。不含仿真逻辑，也不碰历史存储。
被调用方：core.Simulation.reset_round / add_person，以及 app.py 的导入导出接口。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .model import (
    DefaultSettings, Government, Metabolism, Need, Person, Rule, norm_ask, norm_metabolism,
)


# ===================== Person ⇄ dict =====================
def _person_to_dict(p: Person) -> dict:
    return {
        "id": p.id,
        "parentId": p.parentId,
        "name": p.name,
        "metabolism": {"res": p.metabolism.res, "amount": p.metabolism.amount},
        "income": {"res": p.income.res, "amount": p.income.amount},
        "canReproduce": p.canReproduce,
        "reproThresholdMult": p.reproThresholdMult,
        "reproInheritMult": p.reproInheritMult,
        "reproInheritRatio": p.reproInheritRatio,
        "attrs": dict(p.attrs),
        "needs": [{"key": n.key, "amount": n.amount} for n in p.needs],
        "rules": [{"sell": r.sell, "buy": r.buy, "rate": r.rate} for r in p.rules],
        "birthRound": p.birthRound,
        "dependent": p.dependent,
        "weanMinRounds": p.weanMinRounds,
        "weanMaxRounds": p.weanMaxRounds,
    }


def _person_from_dict(d: dict, defaults: DefaultSettings) -> Person:
    return Person(
        id=int(d.get("id", 0)),
        parentId=d.get("parentId"),
        name=d.get("name") or "未命名",
        metabolism=norm_metabolism(d.get("metabolism"), defaults.metabolism.res),
        income=norm_metabolism(d.get("income"), defaults.income.res),
        canReproduce=d.get("canReproduce", True),
        reproThresholdMult=float(d.get("reproThresholdMult", defaults.reproThresholdMult)),
        reproInheritMult=float(d.get("reproInheritMult", defaults.reproInheritMult)),
        reproInheritRatio=float(d.get("reproInheritRatio", defaults.reproInheritRatio)),
        attrs={k: float(v) for k, v in (d.get("attrs") or {}).items()},
        needs=[Need(n["key"], float(n["amount"])) for n in (d.get("needs") or [])],
        rules=[norm_ask(r) for r in (d.get("rules") or [])],
        birthRound=(int(d["birthRound"]) if d.get("birthRound") is not None else None),
        dependent=bool(d.get("dependent", True)),
        weanMinRounds=(int(d["weanMinRounds"]) if d.get("weanMinRounds") is not None else None),
        weanMaxRounds=(int(d["weanMaxRounds"]) if d.get("weanMaxRounds") is not None else None),
    )


# ===================== 个体列表 =====================
def export_persons(path: str, persons: list[Person]) -> None:
    data = {
        "type": "economy-persons",
        "version": 1,
        "persons": [_person_to_dict(p) for p in persons],
        "exportedAt": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def import_persons(path: str, defaults: DefaultSettings) -> list[Person]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [_person_from_dict(p, defaults) for p in data.get("persons", [])]


# ===================== 基础设置 =====================
def export_defaults(path: str, defaults: DefaultSettings, government: Government | None = None) -> None:
    data = {
        "type": "economy-defaults",
        "version": 1,
        "defaultSettings": {
            "metabolism": {"res": defaults.metabolism.res, "amount": defaults.metabolism.amount},
            "income": {"res": defaults.income.res, "amount": defaults.income.amount},
            "canReproduce": defaults.canReproduce,
            "reproThresholdMult": defaults.reproThresholdMult,
            "reproInheritMult": defaults.reproInheritMult,
            "reproInheritRatio": defaults.reproInheritRatio,
            "weanMinRounds": defaults.weanMinRounds,
            "weanMaxRounds": defaults.weanMaxRounds,
            "attrs": dict(defaults.attrs),
            "needs": [{"key": n.key, "amount": n.amount} for n in defaults.needs],
            "rules": [{"sell": r.sell, "buy": r.buy, "rate": r.rate} for r in defaults.rules],
            "perishable_resources": list(defaults.perishable_resources),
            "adaptive_pricing": defaults.adaptive_pricing,
            "price_adjust_alpha": defaults.price_adjust_alpha,
            "price_index_numeraire": defaults.price_index_numeraire,
            "max_population": defaults.max_population,
        },
        "government": {
            "tax_rate": government.tax_rate if government is not None else 0.1,
        },
        "exportedAt": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_gov_tax_rate(path: str) -> float:
    """从 defaults 配置文件读取政府税率，缺失时返回 0.1。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    gov = data.get("government") or {}
    return float(gov.get("tax_rate", 0.1))


def import_defaults(path: str) -> DefaultSettings:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    src = data.get("defaultSettings") or data
    return DefaultSettings(
        metabolism=norm_metabolism(src.get("metabolism"), "食物"),
        income=norm_metabolism(src.get("income"), "钱"),
        canReproduce=src.get("canReproduce", True),
        reproThresholdMult=float(src.get("reproThresholdMult", 2.0)),
        reproInheritMult=float(src.get("reproInheritMult", 1.0)),
        reproInheritRatio=float(src.get("reproInheritRatio", 0.5)),
        weanMinRounds=int(src.get("weanMinRounds", 3)),
        weanMaxRounds=int(src.get("weanMaxRounds", 8)),
        attrs={k: float(v) for k, v in (src.get("attrs") or {}).items()},
        needs=[Need(n["key"], float(n["amount"])) for n in (src.get("needs") or [])],
        rules=[norm_ask(r) for r in (src.get("rules") or [])],
        perishable_resources=[str(r) for r in (src.get("perishable_resources") or [])],
        adaptive_pricing=bool(src.get("adaptive_pricing", False)),
        price_adjust_alpha=float(src.get("price_adjust_alpha", 0.1)),
        price_index_numeraire=str(src.get("price_index_numeraire", "钱")),
        max_population=int(src.get("max_population", 300000)),
    )
