"""B 层：历史曲线的分层降采样持久化。

职责：把「每回合一行」的指标行按 L0(原始) + L1..Ln(10:1 逐层折叠) 存放，
负责启动加载、追加写、归档落盘、L0 压缩、区间切片查询、清空。

对外接口：HistoryStore（core.Simulation 持有一个实例并委托）。
依赖：仅标准库 + 本模块内的 aggregate_rows。
不依赖 persons / 交易 / 指标语义 —— 只认「行 dict」这一种契约：
    {"round", "span", "total", "groups", "resource_totals", "metrics"}
因此可脱离仿真独立测试。

为什么要分层：长时间运行时历史会无限增长，写入是「每 10 回合全量重写」→ O(R²)。
改为 L0 保留近 1000 回合原始行、更老的按 10:1 逐层折叠归档后，
内存与磁盘占用变成 O(log R)，单次写盘成本与已跑回合数无关。
"""
from __future__ import annotations

import json
import os

_L0_CAPACITY = 1000          # L0 保留的原始行数
_LEVEL_CAPACITY = 1000       # 各归档层保留的行数
_FOLD = 10                   # 折叠倍率：10 行 → 1 行
_ARCHIVE_WRITE_EVERY = 100   # 累计多少次折叠后落盘归档（其间只追加 L0）

# 聚合规则必须按字段语义分派，不能一律取平均
_SUM_METRICS = ("volume", "born", "dead")          # 计数类 → 求和
_MEAN_METRICS = (                                   # 比率/价格类 → 均值
    "price_index", "gini", "hhi_wealth", "hhi_pop", "unemployment", "met_rate",
)


