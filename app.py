"""经济系统 HTTP 服务（基于 Python 标准库 http.server，无第三方依赖）。

启动：python app.py  （默认 127.0.0.1:8000）
浏览器访问：http://127.0.0.1:8000/   （自动打开 economy.html）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# Windows 控制台默认 GBK(936) 代码页，而 PyPy/UTF-8 模式下 print 中文走 UTF-8 字节，
# 两者不匹配会导致启动提示乱码。这里显式锁死 stdout 为 UTF-8（配合启动脚本 chcp 65001）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine import (
    Simulation, DefaultSettings, Person, Metabolism, Need, Rule, Government,
    norm_ask, norm_metabolism, export_persons, export_defaults, get_base_name,
)

# ===================== 全局引擎 =====================
sim = Simulation()
if os.path.exists("config"):
    sim.reset_round()


# ===================== 序列化辅助 =====================
def person_to_dict(p: Person) -> dict:
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


def defaults_to_dict(d: DefaultSettings) -> dict:
    return {
        "metabolism": {"res": d.metabolism.res, "amount": d.metabolism.amount},
        "income": {"res": d.income.res, "amount": d.income.amount},
        "canReproduce": d.canReproduce,
        "reproThresholdMult": d.reproThresholdMult,
        "reproInheritMult": d.reproInheritMult,
        "reproInheritRatio": d.reproInheritRatio,
        "weanMinRounds": d.weanMinRounds,
        "weanMaxRounds": d.weanMaxRounds,
        "attrs": dict(d.attrs),
        "needs": [{"key": n.key, "amount": n.amount} for n in d.needs],
        "rules": [{"sell": r.sell, "buy": r.buy, "rate": r.rate} for r in d.rules],
        "perishable_resources": list(d.perishable_resources),
        "adaptive_pricing": d.adaptive_pricing,
        "price_adjust_alpha": d.price_adjust_alpha,
        "price_index_numeraire": d.price_index_numeraire,
        "max_population": d.max_population,
    }


def government_to_dict(g: Government) -> dict:
    return {
        "tax_rate": g.tax_rate,
        "treasury": dict(g.treasury),
        "total_collected": dict(g.total_collected),
    }


def person_from_dict(d: dict) -> Person:
    m = d.get("metabolism") or {}
    inc = d.get("income") or {}
    return Person(
        id=int(d.get("id", 0)),
        parentId=d.get("parentId"),
        name=d.get("name") or "未命名",
        metabolism=Metabolism(m.get("res", "食物"), float(m.get("amount", 1))),
        income=Metabolism(inc.get("res", "钱"), float(inc.get("amount", 1))),
        canReproduce=d.get("canReproduce", True),
        reproThresholdMult=float(d.get("reproThresholdMult", 2.0)),
        reproInheritMult=float(d.get("reproInheritMult", 1.0)),
        reproInheritRatio=float(d.get("reproInheritRatio", 0.5)),
        attrs={k: float(v) for k, v in (d.get("attrs") or {}).items()},
        needs=[Need(n["key"], float(n["amount"])) for n in (d.get("needs") or [])],
        rules=[norm_ask(r) for r in (d.get("rules") or [])],
        birthRound=(int(d["birthRound"]) if d.get("birthRound") is not None else None),
        dependent=bool(d.get("dependent", True)),
        weanMinRounds=(int(d["weanMinRounds"]) if d.get("weanMinRounds") is not None else None),
        weanMaxRounds=(int(d["weanMaxRounds"]) if d.get("weanMaxRounds") is not None else None),
    )


def defaults_from_dict(d: dict) -> DefaultSettings:
    m = d.get("metabolism") or {}
    inc = d.get("income") or {}
    return DefaultSettings(
        metabolism=Metabolism(m.get("res", "食物"), float(m.get("amount", 1))),
        income=Metabolism(inc.get("res", "钱"), float(inc.get("amount", 1))),
        canReproduce=d.get("canReproduce", True),
        reproThresholdMult=float(d.get("reproThresholdMult", 2.0)),
        reproInheritMult=float(d.get("reproInheritMult", 1.0)),
        reproInheritRatio=float(d.get("reproInheritRatio", 0.5)),
        weanMinRounds=int(d.get("weanMinRounds", 3)),
        weanMaxRounds=int(d.get("weanMaxRounds", 8)),
        attrs={k: float(v) for k, v in (d.get("attrs") or {}).items()},
        needs=[Need(n["key"], float(n["amount"])) for n in (d.get("needs") or [])],
        rules=[norm_ask(r) for r in (d.get("rules") or [])],
        perishable_resources=[str(r) for r in (d.get("perishable_resources") or [])],
        adaptive_pricing=bool(d.get("adaptive_pricing", False)),
        price_adjust_alpha=float(d.get("price_adjust_alpha", 0.1)),
        price_index_numeraire=str(d.get("price_index_numeraire", "钱")),
        max_population=int(d.get("max_population", 300000)),
    )


class HttpError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(msg)


# ===================== HTTP Handler =====================
class Handler(BaseHTTPRequestHandler):
    # 关闭 stdout 日志刷屏（保留错误日志）
    def log_message(self, fmt, *args):
        pass

    # ----- 通用响应 -----
    def _json(self, obj: dict, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, data: bytes, filename: str, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path: str):
        """静态文件服务（仅项目根目录文件）。"""
        if not os.path.exists(path) or not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        ct = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise HttpError(400, f"JSON 解析失败：{e}")

    def _handle(self, method: str):
        """统一路由分发。"""
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            # CORS 预检
            if method == "OPTIONS":
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                return

            # 静态文件路由表
            STATIC_MAP = {
                "/": "index.html",
                "/index.html": "index.html",
                "/economy": "economy.html",
                "/economy.html": "economy.html",
                "/common.css": "common.css",
                "/common.js": "common.js",
                "/echarts.min.js": os.path.join("product-launch", "_shared", "js", "echarts.min.js"),
            }
            if method == "GET" and path in STATIC_MAP:
                self._static(STATIC_MAP[path])
                return

            # === API 路由 ===
            if method == "GET" and path == "/state":
                # 注意：不再回传全量 persons（N 大时单响应上百 MB）。
                # 个体列表走 /persons 分页，个体详情走 GET /person/{id}。
                self._json({
                    "round": sim.round,
                    "defaults": defaults_to_dict(sim.defaults),
                    "summary": sim.summary(),
                    "government": government_to_dict(sim.government),
                })
                return

            if method == "GET" and path == "/defaults":
                self._json(defaults_to_dict(sim.defaults))
                return

            # 个体列表：搜索 / 类型筛选 / 分页全部在服务端完成，只回传当前页
            if method == "GET" and path == "/persons":
                q = (query.get("q", [""])[0] or "").strip().lower()
                typ = (query.get("type", [""])[0] or "").strip()
                offset = max(0, int(query.get("offset", ["0"])[0]))
                limit = min(500, max(1, int(query.get("limit", ["50"])[0])))
                matched = []
                for p in sim.persons:
                    if typ and get_base_name(p.name) != typ:
                        continue
                    if q and (q not in p.name.lower()) and (q not in str(p.id)):
                        continue
                    matched.append(p)
                self._json({
                    "total": len(sim.persons),
                    "total_filtered": len(matched),
                    "offset": offset,
                    "limit": limit,
                    "items": [person_to_dict(p) for p in matched[offset:offset + limit]],
                })
                return

            # 历史曲线数据（持久化 + 历史曲线图用）
            if method == "GET" and path == "/history":
                start = int(query.get("from", ["0"])[0])
                end_q = query.get("to", [None])[0]
                end = int(end_q) if end_q else None
                self._json({"history": sim.get_history(start, end)})
                return

            if method == "POST" and path == "/clear-history":
                sim.clear_history()
                self._json({"ok": True})
                return

            if method == "PUT" and path == "/defaults":
                body = self._read_body()
                sim.defaults = defaults_from_dict(body)
                self._json({"ok": True, "defaults": defaults_to_dict(sim.defaults)})
                return

            if method == "PUT" and path == "/government":
                body = self._read_body()
                rate = body.get("tax_rate")
                if rate is None:
                    raise HttpError(400, "缺少 tax_rate 字段")
                rate = float(rate)
                if rate < 0 or rate > 1:
                    raise HttpError(400, "税率必须在 0~1 之间")
                sim.government.tax_rate = rate
                self._json({"ok": True, "government": government_to_dict(sim.government)})
                return

            if method == "POST" and path == "/person":
                name = query.get("name", [None])[0]
                p = sim.add_person(name)
                self._json({"ok": True, "person": person_to_dict(p)})
                return

            m = re_match(r"^/person/(\d+)/?$", path)
            if m:
                pid = int(m.group(1))
                if method == "GET":
                    p = sim.find(pid)
                    if not p:
                        raise HttpError(404, "个体不存在")
                    parent = sim.find(p.parentId) if p.parentId else None
                    self._json({
                        "person": person_to_dict(p),
                        "parentName": parent.name if parent else None,
                        "round": sim.round,
                    })
                    return
                if method == "DELETE":
                    sim.del_person(pid)
                    self._json({"ok": True})
                    return
                if method == "PUT":
                    body = self._read_body()
                    p = sim.find(pid)
                    if not p:
                        raise HttpError(404, "个体不存在")
                    new_p = person_from_dict(body)
                    new_p.id = pid
                    idx = sim.persons.index(p)
                    sim.persons[idx] = new_p
                    self._json({"ok": True, "person": person_to_dict(new_p)})
                    return

            if method == "POST" and path == "/next-round":
                log = sim.next_round()
                self._json({"log": log, "summary": sim.summary(), "government": government_to_dict(sim.government), "duration_ms": sim.last_elapsed_ms})
                return

            if method == "POST" and path == "/calculate":
                log = sim.calculate()
                self._json({"log": log, "summary": sim.summary(), "government": government_to_dict(sim.government), "duration_ms": sim.last_elapsed_ms})
                return

            if method == "POST" and path == "/next-and-calc":
                log = sim.next_round_and_calculate()
                self._json({"log": log, "summary": sim.summary(), "government": government_to_dict(sim.government), "duration_ms": sim.last_elapsed_ms})
                return

            if method == "POST" and path in ("/reset", "/load-config-folder"):
                log = sim.reset_round()
                self._json({
                    "log": log,
                    "summary": sim.summary(),
                    "defaults": defaults_to_dict(sim.defaults),
                    "government": government_to_dict(sim.government),
                })
                return

            if method == "GET" and path == "/export-persons":
                fd, tmp = tempfile.mkstemp(suffix=".json")
                os.close(fd)
                export_persons(tmp, sim.persons)
                with open(tmp, "rb") as f:
                    data = f.read()
                os.unlink(tmp)
                self._file(data, "economy_persons.json", "application/json; charset=utf-8")
                return

            if method == "POST" and path == "/import-persons":
                body = self._read_body()
                # body 可能是 {"persons":[...]} 完整文件，或直接 [{...}, ...]
                src = body if isinstance(body, dict) else {"persons": body}
                sim.persons = [person_from_dict(p) for p in src.get("persons", [])]
                sim.next_id = max((p.id for p in sim.persons), default=0) + 1
                sim._fill_missing_birth_round()
                warning = None
                cap = sim.defaults.max_population
                if cap > 0 and len(sim.persons) > cap:
                    warning = f"导入后个体数 {len(sim.persons)} 已超上限 {cap}，将阻止后续繁殖与新增"
                self._json({"ok": True, "count": len(sim.persons), "warning": warning})
                return

            if method == "GET" and path == "/export-defaults":
                fd, tmp = tempfile.mkstemp(suffix=".json")
                os.close(fd)
                export_defaults(tmp, sim.defaults, sim.government)
                with open(tmp, "rb") as f:
                    data = f.read()
                os.unlink(tmp)
                self._file(data, "economy_defaults.json", "application/json; charset=utf-8")
                return

            if method == "POST" and path == "/import-defaults":
                body = self._read_body()
                src = body.get("defaultSettings") if isinstance(body, dict) else body
                if src is None:
                    src = body
                sim.defaults = defaults_from_dict(src)
                self._json({"ok": True, "defaults": defaults_to_dict(sim.defaults)})
                return

            self.send_error(404, f"Not Found: {method} {path}")

        except HttpError as e:
            self._json({"detail": e.msg}, code=e.code)
        except ValueError as e:
            # 引擎层抛出的业务错误（如"无个体"）
            self._json({"detail": str(e)}, code=400)
        except Exception as e:
            self._json({"detail": f"服务器错误：{e}"}, code=500)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self._handle("OPTIONS")


def re_match(pattern: str, path: str):
    """简化正则匹配，避免每次都 import re。"""
    import re
    return re.match(pattern, path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    host = "127.0.0.1"
    server = HTTPServer((host, port), Handler)
    print(f"经济系统服务已启动：http://{host}:{port}/")
    print(f"按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()
