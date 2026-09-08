"""C 层：回合经济指标计算。

职责：把「本回合末状态 + 成交聚合」折算成一组可绘制曲线的指标。
纯函数，无副作用、不触碰 persons 与磁盘 —— 输入什么就输出什么，
因此可以脱离 Simulation 单独测试与复用。

依赖：仅 model.gini（统计工具）。
被调用方：core.Simulation._record_history（算完即写入 B 层历史行）。

所有指标均可为空（None）：无成交/无人挂劳动力时对应指标置 None，
前端用 (h.metrics || {}) 兜底，老历史数据缺字段也不崩。
"""
from __future__ import annotations

from .model import gini

# gini 采样上限：财富列表超过该规模时均匀采样，近似计算，避免 O(N log N)（3.5）
_GINI_SAMPLE_SIZE = 10000


def compute_metrics(
    *,
    trade_agg: dict,
    met_rate: float | None,
    groups: dict[str, int],
    group_wealth: dict[str, float],
    wealth_list: list[float],
    labor_posted: list,
    numeraire: str,
    born: int,
    dead: int,
) -> dict:
    """根据本回合末状态 + 暂存的成交聚合，计算经济指标。

    参数全部显式传入（不用 self），是为了让指标口径可被独立验证：
    trade_agg 结构：{res: {"qty": float, "pay": {res: float}, "sellers": set[id]}}。
    """
    m: dict = {}

    # 1. 价格指数（成交加权，单一记账单位）：平均"每单位商品值多少 钱"
    num_pay = 0.0
    num_qty = 0.0
    for res, ta in trade_agg.items():
        p = ta["pay"].get(numeraire, 0.0)
        if ta["qty"] > 0 and p > 0:
            num_pay += p
            num_qty += ta["qty"]
    m["price_index"] = (num_pay / num_qty) if num_qty > 0 else None

    # 2. 成交总量（所有资源成交量之和）
    m["volume"] = sum(ta["qty"] for ta in trade_agg.values())

    # 3. 需求满足率（生态健康信号）
    m["met_rate"] = met_rate

    # 4. 基尼系数（财富净值分布）：大 N 均匀采样近似，避免 O(N log N)（3.5）
    wl = wealth_list
    if len(wl) > _GINI_SAMPLE_SIZE:
        step = (len(wl) + _GINI_SAMPLE_SIZE - 1) // _GINI_SAMPLE_SIZE
        wl = wl[::step]
    m["gini"] = gini(wl)

    # 5. 产业集中度 HHI（财富口径 + 人口口径）：Σ share²，1/n ~ 1
    total_w = sum(group_wealth.values())
    m["hhi_wealth"] = (
        sum((w / total_w) ** 2 for w in group_wealth.values()) if total_w > 0 else 0.0
    )
    total_p = sum(groups.values())
    m["hhi_pop"] = (
        sum((c / total_p) ** 2 for c in groups.values()) if total_p > 0 else 0.0
    )

    # 6. 失业率（劳动力口径）：挂出劳动力却 0 成交者占比（labor_posted 由调用方同趟收集）
    n_posted = len(labor_posted)
    if n_posted > 0:
        sold_ids = trade_agg.get("劳动力", {}).get("sellers", set())
        employed = sum(1 for p in labor_posted if p.id in sold_ids)
        m["unemployment"] = (n_posted - employed) / n_posted
    else:
        m["unemployment"] = None

    # 7. 出生 / 死亡（回合事件计数）
    m["born"] = born
    m["dead"] = dead
    return m
