"""A 层：数据结构 + 规范化 + 通用工具。

职责：定义仿真中流转的全部数据形态（个体/规则/需求/政府/默认模板），
以及与之配套的规范化、格式化、统计工具。

依赖：仅标准库。不依赖 engine 包内任何模块，也不含 IO 与仿真逻辑，
是包内最底层（被 history / metrics / config_io / core 共同依赖）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# ===================== 数据结构 =====================
@dataclass(slots=True)
class Rule:
    """挂牌规则：1 单位 sell 兑换 rate 单位 buy。"""
    sell: str
    buy: str
    rate: float


@dataclass(slots=True)
class Metabolism:
    """代谢/收入：每回合扣减/增加 amount 单位 res 资源。"""
    res: str
    amount: float


@dataclass(slots=True)
class Need:
    key: str
    amount: float


@dataclass(slots=True)
class Person:
    id: int
    parentId: int | None           # 永久依赖母体（繁殖时记录）
    name: str
    metabolism: Metabolism
    income: Metabolism
    canReproduce: bool = True
    reproThresholdMult: float = 2.0   # 繁殖阈值倍数（个体级，可独立调整）
    reproInheritMult: float = 1.0     # 子代继承倍数（个体级，可独立调整）
    # 子代继承非代谢资源的比例：从母体转移（母减子增），不是复制。
    # 0=白手起家，1=母体非代谢资源全部给子代。这是保证资源守恒的关键字段。
    reproInheritRatio: float = 0.5
    attrs: dict[str, float] = field(default_factory=dict)
    needs: list[Need] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    # —— 抚养与断乳 ——
    birthRound: int | None = None   # 出生回合（原始个体为 None）
    dependent: bool = True          # 是否仍依赖母体接受抚养；断乳后置 False
    # 个体级养育期覆盖（None = 继承 DefaultSettings）；繁殖时传给子代
    weanMinRounds: int | None = None
    weanMaxRounds: int | None = None


@dataclass
class DefaultSettings:
    """抽象模板：新建个体时的初始值。"""
    metabolism: Metabolism
    income: Metabolism
    canReproduce: bool = True
    reproThresholdMult: float = 2.0   # 繁殖阈值倍数：代谢资源 > reproThresholdMult × 代谢值 → 繁殖
    reproInheritMult: float = 1.0     # 子代继承倍数：继承 min(reproInheritMult × 代谢值, 母体付出)
    reproInheritRatio: float = 0.5    # 子代继承非代谢资源的比例（从母体转移，保证守恒）
    weanMinRounds: int = 3            # 最小养育期：出生后至少 N 回合才允许断乳
    weanMaxRounds: int = 8            # 最大养育期：到点强制断乳（富裕度不达标也独立）
    attrs: dict[str, float] = field(default_factory=dict)
    needs: list[Need] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    perishable_resources: list[str] = field(default_factory=list)  # 易腐资源：每回合开始清零，仅当期有效
    adaptive_pricing: bool = False        # 轻量价格发现：按成交率反馈调整每个挂牌 rate
    price_adjust_alpha: float = 0.1       # 调价强度 α：0~1，越大价格波动越剧烈
    price_index_numeraire: str = "钱"     # 价格指数记账单位（单一计价货币，默认 钱）
    max_population: int = 300000          # 个体总数上限：0 = 不限制（防止人口膨胀/内存失控）


@dataclass
class Government:
    """政府：非交易主体，从每笔成交的支付中强制抽取税收。

    不进 persons 列表，不参与代谢/死亡/繁殖，summary() 不计入总人口。
    """
    tax_rate: float = 0.1                        # 税率：从支付额中抽取的比例
    treasury: dict[str, float] = field(default_factory=dict)   # 国库当前持有
    total_collected: dict[str, float] = field(default_factory=dict)  # 累计征收

    def collect(self, res: str, amount: float) -> None:
        """征收 amount 单位 res 资源（同时计入累计）。"""
        if amount <= 0:
            return
        self.treasury[res] = self.treasury.get(res, 0.0) + amount
        self.total_collected[res] = self.total_collected.get(res, 0.0) + amount

    def reset(self) -> None:
        """回合重置时清空国库与累计（税率保留）。"""
        self.treasury.clear()
        self.total_collected.clear()


# ===================== 规范化函数 =====================
def norm_ask(r) -> Rule:
    """规范化挂牌规则。兼容 Rule 对象、sell/buy、res1/res2、from/to 三种历史 dict 格式。"""
    # 已是规范 dataclass 直接克隆返回（保持不可变语义）
    if isinstance(r, Rule):
        return Rule(r.sell, r.buy, r.rate)
    sell, buy, rate = "", "", 0.0
    if isinstance(r, dict):
        if r.get("sell") is not None and r.get("buy") is not None:
            sell, buy, rate = r["sell"], r["buy"], r.get("rate", 0)
        elif r.get("res1") is not None and r.get("res2") is not None:
            sell, buy, rate = r["res1"], r["res2"], r.get("rate", 0)
        elif r.get("from") is not None and r.get("to") is not None:
            sell, buy, rate = r["from"], r["to"], r.get("rate", 0)
    return Rule(
        sell=str(sell or "").strip(),
        buy=str(buy or "").strip(),
        rate=float(rate) if rate else 0.0,
    )


def norm_metabolism(m, fallback_res: str = "食物") -> Metabolism:
    """规范化代谢/收入对象。负 amount 归 0；缺失时 fallback 到 fbRes+1。"""
    fb = fallback_res if isinstance(fallback_res, str) and fallback_res.strip() else "食物"
    if isinstance(m, dict):
        res = str(m.get("res") or "").strip() or fb
        amount = max(0.0, float(m.get("amount") or 0))
        return Metabolism(res, amount)
    if isinstance(m, str) and m.strip():
        return Metabolism(m.strip(), 1.0)
    return Metabolism(fb, 1.0)


# ===================== 格式化 / 统计工具 =====================
def fmt_num(v) -> str:
    """整数无小数，非整数 2 位。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(n):
        return "0"
    if n.is_integer():
        return str(int(n))
    return f"{n:.2f}"


def get_base_name(name: str) -> str:
    """截 # 前的基础名（饼图分组用）。"""
    idx = name.find("#")
    return name[:idx] if idx > 0 else name


def gini(values: list[float]) -> float:
    """基尼系数：财富（或任意非负量）分布的集中度，0=完全平等，1=完全集中。

    采用升序公式 G = (2·Σ i·xᵢ)/(n·Σx) − (n+1)/n（i 从 1 起）。
    空列表或总和为 0 时返回 0.0 以避免除零。
    """
    n = len(values)
    if n == 0:
        return 0.0
    s = sorted(values)
    total = sum(s)
    if total <= 0:
        return 0.0
    cum = 0.0
    for i, x in enumerate(s, start=1):
        cum += i * x
    return (2.0 * cum) / (n * total) - (n + 1.0) / n
