"""经济模拟引擎包。

对外 API 与原 engine.py 完全一致（含测试/基准依赖的私有符号），
内部按职责拆分为五层：

  A  model.py      数据结构 + 规范化 + 格式化/统计工具（零依赖）
  B  history.py    历史曲线的分层降采样持久化（HistoryStore）
  C  metrics.py    回合经济指标计算（纯函数 compute_metrics）
  F  config_io.py  个体/基础设置的 JSON 序列化与导入导出
  core.py          Simulation（D 交换撮合 + E 回合流程）

D/E 之所以仍合在 core.py：撮合与回合流程共享 res_delta 账本、成交聚合暂存、
verbose 日志渲染等紧密上下文，强行拆分收益低而风险高（详见 README「引擎拆分」）。
"""
from __future__ import annotations

# A：数据结构 + 工具
from .model import (
    DefaultSettings, Government, Metabolism, Need, Person, Rule,
    fmt_num, get_base_name, gini, norm_ask, norm_metabolism,
)
# B：历史持久化
from .history import (
    HistoryStore, aggregate_rows,
    _ARCHIVE_WRITE_EVERY, _FOLD, _L0_CAPACITY, _LEVEL_CAPACITY,
    _MEAN_METRICS, _SUM_METRICS, _mean_or_none,
)
# C：指标计算
from .metrics import _GINI_SAMPLE_SIZE, compute_metrics
# F：序列化
from .config_io import (
    export_defaults, export_persons, import_defaults, import_persons,
    load_gov_tax_rate, _person_from_dict, _person_to_dict,
)
# 核心
from .core import Simulation, _MAX_CLEARING_ROUNDS, _TRADE_EPS

__all__ = [
    # 核心
    "Simulation",
    # A
    "DefaultSettings", "Government", "Metabolism", "Need", "Person", "Rule",
    "norm_ask", "norm_metabolism", "fmt_num", "get_base_name", "gini",
    # B
    "HistoryStore", "aggregate_rows",
    "_L0_CAPACITY", "_LEVEL_CAPACITY", "_FOLD", "_ARCHIVE_WRITE_EVERY",
    "_SUM_METRICS", "_MEAN_METRICS",
    # C
    "compute_metrics",
    # F
    "export_persons", "import_persons", "export_defaults", "import_defaults",
    "load_gov_tax_rate",
]
