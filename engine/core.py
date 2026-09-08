"""核心层：Simulation（D 交换撮合 + E 回合流程）。

职责：
  - E：一回合的时序闭环（清易腐 → 收入 → 交换 → 代谢 → 死亡 → 繁殖 → 断乳 → 抚养）
  - D：生产者挂牌定价的多轮迭代出清撮合（双指针 + 税收 + 自适应调价）

依赖：
  - A model        （数据结构 + 格式化/规范化工具）
  - B history      （历史持久化，经 HistoryStore 委托）
  - C metrics      （指标计算纯函数）
  - F config_io    （config 目录读写）

⚠️ 热路径：calculate() 的双指针内层循环是反复优化过的（SoA 平行数组 + 局部变量），
不要为了"好看"把它拆成小函数——函数调用开销会直接打在每笔成交上。
"""
from __future__ import annotations

import os
import time

from .config_io import import_defaults, import_persons, load_gov_tax_rate
from .history import HistoryStore
from .metrics import compute_metrics
from .model import (
    DefaultSettings, Government, Metabolism, Need, Person, Rule,
    fmt_num, get_base_name, norm_ask, norm_metabolism,
)

# 市场出清：多轮迭代上限 + 最小成交量。
# 后者用于避免浮点残渣（1e-18 级别的成交）让迭代永远无法收敛。
_MAX_CLEARING_ROUNDS = 8
_TRADE_EPS = 1e-9