def _mean_or_none(vals: list) -> float | None:
    vals = [float(v) for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def aggregate_rows(rows: list[dict]) -> dict:
    """把连续若干行折叠成一行。

    ⚠️ 计数类（volume/born/dead）必须求和、比率类取均值、存量类取均值。
    若一律取平均，计数类会随折叠倍率缩水，且是静默错误（不报错、只失真）。
    """
    if not rows:
        raise ValueError("aggregate_rows: 行组为空")
    n = len(rows)
    groups_acc: dict[str, float] = {}
    res_acc: dict[str, float] = {}
    sum_acc: dict[str, float] = {k: 0.0 for k in _SUM_METRICS}
    mean_buf: dict[str, list] = {k: [] for k in _MEAN_METRICS}

    for r in rows:
        for k, v in (r.get("groups") or {}).items():
            groups_acc[k] = groups_acc.get(k, 0.0) + float(v)
        for k, v in (r.get("resource_totals") or {}).items():
            res_acc[k] = res_acc.get(k, 0.0) + float(v)
        met = r.get("metrics") or {}
        for k in _SUM_METRICS:
            sum_acc[k] += float(met.get(k) or 0.0)
        for k in _MEAN_METRICS:
            mean_buf[k].append(met.get(k))

    metrics: dict = {k: sum_acc[k] for k in _SUM_METRICS}
    for k in _MEAN_METRICS:
        metrics[k] = _mean_or_none(mean_buf[k])

    return {
        "round": rows[0]["round"],           # 区间起点
        "span": sum(int(r.get("span") or 1) for r in rows),
        "total": _mean_or_none([r.get("total") for r in rows]),
        "groups": {k: v / n for k, v in groups_acc.items()},
        "resource_totals": {k: v / n for k, v in res_acc.items()},
        "metrics": metrics,
    }


class HistoryStore:
    """历史行的分层存储。history_dir 必须可注入（测试/基准传临时目录），
    否则会写进真实的 snapshots/（曾发生过基准脚本覆盖真实历史的事故）。
    """

    def __init__(self, history_dir: str = "snapshots", persist_every: int = 10):
        self._history_dir = history_dir
        self._l0_path = os.path.join(history_dir, "history-l0.jsonl")
        self._archive_path = os.path.join(history_dir, "history-archive.json")
        self._legacy_path = os.path.join(history_dir, "history.json")
        self._levels: list[list[dict]] = [[]]   # [0]=L0 原始行，[1..]=逐层 10:1 归档
        self._archive_max_round = -1            # 归档层已覆盖到的最大回合
        self._l0_last_written_round = -1        # 已追加到 jsonl 的 L0 最大回合
        self._folds_since_archive_write = 0
        self._pending_migration = False
        self.persist_errors = 0                 # 读写失败计数（不再静默吞掉）
        self._persist_every = persist_every     # 每 N 回合追加一次 L0
        self.load()

    # ===================== 只读视图 =====================
    @property
    def history_dir(self) -> str:
        return self._history_dir

    @property
    def levels(self) -> list[list[dict]]:
        """分层原始视图（[0]=L0，越靠后越老越粗）。"""
        return self._levels

    @property
    def history(self) -> list[dict]:
        """按回合升序的完整历史视图：归档层在前（更老、更粗），L0 在后。"""
        out: list[dict] = []
        for lvl in reversed(self._levels):
            out.extend(lvl)
        return out

    # ===================== 启动加载 =====================
    def load(self) -> None:
        """启动时加载：归档层 + L0 日志（跳过已被归档覆盖的行）。"""
        self._levels = [[]]
        self._archive_max_round = -1

        # 1) 归档层（levels[1:]）
        if os.path.exists(self._archive_path):
            try:
                with open(self._archive_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._levels.extend(data.get("levels") or [])
                self._archive_max_round = int(data.get("max_round", -1))
            except Exception as e:
                self.persist_errors += 1
                print(f"[警告] 归档历史读取失败，已忽略：{e}")

        # 2) L0：jsonl 中 round > 归档覆盖范围的行
        rows: list[dict] = []
        if os.path.exists(self._l0_path):
            try:
                with open(self._l0_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        r = json.loads(line)
                        if int(r.get("round", -1)) > self._archive_max_round:
                            rows.append(r)
            except Exception as e:
                self.persist_errors += 1
                print(f"[警告] 历史日志读取失败，已忽略：{e}")

        # 3) 旧版单文件 history.json 的一次性迁移
        if not rows and len(self._levels) == 1 and os.path.exists(self._legacy_path):
            try:
                with open(self._legacy_path, "r", encoding="utf-8") as f:
                    rows = json.load(f) or []
                self._pending_migration = True
            except Exception as e:
                self.persist_errors += 1
                print(f"[警告] 旧版历史迁移失败：{e}")

        self._levels[0] = rows
        self._l0_last_written_round = max(
            [int(r["round"]) for r in rows if r.get("round") is not None]
            or [self._archive_max_round]
        )

    # ===================== 写入 =====================
    def append(self, row: dict) -> None:
        """追加一行到 L0，并触发超容折叠。每回合调用一次。"""
        self._levels[0].append(row)
        self._compress_levels()

    def maybe_persist(self, round_no: int) -> None:
        """每 N 回合持久化一次：L0 追加写（O(新增)），归档低频整体写。"""
        if round_no % self._persist_every != 0:
            return
        try:
            os.makedirs(self._history_dir, exist_ok=True)

            # 1) L0 只追加新增行
            pending = [r for r in self._levels[0]
                       if int(r.get("round", -1)) > self._l0_last_written_round]
            if pending:
                # 只 flush 不 fsync：每 10 回合一次强制刷盘会让持久化占掉半数运行时间
                # （实测 5000 回合中 1.7s/3.1s）。崩溃时仅丢最后一批，进程崩溃由 OS 保证。
                with open(self._l0_path, "a", encoding="utf-8") as f:
                    for r in pending:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    f.flush()
                self._l0_last_written_round = int(pending[-1]["round"])

            # 2) 归档层落盘 + 压缩 L0（低频：每累计 _ARCHIVE_WRITE_EVERY 次折叠）
            if self._folds_since_archive_write >= _ARCHIVE_WRITE_EVERY:
                self.write_archive()
                self.compact_l0()
                self._folds_since_archive_write = 0

            # 3) 旧版文件迁移收尾（数据已安全写入新格式后才改名）
            if self._pending_migration:
                try:
                    os.replace(self._legacy_path, self._legacy_path + ".migrated")
                except Exception:
                    pass
                self._pending_migration = False
        except Exception as e:
            self.persist_errors += 1
            print(f"[警告] 历史存盘失败：{e}")

    # --- 原子写：先写临时文件再 os.replace，避免写一半崩溃留下损坏文件 ---
    @staticmethod
    def _atomic_write(path: str, write_fn) -> None:
        tmp = path + ".tmp"
        write_fn(tmp)
        os.replace(tmp, path)

    def write_archive(self) -> None:
        data = {"max_round": self._archive_max_round, "levels": self._levels[1:]}

        def _w(tmp):
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
                f.flush()

        self._atomic_write(self._archive_path, _w)

    def compact_l0(self) -> None:
        """丢弃已被归档覆盖的 L0 行。

        只能在归档落盘之后调用：判断依据是 round > _archive_max_round，
        而该值已随归档持久化，因此崩溃后重新加载也不会丢行。
        """
        keep = [r for r in self._levels[0]
                if int(r.get("round", -1)) > self._archive_max_round]

        def _w(tmp):
            with open(tmp, "w", encoding="utf-8") as f:
                for r in keep:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()

        self._atomic_write(self._l0_path, _w)

    def _compress_levels(self) -> None:
        """各层超容时把最老的 _FOLD 行折叠进下一层（可连锁）。"""
        k = 0
        while k < len(self._levels):
            cap = _L0_CAPACITY if k == 0 else _LEVEL_CAPACITY
            lvl = self._levels[k]
            if len(lvl) <= cap:
                k += 1
                continue
            # 不能折叠尚未落盘的 L0 行，否则崩溃后会丢失
            if k == 0 and int(lvl[0].get("round", -1)) > self._l0_last_written_round:
                break
            group = lvl[:_FOLD]
            del lvl[:_FOLD]
            if k + 1 == len(self._levels):
                self._levels.append([])
            self._levels[k + 1].append(aggregate_rows(group))
            if k == 0:
                # 只有折叠 L0 才会推进归档覆盖范围（折叠更高层涉及的是更老的回合）
                self._archive_max_round = max(self._archive_max_round,
                                               int(group[-1]["round"]))
            self._folds_since_archive_write += 1
            k += 1

    # ===================== 查询 / 清空 =====================
    def get_history(self, start: int = 0, end: int | None = None) -> list[dict]:
        """返回历史曲线数据切片（跨归档层与 L0，按回合升序）。"""
        hist = self.history
        if end is None:
            return [h for h in hist if h["round"] >= start]
        return [h for h in hist if start <= h["round"] <= end]

    def clear(self) -> None:
        """清空历史（reset_round 时调用）。"""
        self._levels = [[]]
        self._archive_max_round = -1
        self._l0_last_written_round = -1
        self._folds_since_archive_write = 0
        for p in (self._l0_path, self._archive_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                self.persist_errors += 1
                print(f"[警告] 历史文件清理失败：{e}")
