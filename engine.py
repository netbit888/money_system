"""经济模拟引擎（从 economy.html 迁移）。

纯逻辑层，无 UI。算法与 HTML 版本保持一致，输出日志文本相同。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ===================== 数据结构 =====================
@dataclass
class Rule:
    """挂牌规则：1 单位 sell 兑换 rate 单位 buy。"""
    sell: str
    buy: str
    rate: float


@dataclass
class Metabolism:
    """代谢/收入：每回合扣减/增加 amount 单位 res 资源。"""
    res: str
    amount: float


@dataclass
class Need:
    key: str
    amount: float


@dataclass
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


# ===================== 引擎 =====================
class Simulation:
    def __init__(self, defaults: DefaultSettings | None = None):
        self.persons: list[Person] = []
        self.next_id: int = 1
        self.round: int = 0
        self.defaults: DefaultSettings = defaults or DefaultSettings(
            metabolism=Metabolism("食物", 1),
            income=Metabolism("钱", 1),
        )
        self.government: Government = Government()
        # 持久化：历史曲线 + 快照存盘
        self.history: list[dict] = []                    # 每回合一行 {round, total, groups, resource_totals}
        self._history_dir = "snapshots"
        self._history_path = os.path.join(self._history_dir, "history.json")
        self._persist_every = 10                          # 每 10 回合存一次盘
        self._load_history()

    # ===================== 持久化 =====================
    def _load_history(self) -> None:
        """启动时加载已有历史。"""
        try:
            if os.path.exists(self._history_path):
                with open(self._history_path, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
        except Exception:
            self.history = []

    def _record_history(self) -> None:
        """记录当前回合到历史曲线。每回合调用一次。"""
        groups: dict[str, int] = {}
        resource_totals: dict[str, float] = {}
        for p in self.persons:
            base = get_base_name(p.name)
            groups[base] = groups.get(base, 0) + 1
            for k, v in p.attrs.items():
                resource_totals[k] = resource_totals.get(k, 0.0) + float(v)
        self.history.append({
            "round": self.round,
            "total": len(self.persons),
            "groups": groups,
            "resource_totals": resource_totals,
        })

    def _maybe_persist(self) -> None:
        """每 N 回合存盘一次。"""
        if self.round % self._persist_every != 0:
            return
        try:
            os.makedirs(self._history_dir, exist_ok=True)
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False)
        except Exception:
            pass  # 存盘失败不阻塞主流程

    def get_history(self, start: int = 0, end: int | None = None) -> list[dict]:
        """返回历史曲线数据切片。"""
        if end is None:
            return [h for h in self.history if h["round"] >= start]
        return [h for h in self.history if start <= h["round"] <= end]

    def clear_history(self) -> None:
        """清空历史（reset_round 时调用）。"""
        self.history = []
        try:
            if os.path.exists(self._history_path):
                os.remove(self._history_path)
        except Exception:
            pass

    # —— 引擎辅助 ——
    def _norm_person_metabolism(self, p: Person) -> Metabolism:
        # 已是规范 dataclass 直接用（内存中的 Person），dict 走规范化（JSON 导入路径）
        if isinstance(p.metabolism, Metabolism):
            return p.metabolism
        return norm_metabolism(p.metabolism, self.defaults.metabolism.res)

    def _norm_person_income(self, p: Person) -> Metabolism:
        if isinstance(p.income, Metabolism):
            return p.income
        return norm_metabolism(p.income, self.defaults.income.res)

    # ===================== 回合 5 步闭环 =====================
    def next_round(self, verbose: bool = False) -> str:
        """推进 1 回合：清除易腐→收入→交换→代谢→死亡→繁殖→抚养。

        时序说明：先收入产出供给，再交换让个体获取代谢所需资源，最后代谢消耗。
        这样以易腐资源（如劳动力）为代谢资源的个体，可通过当回合交换获得资源存活。
        verbose=False（默认）：输出汇总日志，适合大 N。
        verbose=True：输出每个体每步详细日志，调试用。
        """
        if not self.persons:
            raise ValueError("无个体")

        self.round += 1
        n_before = len(self.persons)
        # 回合前快照（用于末尾资源总量变化计算）
        before = {p.id: dict(p.attrs) for p in self.persons}

        out = [f"═══ 第 {self.round} 回合 ═══"]
        if verbose:
            out.append("回合前：")
            for p in self.persons:
                out.append(f"  {p.name}：{self._fmt_attrs(p.attrs)}")
            out.append("")

        # 0. 清除易腐资源（仅当期有效）：代谢前清零，本回合收入再生产新鲜资源
        cleared: dict[str, float] = {}
        if self.defaults.perishable_resources:
            for p in self.persons:
                for res in self.defaults.perishable_resources:
                    v = float(p.attrs.get(res, 0))
                    if v > 0:
                        p.attrs[res] = 0.0
                        cleared[res] = cleared.get(res, 0.0) + v
            if verbose:
                out.append("── 易腐资源清除 ──")
                if cleared:
                    for res, amt in cleared.items():
                        out.append(f"  {res}：共清除 {fmt_num(amt)}")
                else:
                    out.append("  （无易腐资源库存）")
                out.append("")
            elif cleared:
                out.append("易腐资源已清除：" + "，".join(f"{res} {fmt_num(amt)}" for res, amt in cleared.items()))

        # 1. 基础收入：先产出资源，为后续交换提供供给
        earned = []
        for p in self.persons:
            inc = self._norm_person_income(p)
            if inc.amount <= 0:
                continue
            before_v = float(p.attrs.get(inc.res, 0))
            p.attrs[inc.res] = before_v + inc.amount
            earned.append((p, inc.res, inc.amount, before_v, p.attrs[inc.res]))
        if verbose and earned:
            out.append("── 基础收入 ──")
            for p, res, amt, b, a in earned:
                out.append(f"  {p.name}：{res} {fmt_num(b)} → {fmt_num(a)}（+{fmt_num(amt)}）")

        # 2. 交换计算：个体用本回合收入产出的资源交易，获取代谢所需
        out.append("")
        out.extend(self.calculate(verbose=verbose).split("\n"))

        # 3. 基础代谢：每个个体按自身 metabolism {res, amount} 扣减（资源可能来自交换）
        consumed = []
        for p in self.persons:
            m = self._norm_person_metabolism(p)
            before_v = float(p.attrs.get(m.res, 0))
            p.attrs[m.res] = before_v - m.amount
            consumed.append((p, m.res, m.amount, before_v, p.attrs[m.res]))

        if verbose:
            out.append("── 基础代谢 ──")
            for p, res, amt, b, a in consumed:
                out.append(f"  {p.name}：{res} {fmt_num(b)} → {fmt_num(a)}（-{fmt_num(amt)}）")

        # 4. 死亡检查：代谢资源 < 0 立即消灭（破产）
        #    死亡清算：剩余资产（正值）全部转入政府国库，不再凭空消失
        dead = [(p, res, a) for p, res, _amt, _b, a in consumed if float(a) < 0]
        n_dead = len(dead)
        seized_total: dict[str, float] = {}
        if dead:
            if verbose:
                out.append("")
                out.append("── 死亡 ──")
            for p, res, a in dead:
                seized: dict[str, float] = {}
                for k, v in list(p.attrs.items()):
                    v = float(v)
                    if v > 0:
                        self.government.collect(k, v)          # 资产归政府国库
                        seized[k] = v
                        seized_total[k] = seized_total.get(k, 0.0) + v
                if verbose:
                    out.append(f"  ✗ {p.name} 死亡（{res}={fmt_num(a)}）")
                    if seized:
                        out.append(f"    清算资产归政府：{self._fmt_attrs(seized)}")
                    else:
                        out.append("    资产已空，无清算")
            dead_ids = {p.id for p, _r, _a in dead}
            self.persons = [p for p in self.persons if p.id not in dead_ids]

        # 5. 繁殖检查：代谢资源 > 阈值×代谢值 → 转移资源产生子代
        #    守恒规则：母体付出 cost，子代最多获得 cost（差额为生育损耗）；
        #    非代谢资源按 reproInheritRatio 从母体【转移】而非复制。
        born = []
        reproducers = [p for p in self.persons if p.canReproduce]
        for p in reproducers:
            m = self._norm_person_metabolism(p)
            amount = m.amount
            if amount <= 0:
                continue                               # 代谢为 0 → 阈值为 0 → 禁止繁殖（防指数爆炸）
            current = float(p.attrs.get(m.res, 0))
            threshold = amount * p.reproThresholdMult
            if current <= threshold:
                continue
            cost = threshold                                    # 母体付出
            inherit = min(amount * p.reproInheritMult, cost)    # 夹紧：子代获得不得超过母体付出
            ratio = min(1.0, max(0.0, p.reproInheritRatio))
            # 非代谢资源：从母体转移（母减子增）
            child_attrs: dict[str, float] = {}
            for k, v in list(p.attrs.items()):
                v = float(v)
                if k == m.res or v <= 0:
                    continue
                give = v * ratio
                child_attrs[k] = give
                p.attrs[k] = v - give
            p.attrs[m.res] = current - cost                      # 母体付出生育成本
            child_attrs[m.res] = inherit
            loss = cost - inherit                                # 生育损耗：退出系统
            child_id = self.next_id
            self.next_id += 1
            child = Person(
                id=child_id,
                parentId=p.id,                                # 永久依赖母体
                name=f"{get_base_name(p.name)}#{child_id}",  # 基础名+子代id（避免链式叠加）
                metabolism=Metabolism(m.res, amount),
                income=self._norm_person_income(p),          # 克隆母体收入
                canReproduce=p.canReproduce,
                reproThresholdMult=p.reproThresholdMult,
                reproInheritMult=p.reproInheritMult,
                reproInheritRatio=ratio,
                attrs=child_attrs,
                needs=[Need(n.key, n.amount) for n in p.needs],
                rules=[norm_ask(r) for r in p.rules],
                birthRound=self.round,
                dependent=True,
                weanMinRounds=p.weanMinRounds,
                weanMaxRounds=p.weanMaxRounds,
            )
            self.persons.append(child)
            born.append((p, child, m.res, cost, inherit, loss))
        n_born = len(born)
        if verbose and born:
            out.append("")
            out.append("── 繁殖 ──")
            for parent, child, res, cost, inherit, loss in born:
                out.append(
                    f"  ✓ {parent.name} → {child.name}"
                    f"（母体-{fmt_num(cost)}{res}，子代+{fmt_num(inherit)}{res}，"
                    f"损耗 {fmt_num(loss)}{res}）"
                )

        # 6. 断乳：子代富裕到能自繁殖即解除母体依赖（抚养前判定）。
        #    否则终身抚养会让子代永不死亡（每回合收支相抵），选择压力完全失效。
        # 两条路径：①富裕到能自繁殖 → 提前独立；②超过最大养育期 → 强制独立。
        # 仅有 ① 时实操中几乎不可达（子代财富常堆积在非代谢资源上），
        # ②保证母体不会被终身拖累，也让子代真正暴露在死亡压力下。
        weaned = []
        for c in self.persons:
            if c.parentId is None or not c.dependent:
                continue
            cm = self._norm_person_metabolism(c)
            if cm.amount <= 0:
                continue
            # 个体级养育期优先，未设置则继承全局
            min_r = c.weanMinRounds if c.weanMinRounds is not None else self.defaults.weanMinRounds
            max_r = c.weanMaxRounds if c.weanMaxRounds is not None else self.defaults.weanMaxRounds
            min_rounds = max(0, int(min_r))
            max_rounds = max(min_rounds, int(max_r))
            if c.birthRound is not None:
                age = self.round - c.birthRound
                if age < min_rounds:
                    continue                       # 未过最小养育期
                if age >= max_rounds:
                    c.dependent = False            # 到点强制独立
                    weaned.append(c)
                    continue
            if float(c.attrs.get(cm.res, 0)) > cm.amount * c.reproThresholdMult:
                c.dependent = False                # 富裕到能自繁殖，提前独立
                weaned.append(c)
        if verbose and weaned:
            out.append("")
            out.append("── 断乳 ──")
            for c in weaned:
                out.append(f"  ⟩ {c.name} 已独立（脱离母体抚养）")

        # 7. 抚养阶段：母体给每个仍依赖的子代转移其【代谢所需资源】（量=子代代谢值）
        transfers = []
        failures = []
        # 预建 parentId → children 索引（O(N)，避免每父一次全表扫描的 O(N²)）
        children_by_parent: dict[int, list[Person]] = {}
        for c in self.persons:
            if c.parentId is not None and c.dependent:
                children_by_parent.setdefault(c.parentId, []).append(c)
        for parent in self.persons:
            children = children_by_parent.get(parent.id)
            if not children:
                continue
            for child in children:
                cm = self._norm_person_metabolism(child)
                need_res = cm.res          # 子代需要的资源（不是母体的代谢资源）
                need = cm.amount
                if need <= 0:
                    continue
                remaining = float(parent.attrs.get(need_res, 0))
                if remaining >= need:
                    parent.attrs[need_res] = remaining - need
                    child.attrs[need_res] = float(child.attrs.get(need_res, 0)) + need
                    transfers.append((parent, child, need_res, need, False))
                elif remaining > 0:
                    parent.attrs[need_res] = 0.0
                    child.attrs[need_res] = float(child.attrs.get(need_res, 0)) + remaining
                    transfers.append((parent, child, need_res, remaining, True))
                else:
                    failures.append((parent, child, need_res))
        n_transfers = len(transfers)
        n_failures = len(failures)
        if verbose and (transfers or failures):
            out.append("")
            out.append("── 抚养 ──")
            for parent, child, res, amt, partial in transfers:
                tag = "（部分）" if partial else ""
                out.append(f"  {parent.name} → {child.name}：{fmt_num(amt)}{res}{tag}")
            for parent, child, res in failures:
                out.append(f"  ✗ {parent.name} → {child.name}：抚养失败（无{res}）")

        n_after = len(self.persons)

        if verbose:
            # 回合后状态（带变化量）
            out.append("")
            out.append("── 回合后 ──")
            if not self.persons:
                out.append("  无个体存活")
            else:
                for p in self.persons:
                    if p.id in before:
                        out.append(f"  {p.name}：{self._fmt_attrs_diff(p.attrs, before[p.id])}")
                    else:
                        out.append(f"  {p.name}：{self._fmt_attrs(p.attrs)}（新生）")
        else:
            # 汇总：人口变化 + 事件计数 + 资源总量变化
            delta_pop = n_after - n_before
            sign = "+" if delta_pop >= 0 else ""
            out.append(f"人口：{n_before} → {n_after}（{sign}{delta_pop}）")
            out.append(
                f"死亡：{n_dead} / 繁殖：{n_born} / 断乳：{len(weaned)} / "
                f"抚养：{n_transfers + n_failures} 笔（成功 {n_transfers} / 失败 {n_failures}）"
            )
            if n_dead > 0 and seized_total:
                out.append(
                    "死亡清算（归政府）："
                    + "，".join(f"{k} {fmt_num(v)}" for k, v in sorted(seized_total.items()))
                )

            # 资源总量变化（包括死亡消失的资源、新生增加的资源）
            before_total: dict[str, float] = {}
            for attrs in before.values():
                for k, v in attrs.items():
                    before_total[k] = before_total.get(k, 0.0) + float(v)
            after_total: dict[str, float] = {}
            for p in self.persons:
                for k, v in p.attrs.items():
                    after_total[k] = after_total.get(k, 0.0) + float(v)
            all_keys = set(before_total) | set(after_total)
            if all_keys:
                out.append("")
                out.append("── 资源总量变化 ──")
                for k in sorted(all_keys):
                    before_v = before_total.get(k, 0.0)
                    after_v = after_total.get(k, 0.0)
                    delta = after_v - before_v
                    if delta > 0:
                        delta_str = f"（+{fmt_num(delta)}）"
                    elif delta < 0:
                        delta_str = f"（-{fmt_num(abs(delta))}）"
                    else:
                        delta_str = "（不变）"
                    out.append(f"  {k}：{fmt_num(before_v)} → {fmt_num(after_v)}{delta_str}")

        # 持久化：记录历史曲线 + 每 N 回合存盘
        self._record_history()
        self._maybe_persist()

        return "\n".join(out)

    # ===================== 交换计算 =====================
    # 生产者挂牌定价模式：卖家明码标价，买家按价低优先购买
    # 规则 { sell, buy, rate }：1 sell = rate buy，rate 即单价（升序=便宜优先）
    def calculate(self, verbose: bool = False) -> str:
        """交换计算。

        verbose=False（默认）：输出汇总日志（按资源统计 + 整体 + 总量变化），适合大 N。
        verbose=True：输出每笔匹配/成交详细日志，调试用，N 大时慎用（O(N²) 字符串）。
        """
        if not self.persons:
            raise ValueError("无个体")

        # 工作副本：复制 attrs/needs/rules，过程中不破坏原数据
        entities = [
            Person(
                id=p.id, parentId=p.parentId, name=p.name,
                metabolism=p.metabolism, income=p.income,
                canReproduce=p.canReproduce,
                reproThresholdMult=p.reproThresholdMult,
                reproInheritMult=p.reproInheritMult,
                attrs=dict(p.attrs),
                needs=[Need(n.key, n.amount) for n in p.needs],
                rules=[norm_ask(r) for r in p.rules],
            )
            for p in self.persons
        ]
        before_attrs = [dict(e.attrs) for e in entities]

        n_needs = sum(len(e.needs) for e in entities)
        n_rules = sum(len(e.rules) for e in entities)
        out = ["═══ 交换计算 ═══"]
        if verbose:
            out.append("参与：" + "、".join(e.name for e in entities))
            out.append("初始：")
            for e in entities:
                out.append(f"  {e.name}：{self._fmt_attrs(e.attrs)}")
            out.append("")
        else:
            out.append(f"参与：{len(entities)} 人 / {n_needs} 需求 / {n_rules} 挂牌")

        # 需求收集 + 按资源分组（A3：双指针扫描的核心）
        needs_by_res: dict[str, list[tuple[Person, float]]] = {}
        for A in entities:
            for n in A.needs:
                needs_by_res.setdefault(n.key, []).append((A, float(n.amount)))

        # 卖家索引：资源 → [(卖方, 规则), ...]，按 rate 升序预排序
        # 同一卖家的多 ask 通过 seller.attrs[res] 实时维护库存，自然同步
        sellers_by_res: dict[str, list[tuple[Person, Rule, int]]] = {}
        rule_listed: dict[tuple[int, int], float] = {}   # (person_id, rule_idx) -> 挂牌时库存
        rule_sold: dict[tuple[int, int], float] = {}     # (person_id, rule_idx) -> 本回合成交量
        for B in entities:
            for idx, rule in enumerate(B.rules):
                if rule.rate <= 0:
                    continue
                sellers_by_res.setdefault(rule.sell, []).append((B, rule, idx))
                rule_listed[(B.id, idx)] = float(B.attrs.get(rule.sell, 0))
        for lst in sellers_by_res.values():
            lst.sort(key=lambda x: x[1].rate)

        trades = []
        # 按资源累计统计：买方数/卖方数/成交笔数/流转量/总需求/已满足
        resource_stats: dict[str, dict] = {}

        def _res_stat(res: str) -> dict:
            return resource_stats.setdefault(res, {
                "buyers": set(), "sellers": set(),
                "trades": 0, "volume": 0.0, "demand": 0.0, "met": 0.0,
            })

        if not needs_by_res:
            out.append("── 无需求 ──")
        else:
            if verbose:
                out.append("── 需求匹配 ──")
            # 按资源种类顺序处理（保证跨市场依赖一致：先食物市场，再钱市场...）
            # 每个市场内：买家按出现顺序，卖家按 rate 升序，双指针扫描出清
            # 复杂度：每市场 O(买家数 + 卖家数)，总和 O(N)，排序 O(N log N)
            for res in sorted(needs_by_res.keys()):
                buyers = needs_by_res[res]
                sellers = sellers_by_res.get(res, [])
                stat = _res_stat(res)

                if verbose:
                    out.append("")
                    out.append(f"── 资源 {res}：{len(buyers)} 买 / {len(sellers)} 卖 ──")

                # 双指针：seller_idx 单调前进，库存耗尽才前进；下个买家接着用剩余卖家
                seller_idx = 0
                n_sellers = len(sellers)

                for A, amount in buyers:
                    stat["buyers"].add(A.id)
                    stat["demand"] += amount

                    if verbose:
                        out.append(f"[{A.name}] 买 {res}×{fmt_num(amount)}")
                    if amount <= 0:
                        if verbose:
                            out.append("  → 购买量为 0，跳过")
                        continue

                    remaining = amount
                    while remaining > 0 and seller_idx < n_sellers:
                        B, rule, idx = sellers[seller_idx]
                        if B is A:
                            seller_idx += 1
                            continue
                        inv = float(B.attrs.get(res, 0))
                        if inv <= 0:
                            seller_idx += 1
                            continue
                        pay_res = rule.buy
                        rate = rule.rate
                        a_pay = float(A.attrs.get(pay_res, 0))
                        if a_pay <= 0:
                            if verbose:
                                out.append(f"  ✗ 无{pay_res}支付")
                            break  # 买家没钱，跳过该买家（下个卖家更贵，更付不起）

                        max_by_budget = a_pay / rate
                        q = min(remaining, inv, max_by_budget)
                        if q <= 0:
                            # 卖家库存或买家支付不足，下个卖家
                            seller_idx += 1
                            continue

                        pay = q * rate
                        # 政府抽税：从支付额中按税率抽取，卖家实收 = pay - tax
                        tax = pay * self.government.tax_rate
                        seller_gets = pay - tax
                        A.attrs[pay_res] = a_pay - pay
                        A.attrs[res] = float(A.attrs.get(res, 0)) + q
                        B.attrs[res] = inv - q
                        B.attrs[pay_res] = float(B.attrs.get(pay_res, 0)) + seller_gets
                        rule_sold[(B.id, idx)] = rule_sold.get((B.id, idx), 0.0) + q
                        self.government.collect(pay_res, tax)
                        remaining -= q

                        stat["sellers"].add(B.id)
                        stat["trades"] += 1
                        stat["volume"] += q
                        stat["met"] += q

                        if verbose:
                            out.append(f"  ✓ {B.name} 成交 {fmt_num(q)}，付 {fmt_num(pay)}{pay_res}（税 {fmt_num(tax)}，@{fmt_num(rate)}）")
                        trades.append((
                            A.name, B.name, res,
                            q, pay, pay_res, rate, tax,
                        ))

                        # 卖家库存耗尽 → 下个卖家；否则继续用该卖家
                        if inv - q <= 0:
                            seller_idx += 1

                    if verbose:
                        met = amount - remaining
                        if remaining <= 0:
                            out.append(f"  → 完成 {fmt_num(met)}/{fmt_num(amount)}")
                        else:
                            out.append(f"  → 成交 {fmt_num(met)}/{fmt_num(amount)}")

        # 汇总输出（非 verbose 模式）：按资源统计 + 整体
        if not verbose and resource_stats:
            out.append("")
            out.append("── 资源汇总 ──")
            for res in sorted(resource_stats.keys()):
                s = resource_stats[res]
                out.append(
                    f"  {res}：{len(s['buyers'])} 买 / {len(s['sellers'])} 卖 / "
                    f"{s['trades']} 笔 / 流转 {fmt_num(s['volume'])} / "
                    f"满足 {fmt_num(s['met'])}/{fmt_num(s['demand'])}"
                )
            total_trades = sum(s["trades"] for s in resource_stats.values())
            total_volume = sum(s["volume"] for s in resource_stats.values())
            total_demand = sum(s["demand"] for s in resource_stats.values())
            total_met = sum(s["met"] for s in resource_stats.values())
            out.append("")
            out.append("── 整体 ──")
            rate_pct = f" ({total_met/total_demand*100:.1f}%)" if total_demand > 0 else ""
            out.append(
                f"  成交 {total_trades} 笔 / 流转 {fmt_num(total_volume)} / "
                f"满足率 {fmt_num(total_met)}/{fmt_num(total_demand)}{rate_pct}"
            )

        # 政府税收汇总（两种模式均输出）
        if self.government.tax_rate > 0 and self.government.total_collected:
            out.append("")
            out.append("── 政府税收 ──")
            for res in sorted(set(self.government.treasury) | set(self.government.total_collected)):
                out.append(
                    f"  {res}：国库 {fmt_num(self.government.treasury.get(res, 0))}"
                    f" / 累计征收 {fmt_num(self.government.total_collected.get(res, 0))}"
                )

        # 交易汇总（仅 verbose）
        if verbose and trades:
            out.append("")
            out.append("── 交易汇总 ──")
            for buyer, seller, res, qty, pay, pay_res, rate, tax in trades:
                out.append(
                    f"  {buyer} ← {seller}：{res}×{fmt_num(qty)} @"
                    f"{fmt_num(rate)}{pay_res} = {fmt_num(pay)}{pay_res}"
                    f"（税 {fmt_num(tax)}）"
                )

        # 最终状态：verbose 模式输出每个体变化（交换是零和的，汇总模式不输出总量变化）
        if verbose:
            out.append("")
            out.append("── 最终状态 ──")
            for e, b in zip(entities, before_attrs):
                out.append(f"  {e.name}：{self._fmt_attrs_diff(e.attrs, b)}")

        # 写回 persons（顺序严格对应）
        for i, e in enumerate(entities):
            self.persons[i].attrs = dict(e.attrs)

        # 自适应调价（轻量价格发现）：按本回合成交率反馈调整每个挂牌的 rate
        # fill = 成交量 / 挂牌库存；卖光(fill=1)→涨价，滞销(fill=0)→降价
        if self.defaults.adaptive_pricing and self.defaults.price_adjust_alpha > 0:
            alpha = float(self.defaults.price_adjust_alpha)
            adjusted = 0
            for (pid, idx), listed in rule_listed.items():
                if listed <= 0:
                    continue
                sold = rule_sold.get((pid, idx), 0.0)
                fill = sold / listed
                p = self.find(pid)
                if p is None or idx >= len(p.rules):
                    continue
                new_rate = p.rules[idx].rate * (1.0 + alpha * (2.0 * fill - 1.0))
                if new_rate < 1e-6:
                    new_rate = 1e-6
                p.rules[idx].rate = new_rate
                adjusted += 1
            out.append(f"价格自适应：调整 {adjusted} 条挂牌（α={alpha}）")

        return "\n".join(out)

    # ===================== 合并操作 =====================
    def next_round_and_calculate(self, verbose: bool = False) -> str:
        """下一回合（已内含交换计算）。

        由于 next_round 已将交换纳入回合时序，本方法等价于调用 next_round，
        保留接口仅为向后兼容。
        """
        return self.next_round(verbose=verbose)

    # ===================== 重置 =====================
    def reset_round(self, config_folder: str = "config") -> str:
        """回合归零 + 从 config 文件夹重新加载。"""
        msgs = []

        # 1. 基础设置
        dpath = os.path.join(config_folder, "economy_defaults.json")
        try:
            self.defaults = import_defaults(dpath)
            msgs.append("基础设置已从 config/economy_defaults.json 加载")
        except FileNotFoundError:
            msgs.append("❌ 未找到 config/economy_defaults.json，基础设置未更新")
        except Exception as e:
            msgs.append(f"❌ 读取 config/economy_defaults.json 失败：{e}")

        # 1.5 政府税率（从同一 defaults 文件读取）
        try:
            self.government.tax_rate = load_gov_tax_rate(dpath)
            self.government.reset()
            msgs.append(f"政府税率已加载：{self.government.tax_rate}")
        except FileNotFoundError:
            self.government.reset()
        except Exception as e:
            msgs.append(f"❌ 读取政府税率失败：{e}")
            self.government.reset()

        # 2. 个体配置
        ppath = os.path.join(config_folder, "economy_persons.json")
        try:
            self.persons = import_persons(ppath, self.defaults)
            self.next_id = max((p.id for p in self.persons), default=0) + 1
            self._fill_missing_birth_round()
            msgs.append(
                f"个体列表已从 config/economy_persons.json 加载（个体数：{len(self.persons)}）"
            )
        except FileNotFoundError:
            msgs.append("❌ 未找到 config/economy_persons.json，个体列表未更新")
            self.persons = []
        except Exception as e:
            msgs.append(f"❌ 读取 config/economy_persons.json 失败：{e}")
            self.persons = []

        # 3. 回合归零 + 清空历史曲线
        self.round = 0
        self.clear_history()

        # 4. 日志输出
        out = [
            "=== 回合已重置（已从 config 文件夹重新加载） ===",
            "",
            f"当前回合数：{self.round}",
            "",
        ]
        for m in msgs:
            out.append(f"- {m}")
        out.append("")
        out.append("重置后个体状态：")
        if not self.persons:
            out.append("  （无个体）")
        else:
            for p in self.persons:
                out.append(f"  {p.name}：{self._fmt_attrs(p.attrs)}")
        return "\n".join(out)

    # ===================== 个体管理 =====================
    def add_person(self, name: str | None = None) -> Person:
        """按 defaultSettings 模板创建个体。"""
        if not name:
            name = f"个体{self.next_id}"
        p = Person(
            id=self.next_id,
            parentId=None,
            name=name,
            metabolism=norm_metabolism(self.defaults.metabolism),
            income=norm_metabolism(self.defaults.income),
            canReproduce=self.defaults.canReproduce,
            reproThresholdMult=self.defaults.reproThresholdMult,
            reproInheritMult=self.defaults.reproInheritMult,
            reproInheritRatio=self.defaults.reproInheritRatio,
            attrs=dict(self.defaults.attrs),
            needs=[Need(n.key, n.amount) for n in self.defaults.needs],
            rules=[norm_ask(r) for r in self.defaults.rules],
        )
        self.next_id += 1
        self.persons.append(p)
        return p

    def del_person(self, pid: int) -> None:
        self.persons = [p for p in self.persons if p.id != pid]

    def find(self, pid: int) -> Person | None:
        return next((p for p in self.persons if p.id == pid), None)

    def _fill_missing_birth_round(self) -> None:
        """导入的个体若缺少 birthRound，用当前回合补上，养育期从导入时刻起算。"""
        for p in self.persons:
            if p.parentId is not None and p.birthRound is None:
                p.birthRound = self.round

    # ===================== 状态查询（前端友好的精简摘要）=====================
    def summary(self) -> dict:
        """前端用：人数 + 按基础名分组的计数（饼图直接用）。"""
        groups: dict[str, int] = {}
        for p in self.persons:
            base = get_base_name(p.name)
            groups[base] = groups.get(base, 0) + 1
        return {
            "round": self.round,
            "total": len(self.persons),
            "groups": groups,
        }

    # ===================== 日志辅助 =====================
    @staticmethod
    def _fmt_attrs(attrs: dict) -> str:
        return "，".join(f"{k}={fmt_num(v)}" for k, v in attrs.items())

    @staticmethod
    def _fmt_attrs_diff(after: dict, before: dict) -> str:
        keys = list(dict.fromkeys(list(before.keys()) + list(after.keys())))
        parts = []
        for k in keys:
            bv = float(before.get(k, 0))
            av = float(after.get(k, 0))
            diff = av - bv
            if diff == 0:
                parts.append(f"{k}={fmt_num(av)}")
            else:
                arrow = "↑" if diff > 0 else "↓"
                parts.append(f"{k}={fmt_num(av)} {arrow}{fmt_num(abs(diff))}")
        return "，".join(parts)


# ===================== IO =====================
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
            "attrs": dict(defaults.attrs),
            "needs": [{"key": n.key, "amount": n.amount} for n in defaults.needs],
            "rules": [{"sell": r.sell, "buy": r.buy, "rate": r.rate} for r in defaults.rules],
            "perishable_resources": list(defaults.perishable_resources),
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
        attrs={k: float(v) for k, v in (src.get("attrs") or {}).items()},
        needs=[Need(n["key"], float(n["amount"])) for n in (src.get("needs") or [])],
        rules=[norm_ask(r) for r in (src.get("rules") or [])],
        perishable_resources=[str(r) for r in (src.get("perishable_resources") or [])],
    )


# ===================== 冒烟测试 =====================
if __name__ == "__main__":
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