# ===================== 引擎 =====================
class Simulation:
    def __init__(self, defaults: DefaultSettings | None = None, history_dir: str = "snapshots"):
        self.persons: list[Person] = []
        self.next_id: int = 1
        self.round: int = 0
        self.defaults: DefaultSettings = defaults or DefaultSettings(
            metabolism=Metabolism("食物", 1),
            income=Metabolism("钱", 1),
        )
        self.government: Government = Government()
        # 经济指标暂存（每回合 calculate/next_round 后刷新，供 _record_history 使用）
        self._last_trade_agg: dict = {}    # 本回合成交聚合：{res: {qty, pay:{pay_res:sum}, sellers:set}}
        self._last_met_rate: float | None = None   # 整体需求满足率（met/demand）
        self._last_tax: dict[str, float] = {}      # 本回合税收（按支付资源），供资源总量 delta 账本
        self._last_res_delta: dict[str, float] = {}  # 本回合资源总量变化（delta 账本）
        self._last_n_dead: int = 0
        self._last_n_born: int = 0
        self._last_metrics: dict | None = None  # 最近一次完整回合的指标（供前端 KPI 卡片）
        self.last_elapsed_ms: float = 0.0   # 最近一回合/一次交换的耗时（ms），供前端展示
        # 持久化：委托 B 层 HistoryStore。history_dir 可注入 —— 测试/基准必须传临时目录，
        # 否则会写进真实的 snapshots/（曾发生过基准脚本覆盖真实历史的事故）
        self._history = HistoryStore(history_dir)

    # ===================== 持久化委托（B 层）=====================
    # 历史存储已独立到 history.HistoryStore，这里保留原有的 Simulation 接口，
    # 供 app.py 与既有测试/基准继续使用（含测试直接读写的内部字段）。
    @property
    def _history_dir(self) -> str:
        return self._history.history_dir

    @property
    def _levels(self) -> list[list[dict]]:
        return self._history.levels

    @property
    def history(self) -> list[dict]:
        """按回合升序的完整历史视图：归档层在前（更老、更粗），L0 在后。"""
        return self._history.history

    @property
    def persist_errors(self) -> int:
        """读写失败计数（B 层累计，不再静默吞掉）。"""
        return self._history.persist_errors

    @property
    def _persist_every(self) -> int:
        return self._history._persist_every

    @_persist_every.setter
    def _persist_every(self, value: int) -> None:
        self._history._persist_every = value

    @property
    def _folds_since_archive_write(self) -> int:
        return self._history._folds_since_archive_write

    @_folds_since_archive_write.setter
    def _folds_since_archive_write(self, value: int) -> None:
        self._history._folds_since_archive_write = value

    def _write_archive(self) -> None:
        self._history.write_archive()

    def _compact_l0(self) -> None:
        self._history.compact_l0()

    def _maybe_persist(self) -> None:
        self._history.maybe_persist(self.round)

    def get_history(self, start: int = 0, end: int | None = None) -> list[dict]:
        """返回历史曲线数据切片（跨归档层与 L0，按回合升序）。"""
        return self._history.get_history(start, end)

    def clear_history(self) -> None:
        """清空历史（reset_round 时调用）。"""
        self._history.clear()

    # ===================== 历史行记录（本层负责采样式，B 层负责存）=====================
    def _record_history(self) -> None:
        """记录当前回合到历史曲线。每回合调用一次。"""
        groups: dict[str, int] = {}
        resource_totals: dict[str, float] = {}
        group_wealth: dict[str, float] = {}
        wealth_list: list[float] = []
        labor_posted: list[Person] = []   # 挂出劳动力的个体（3.6：与统计同趟收集，省二次遍历）
        for p in self.persons:
            base = get_base_name(p.name)
            groups[base] = groups.get(base, 0) + 1
            w = 0.0
            for k, v in p.attrs.items():
                resource_totals[k] = resource_totals.get(k, 0.0) + v
                w += v
            group_wealth[base] = group_wealth.get(base, 0.0) + w
            wealth_list.append(w)
            for r in p.rules:
                if r.sell == "劳动力":
                    labor_posted.append(p)
                    break
        metrics = compute_metrics(
            trade_agg=self._last_trade_agg,
            met_rate=self._last_met_rate,
            groups=groups,
            group_wealth=group_wealth,
            wealth_list=wealth_list,
            labor_posted=labor_posted,
            numeraire=self.defaults.price_index_numeraire,
            born=self._last_n_born,
            dead=self._last_n_dead,
        )
        self._last_metrics = metrics
        self._history.append({
            "round": self.round,
            "span": 1,
            "total": len(self.persons),
            "groups": groups,
            "resource_totals": resource_totals,
            "metrics": metrics,
        })

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
        t0 = time.perf_counter()
        if not self.persons:
            raise ValueError("无个体")

        self.round += 1
        n_before = len(self.persons)
        # 回合前快照：仅 verbose 需要（per-person diff 展示）；大 N 非 verbose 走 delta 账本，省全量拷贝（3.1）
        before = {p.id: dict(p.attrs) for p in self.persons} if verbose else None

        # 资源总量变化账本：回合内就地累加，替代 before/after 快照差值（3.1）
        res_delta: dict[str, float] = {}

        def _add_delta(res: str, amount: float) -> None:
            if amount:
                res_delta[res] = res_delta.get(res, 0.0) + amount

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
                        _add_delta(res, -v)
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
            inc = p.income          # 内存中恒为 Metabolism，省函数调用 + isinstance
            if inc.amount <= 0:
                continue
            before_v = float(p.attrs.get(inc.res, 0))
            p.attrs[inc.res] = before_v + inc.amount
            res_delta[inc.res] = res_delta.get(inc.res, 0.0) + inc.amount
            if verbose:
                earned.append((p, inc.res, inc.amount, before_v, p.attrs[inc.res]))
        if verbose and earned:
            out.append("── 基础收入 ──")
            for p, res, amt, b, a in earned:
                out.append(f"  {p.name}：{res} {fmt_num(b)} → {fmt_num(a)}（+{fmt_num(amt)}）")

        # 2. 交换计算：个体用本回合收入产出的资源交易，获取代谢所需
        out.append("")
        out.extend(self.calculate(verbose=verbose).split("\n"))
        # 税收从 persons 转政府（交换净流出 = tax）
        for res, amt in self._last_tax.items():
            _add_delta(res, -amt)

        # 3. 基础代谢：每个个体按自身 metabolism {res, amount} 扣减（资源可能来自交换）
        consumed = []
        for p in self.persons:
            m = p.metabolism
            before_v = float(p.attrs.get(m.res, 0))
            after = before_v - m.amount
            p.attrs[m.res] = after
            if m.amount:
                res_delta[m.res] = res_delta.get(m.res, 0.0) - m.amount
            if after < 0 or verbose:
                consumed.append((p, m.res, m.amount, before_v, after))

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
                        _add_delta(k, -v)                      # 清算资产转政府
                _add_delta(res, -a)                            # 负代谢资源回补（a < 0）
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
        max_pop = self.defaults.max_population
        for p in self.persons:
            # 人口上限：达到上限即停止本回合繁殖（逐个体判断，防止单回合超生；
            # 死亡名额已在第 4 步清算，当回合即可被占用）。直接遍历 persons，
            # 稳态（已达上限）首个个体即 break，省去 reproducers 列表的 O(N) 构建。
            if max_pop > 0 and len(self.persons) >= max_pop:
                break
            if not p.canReproduce:
                continue
            m = p.metabolism
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
            _add_delta(m.res, -loss)
            child_id = self.next_id
            self.next_id += 1
            child = Person(
                id=child_id,
                parentId=p.id,                                # 永久依赖母体
                name=f"{get_base_name(p.name)}#{child_id}",  # 基础名+子代id（避免链式叠加）
                metabolism=Metabolism(m.res, amount),
                income=p.income,                              # 与 _norm_person_income 原语义一致（共享引用）
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
            cm = c.metabolism
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
                cm = child.metabolism
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

            # 资源总量变化（delta 账本：回合内就地累加，含死亡消失、新生增加、税收、损耗）
            if res_delta:
                out.append("")
                out.append("── 资源总量变化 ──")
                for k in sorted(res_delta):
                    delta = res_delta[k]
                    if delta > 0:
                        delta_str = f"（+{fmt_num(delta)}）"
                    elif delta < 0:
                        delta_str = f"（-{fmt_num(abs(delta))}）"
                    else:
                        delta_str = "（不变）"
                    out.append(f"  {k}：{delta_str}")

        # 持久化：记录历史曲线 + 每 N 回合存盘
        self._last_n_dead = n_dead
        self._last_n_born = n_born
        self._last_res_delta = res_delta
        self._record_history()
        self._maybe_persist()

        self.last_elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return "\n".join(out)

    # ===================== 交换计算 =====================
    # 生产者挂牌定价模式：卖家明码标价，买家按价低优先购买
    # 规则 { sell, buy, rate }：1 sell = rate buy，rate 即单价（升序=便宜优先）
    def calculate(self, verbose: bool = False) -> str:
        """交换计算。

        verbose=False（默认）：输出汇总日志（按资源统计 + 整体 + 总量变化），适合大 N。
        verbose=True：输出每笔匹配/成交详细日志，调试用，N 大时慎用（O(N²) 字符串）。
        """
        t0 = time.perf_counter()
        if not self.persons:
            raise ValueError("无个体")

        self._last_tax = {}   # 本回合税收（按支付资源），供 next_round 的 delta 账本

        # 工作副本（SoA 平行数组，3.3）：
        #   attrs 需复制（交换会修改）；needs/rules 只读，复用引用（不再克隆 Need/Rule/Person）
        n_entities = len(self.persons)
        ids = [p.id for p in self.persons]
        names = [p.name for p in self.persons]
        attrs = [p.attrs.copy() for p in self.persons]
        needs_list = [p.needs for p in self.persons]
        rules_list = [p.rules for p in self.persons]
        before_attrs = [dict(a) for a in attrs] if verbose else None

        n_needs = sum(len(nl) for nl in needs_list)
        n_rules = sum(len(rl) for rl in rules_list)
        out = ["═══ 交换计算 ═══"]
        if verbose:
            out.append("参与：" + "、".join(names))
            out.append("初始：")
            for i in range(n_entities):
                out.append(f"  {names[i]}：{self._fmt_attrs(attrs[i])}")
            out.append("")
        else:
            out.append(f"参与：{n_entities} 人 / {n_needs} 需求 / {n_rules} 挂牌")

        # 待满足需求清单 (买方索引, 资源, 剩余需求) —— 多轮迭代出清的工作集
        pending: list[tuple[int, str, float]] = []
        for i in range(n_entities):
            for nd in needs_list[i]:
                pending.append((i, nd.key, float(nd.amount)))

        # 卖家索引：资源 → [(卖方索引, 规则), ...]，按 rate 升序预排序
        # 同一卖家的多 ask 通过 attrs[i][res] 实时维护库存，自然同步
        sellers_by_res: dict[str, list[tuple[int, Rule, int]]] = {}
        rule_listed: dict[tuple[int, int], float] = {}   # (person_idx, rule_idx) -> 挂牌时库存
        rule_sold: dict[tuple[int, int], float] = {}     # (person_idx, rule_idx) -> 本回合成交量
        for i in range(n_entities):
            for idx, rule in enumerate(rules_list[i]):
                if rule.rate <= 0:
                    continue
                sellers_by_res.setdefault(rule.sell, []).append((i, rule, idx))
                rule_listed[(i, idx)] = float(attrs[i].get(rule.sell, 0))
        for lst in sellers_by_res.values():
            lst.sort(key=lambda x: x[1].rate)

        trades = []
        # 经济指标：本回合成交聚合（资源 → 成交量/支付/卖家集合），供历史曲线使用
        trade_agg: dict[str, dict] = {}
        # 按资源累计统计：买方数/卖方数/成交笔数/流转量/总需求/已满足
        resource_stats: dict[str, dict] = {}

        def _res_stat(res: str) -> dict:
            return resource_stats.setdefault(res, {
                "buyers": set(), "sellers": set(),
                "trades": 0, "volume": 0.0, "demand": 0.0, "met": 0.0,
            })

        # 需求统计：总需求与买方数只统计一次，不随迭代轮次重复累加
        for a_idx, res, amount in pending:
            _st = _res_stat(res)
            _st["demand"] += amount
            _st["buyers"].add(ids[a_idx])

        if not pending:
            out.append("── 无需求 ──")
        else:
            if verbose:
                out.append("── 需求匹配 ──")
            # ===== 多轮迭代出清 =====
            # 单趟扫描会让「资源处理顺序」决定生死：买家支付能力是当刻读取的，
            # 若其卖出市场排在买入市场之后，当回合就没钱买（实测同一配置仅翻转
            # 顺序，100 回合后人口差 14%）。迭代到收敛后，顺序只影响成交先后，
            # 不再决定能否成交。
            # 成本控制：第 2 轮起只重跑「仍有未满足需求」者（工作集），
            #           稳定态下第 1 轮即出清，额外开销接近 0。
            for _rd in range(_MAX_CLEARING_ROUNDS):
                if not pending:
                    break
                # 本轮按资源重新分组（只含仍未满足的需求）
                by_res: dict[str, list[tuple[int, float]]] = {}
                for a_idx, res, amount in pending:
                    by_res.setdefault(res, []).append((a_idx, amount))
                next_pending: list[tuple[int, str, float]] = []
                traded_this_round = False

                for res in sorted(by_res.keys()):
                    buyers = by_res[res]
                    sellers = sellers_by_res.get(res, [])
                    stat = _res_stat(res)

                    if verbose:
                        out.append("")
                        out.append(f"── 第 {_rd + 1} 轮 资源 {res}：{len(buyers)} 买 / {len(sellers)} 卖 ──")

                    # 双指针：本轮内 seller_idx 单调前进；每轮重置
                    # （某个卖家可能在别的市场买入后库存增加）
                    seller_idx = 0
                    n_sellers = len(sellers)

                    for a_idx, amount in buyers:
                        if verbose:
                            out.append(f"[{names[a_idx]}] 买 {res}×{fmt_num(amount)}")
                        if amount <= _TRADE_EPS:
                            if verbose:
                                out.append("  → 购买量为 0，跳过")
                            continue

                        remaining = amount
                        while remaining > _TRADE_EPS and seller_idx < n_sellers:
                            b_idx, rule, idx = sellers[seller_idx]
                            if b_idx == a_idx:
                                seller_idx += 1
                                continue
                            inv = float(attrs[b_idx].get(res, 0))
                            if inv <= _TRADE_EPS:
                                seller_idx += 1
                                continue
                            pay_res = rule.buy
                            rate = rule.rate
                            a_pay = float(attrs[a_idx].get(pay_res, 0))
                            if a_pay <= _TRADE_EPS:
                                if verbose:
                                    out.append(f"  ✗ 无{pay_res}支付")
                                break  # 本轮没钱；下一轮卖出资产拿到钱后会重试

                            max_by_budget = a_pay / rate
                            q = min(remaining, inv, max_by_budget)
                            if q <= _TRADE_EPS:
                                # 卖家库存或买家支付不足，下个卖家
                                seller_idx += 1
                                continue

                            pay = q * rate
                            # 政府抽税：从支付额中按税率抽取，卖家实收 = pay - tax
                            tax = pay * self.government.tax_rate
                            seller_gets = pay - tax
                            # 经济指标：累积本回合成交（资源/支付额/卖家），供历史曲线使用
                            _ta = trade_agg.setdefault(res, {"qty": 0.0, "pay": {}, "sellers": set()})
                            _ta["qty"] += q
                            _ta["pay"][pay_res] = _ta["pay"].get(pay_res, 0.0) + pay
                            _ta["sellers"].add(ids[b_idx])
                            attrs[a_idx][pay_res] = a_pay - pay
                            attrs[a_idx][res] = float(attrs[a_idx].get(res, 0)) + q
                            attrs[b_idx][res] = inv - q
                            attrs[b_idx][pay_res] = float(attrs[b_idx].get(pay_res, 0)) + seller_gets
                            rule_sold[(b_idx, idx)] = rule_sold.get((b_idx, idx), 0.0) + q
                            self._last_tax[pay_res] = self._last_tax.get(pay_res, 0.0) + tax
                            remaining -= q

                            stat["sellers"].add(ids[b_idx])
                            stat["trades"] += 1
                            stat["volume"] += q
                            stat["met"] += q
                            traded_this_round = True

                            if verbose:
                                out.append(f"  ✓ {names[b_idx]} 成交 {fmt_num(q)}，付 {fmt_num(pay)}{pay_res}（税 {fmt_num(tax)}，@{fmt_num(rate)}）")
                            trades.append((
                                names[a_idx], names[b_idx], res,
                                q, pay, pay_res, rate, tax,
                            ))

                            # 卖家库存耗尽 → 下个卖家；否则继续用该卖家
                            if inv - q <= _TRADE_EPS:
                                seller_idx += 1

                        if verbose:
                            met = amount - remaining
                            if remaining <= _TRADE_EPS:
                                out.append(f"  → 完成 {fmt_num(met)}/{fmt_num(amount)}")
                            else:
                                out.append(f"  → 成交 {fmt_num(met)}/{fmt_num(amount)}")

                        # 仍未满足 → 进入下一轮重试（下一轮可能已卖出资产拿到钱）
                        if remaining > _TRADE_EPS:
                            next_pending.append((a_idx, res, remaining))

                pending = next_pending
                if not traded_this_round:
                    break

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

        # 税收统一入账：撮合内不再逐笔 collect，改为结束时一次入账（省每回合 ~10 万次字典操作）
        for _res, _amt in self._last_tax.items():
            self.government.collect(_res, _amt)

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
            for i in range(n_entities):
                out.append(f"  {names[i]}：{self._fmt_attrs_diff(attrs[i], before_attrs[i])}")

        # 写回 persons（顺序严格对应）：attrs 已是独立副本，直接接管，无需再复制
        for i in range(n_entities):
            self.persons[i].attrs = attrs[i]

        # 自适应调价（轻量价格发现）：按本回合成交率反馈调整每个挂牌的 rate
        # fill = 成交量 / 挂牌库存；卖光(fill=1)→涨价，滞销(fill=0)→降价
        if self.defaults.adaptive_pricing and self.defaults.price_adjust_alpha > 0:
            alpha = float(self.defaults.price_adjust_alpha)
            adjusted = 0
            # rule_listed/rule_sold 的 key 直接用个体索引 i（而非 id），
            # 调价时 self.persons[i] 直接取个体，省去 by_id 建表的 O(N) 字典开销
            for (i, idx), listed in rule_listed.items():
                if listed <= 0:
                    continue
                sold = rule_sold.get((i, idx), 0.0)
                fill = sold / listed
                p = self.persons[i]
                if idx >= len(p.rules):
                    continue
                new_rate = p.rules[idx].rate * (1.0 + alpha * (2.0 * fill - 1.0))
                if new_rate < 1e-6:
                    new_rate = 1e-6
                p.rules[idx].rate = new_rate
                adjusted += 1
            out.append(f"价格自适应：调整 {adjusted} 条挂牌（α={alpha}）")

        # 经济指标：暂存本回合成交聚合与整体满足率，供 _record_history 使用
        self._last_trade_agg = trade_agg
        _tot_demand = sum(s["demand"] for s in resource_stats.values())
        _tot_met = sum(s["met"] for s in resource_stats.values())
        self._last_met_rate = (_tot_met / _tot_demand) if _tot_demand > 0 else None

        self.last_elapsed_ms = (time.perf_counter() - t0) * 1000.0
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
        if self.defaults.max_population > 0 and len(self.persons) >= self.defaults.max_population:
            raise ValueError(f"已达个体上限 {self.defaults.max_population}，无法新增")
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
    def latest_metrics(self) -> dict | None:
        """最近一次完整回合计算出的经济指标（供前端 KPI 卡片），尚无回合时为 None。"""
        return self._last_metrics

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
