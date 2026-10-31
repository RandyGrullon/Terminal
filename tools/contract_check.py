"""Contract checker: calls every REST endpoint of a running NEXUS server and compares the JSON responses with
the TypeScript interfaces in frontend/src/api/types.ts and the feature files next to it (types-*.ts, re-exported by
types.ts): missing keys, unexpected keys, NaN, HTTP errors.

Read-only on purpose: it never calls POST /api/alerts/test (it would message the user's Telegram or email), POST
/api/validation/run (a background job that downloads data) nor places or cancels demo orders.

Usage (server must be running):
    backend/.venv/Scripts/python.exe tools/contract_check.py [--base http://127.0.0.1:8420] [--symbol BINANCE:BTCUSDT]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
TYPES_TS = ROOT / "frontend" / "src" / "api" / "types.ts"
# Response types that still live next to their UI (used only when src/api does not define them yet)
UI_TYPE_FILES = {"SkillReport": ROOT / "frontend" / "src" / "components" / "brain" / "skill.ts"}
# GET /api/macro/calendar `coverage` (documented keys; used while src/api has no MacroCalendarCoverage type)
CALENDAR_COVERAGE_KEYS = ("weekly_until", "weekly_stale", "verified", "sources")
CALENDAR_SOURCE_KEYS = ("id", "label", "live", "ok", "updated_at", "until", "error")


# ----------------------------------------------------------------------------- types.ts parser


def parse_interfaces(src: str) -> dict[str, dict[str, bool]]:
    """interface name -> {field: optional}. Handles `extends` and one-level fields (nested inline types skipped)."""
    out: dict[str, dict[str, bool]] = {}
    parents: dict[str, list[str]] = {}
    for m in re.finditer(r"export interface (\w+)(?:\s+extends\s+([\w\s,]+))?\s*\{", src):
        name = m.group(1)
        start = m.end()
        depth, i = 1, start
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        body = src[start : i - 1]
        fields: dict[str, bool] = {}
        level = 0
        for line in body.splitlines():
            stripped = line.strip()
            if level == 0:
                fm = re.match(r"^(\w+)(\?)?\s*:", stripped)
                if fm:
                    fields[fm.group(1)] = bool(fm.group(2))
            level += stripped.count("{") + stripped.count("[{") * 0 - stripped.count("}")
            level = max(level, 0)
        out[name] = fields
        if m.group(2):
            parents[name] = [p.strip() for p in m.group(2).split(",") if p.strip()]
    for name, ps in parents.items():
        merged: dict[str, bool] = {}
        for p in ps:
            merged.update(out.get(p, {}))
        merged.update(out[name])
        out[name] = merged
    return out


def type_files(types_ts: Path = TYPES_TS) -> list[Path]:
    """types.ts and its feature files (types-*.ts in the same folder), in a stable order."""
    return [types_ts, *sorted(types_ts.parent.glob("types-*.ts"))]


def load_interfaces(types_ts: Path = TYPES_TS) -> tuple[dict[str, dict[str, bool]], list[str]]:
    """(interfaces of src/api, notes). A response type still defined only next to its UI (UI_TYPE_FILES) is taken
    from there and named in the notes, so its endpoint is checked anyway."""
    ifaces = parse_interfaces("\n".join(p.read_text(encoding="utf-8") for p in type_files(types_ts)))
    notes = []
    for name, path in UI_TYPE_FILES.items():
        if name in ifaces or not path.is_file():
            continue
        extra = parse_interfaces(path.read_text(encoding="utf-8"))
        added = sorted(k for k in extra if k not in ifaces)
        if name in extra:
            ifaces.update({k: extra[k] for k in added})
            try:
                where = path.relative_to(ROOT).as_posix()
            except ValueError:
                where = path.as_posix()
            notes.append(f"{name} (y {len(added) - 1} tipos más) aún en {where}, no en src/api (regla 6: tipos "
                         "canónicos en types.ts)")
    return ifaces, notes


# ----------------------------------------------------------------------------- checks


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, where: str, detail: str = "") -> None:
        self.rows.append((status, where, detail))

    def print(self) -> int:
        bad = 0
        for status, where, detail in self.rows:
            color = {"OK": "\033[92m", "WARN": "\033[93m", "FAIL": "\033[91m"}.get(status, "")
            print(f"{color}{status:<4}\033[0m {where} {detail}")
            bad += status == "FAIL"
        warns = sum(1 for r in self.rows if r[0] == "WARN")
        print(f"\n{len(self.rows)} comprobaciones · {bad} fallos · {warns} avisos")
        return bad


def check_obj(rep: Report, ifaces: dict, iface: str, obj: Any, where: str) -> None:
    fields = ifaces.get(iface)
    if fields is None:
        rep.add("WARN", where, f"interfaz desconocida {iface}")
        return
    if not isinstance(obj, dict):
        rep.add("FAIL", where, f"se esperaba objeto {iface}, llegó {type(obj).__name__}")
        return
    missing = [k for k, opt in fields.items() if not opt and k not in obj]
    extra = [k for k in obj if k not in fields]
    if missing:
        rep.add("FAIL", where, f"{iface}: faltan {missing}")
    elif extra:
        rep.add("WARN", where, f"{iface}: claves extra {extra}")
    else:
        rep.add("OK", where, iface)


def check_list(rep: Report, ifaces: dict, iface: str, arr: Any, where: str, sample: int = 3) -> None:
    if not isinstance(arr, list):
        rep.add("FAIL", where, f"se esperaba lista de {iface}, llegó {type(arr).__name__}")
        return
    if not arr:
        rep.add("WARN", where, f"lista vacía ({iface})")
        return
    for i, item in enumerate(arr[:sample]):
        check_obj(rep, ifaces, iface, item, f"{where}[{i}]")


def check_all(rep: Report, ifaces: dict, iface: str, arr: Any, where: str, group: str | None = None) -> None:
    """Every item of a list against `iface` (keys exactly: missing = FAIL, extra = WARN), one row per distinct
    result; `group` names the field that tells the items apart in that row (e.g. the event's source)."""
    if not isinstance(arr, list):
        rep.add("FAIL", where, f"se esperaba lista de {iface}, llegó {type(arr).__name__}")
        return
    if not arr:
        rep.add("WARN", where, f"lista vacía ({iface})")
        return
    seen: dict[tuple[str, str], tuple[list[int], list[str]]] = {}
    for item in arr:
        tmp = Report()
        check_obj(tmp, ifaces, iface, item, "")
        status, _, detail = tmp.rows[0]
        label = str(item.get(group)) if group and isinstance(item, dict) else ""
        count, names = seen.setdefault((status, detail), ([0], []))
        count[0] += 1
        if label and label not in names:
            names.append(label)
    for (status, detail), (count, names) in seen.items():
        rep.add(status, f"{where} ({count[0]} de {len(arr)}" + (f": {', '.join(names)}" if names else "") + ")",
                detail)


def check_keys(rep: Report, name: str, keys: tuple[str, ...], obj: Any, where: str) -> None:
    """Like check_obj for a documented shape without a TypeScript interface (every key required)."""
    check_obj(rep, {name: {k: False for k in keys}}, name, obj, where)


def no_nan(text: str) -> bool:
    try:
        json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        return True
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8420")
    ap.add_argument("--symbol", default="BINANCE:BTCUSDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--skip-slow", action="store_true")
    args = ap.parse_args()

    ifaces, notes = load_interfaces()
    rep = Report()
    for note in notes:
        rep.add("WARN", "types", note)
    client = httpx.Client(base_url=args.base, timeout=180)
    sym, tf = args.symbol, args.tf

    def call(method: str, path: str, *, params: dict | None = None, body: Any = None, expect: int = 200) -> Any:
        t0 = time.perf_counter()
        try:
            r = client.request(method, path, params=params, json=body)
        except Exception as exc:
            rep.add("FAIL", f"{method} {path}", f"sin respuesta: {exc}")
            return None
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != expect:
            rep.add("FAIL", f"{method} {path}", f"HTTP {r.status_code} ({ms:.0f} ms): {r.text[:300]}")
            return None
        if not no_nan(r.text):
            rep.add("FAIL", f"{method} {path}", "contiene NaN/Infinity")
        if ms > 15000:
            rep.add("WARN", f"{method} {path}", f"lento: {ms:.0f} ms")
        return r.json()

    d = call("GET", "/api/health")
    if d:
        check_obj(rep, ifaces, "Health", d, "health")
        if d.get("load_errors"):
            rep.add("FAIL", "health.load_errors", str(d["load_errors"]))
    d = call("GET", "/api/settings")
    d = call("GET", "/api/events", params={"limit": 20})
    if d:
        check_list(rep, ifaces, "NexusEvent", d.get("events"), "events")
    d = call("GET", "/api/terminal/commands")
    if d:
        check_list(rep, ifaces, "CommandInfo", d.get("commands"), "commands", 2)
    d = call("GET", "/api/db/stats")
    if d:
        check_obj(rep, ifaces, "DbStats", d, "db/stats")

    # external alerts (status only: POST /api/alerts/test would message the user) and validation (no run: it is a
    # background job that downloads data)
    d = call("GET", "/api/alerts/status")
    if d:
        check_obj(rep, ifaces, "AlertsStatus", d, "alerts.status")
        check_list(rep, ifaces, "AlertChannelStatus", d.get("channels"), "alerts.channels", 2)
    d = call("GET", "/api/validation/status")
    if d:
        check_obj(rep, ifaces, "ValidationStatus", d, "validation.status")
        if d.get("job"):
            check_obj(rep, ifaces, "ValidationJob", d["job"], "validation.status.job")
    d = call("GET", "/api/validation/latest")
    if d:
        check_obj(rep, ifaces, "ValidationLatestResponse", d, "validation.latest")
        if d.get("report"):
            check_obj(rep, ifaces, "ValidationReport", d["report"], "validation.latest.report")
            check_obj(rep, ifaces, "ValidationSummary", d["report"].get("summary"), "validation.latest.summary")
        else:
            rep.add("WARN", "validation.latest", "sin informe guardado todavía (report = null)")

    # market
    d = call("GET", "/api/markets")
    if d:
        check_list(rep, ifaces, "MarketInfo", d.get("markets"), "markets", 2)
    d = call("GET", "/api/symbols/search", params={"q": "btc", "limit": 10})
    if d:
        check_list(rep, ifaces, "SearchResult", d.get("results"), "search")
        top = (d.get("results") or [{}])[0].get("symbol")
        rep.add("OK" if top and "BTC" in top and "_" not in top else "WARN", "search.top", str(top))
    d = call("GET", "/api/universe", params={"market": "crypto", "limit": 5})
    if d:
        check_obj(rep, ifaces, "UniverseResponse", d, "universe")
        check_list(rep, ifaces, "UniverseRow", d.get("rows"), "universe.rows", 2)
    for s in (sym, "SIM:NEXUS"):
        d = call("GET", "/api/candles", params={"symbol": s, "tf": tf, "limit": 300})
        if d:
            check_obj(rep, ifaces, "CandlesResponse", d, f"candles {s}")
            check_list(rep, ifaces, "Candle", d.get("candles"), f"candles {s}.candles", 1)
    d = call("GET", "/api/quotes", params={"symbols": sym})
    if d:
        q = (d.get("quotes") or {}).get(sym)
        check_obj(rep, ifaces, "Quote", q, "quotes") if q else rep.add("WARN", "quotes", "sin cotización")
    d = call("GET", "/api/symbol", params={"symbol": sym})
    if d:
        check_obj(rep, ifaces, "SymbolDetail", d, "symbol")
    d = call("GET", "/api/watchlist")
    if d:
        check_list(rep, ifaces, "WatchItem", d.get("items"), "watchlist", 2)
    d = call("GET", "/api/realtime")
    if d:
        check_obj(rep, ifaces, "RealtimeStatus", d, "realtime")
        check_list(rep, ifaces, "RealtimeProvider", d.get("providers"), "realtime.providers")
        if d.get("symbols"):
            check_list(rep, ifaces, "RealtimeSymbol", d["symbols"], "realtime.symbols", 2)
    d = call("GET", "/api/macro/calendar", params={"days": 14})
    if d:
        if "MacroCalendarResponse" in ifaces:
            check_obj(rep, ifaces, "MacroCalendarResponse", d, "macro.calendar")
        check_all(rep, ifaces, "MacroEvent", d.get("events"), "macro.calendar.events", group="source")
        cov = d.get("coverage")
        if cov is not None:
            if "MacroCalendarCoverage" in ifaces:
                check_obj(rep, ifaces, "MacroCalendarCoverage", cov, "macro.calendar.coverage")
            else:
                check_keys(rep, "CalendarCoverage", CALENDAR_COVERAGE_KEYS, cov, "macro.calendar.coverage")
            sources = cov.get("sources") if isinstance(cov, dict) else None
            if "MacroCalendarSource" in ifaces:
                check_all(rep, ifaces, "MacroCalendarSource", sources, "macro.calendar.coverage.sources", group="id")
            else:
                check_all(rep, {"CalendarSource": {k: False for k in CALENDAR_SOURCE_KEYS}}, "CalendarSource",
                          sources, "macro.calendar.coverage.sources", group="id")

    # nexus / gate / indicators
    for s in (sym, "SIM:NEXUS"):
        d = call("GET", "/api/nexus", params={"symbol": s, "tf": tf})
        if not d:
            continue
        w = f"nexus {s}"
        check_obj(rep, ifaces, "NexusResponse", d, w)
        if d.get("ok"):
            check_obj(rep, ifaces, "NexusBars", d.get("bars"), w + ".bars")
            bars = d.get("bars") or {}
            n = len(bars.get("time") or [])
            lens = {k: len(v) for k, v in bars.items() if isinstance(v, list)}
            rep.add("OK" if all(v == n for v in lens.values()) else "FAIL", w + ".bars.lengths", str(lens))
            check_list(rep, ifaces, "ChartMarker", d.get("markers"), w + ".markers", 2)
            check_list(rep, ifaces, "Trade", d.get("trades"), w + ".trades", 2)
            cur = d.get("current")
            check_obj(rep, ifaces, "NexusCurrent", cur, w + ".current")
            if isinstance(cur, dict):
                for side in ("long", "short"):
                    g = (cur.get("gate") or {}).get(side)
                    check_obj(rep, ifaces, "GateResult", g, f"{w}.current.gate.{side}")
                    if isinstance(g, dict):
                        check_list(rep, ifaces, "GateCheck", g.get("checks"), f"{w}.gate.{side}.checks", 2)
                if cur.get("position"):
                    check_obj(rep, ifaces, "PositionState", cur["position"], w + ".current.position")
                check_list(rep, ifaces, "Contribution", cur.get("contributions"), w + ".contributions", 1)
            if d.get("prediction"):
                check_obj(rep, ifaces, "Prediction", d["prediction"], w + ".prediction")
                check_list(rep, ifaces, "ConePoint", d["prediction"].get("cone"), w + ".cone", 1)
            check_obj(rep, ifaces, "NexusLearning", d.get("learning"), w + ".learning")
            check_obj(rep, ifaces, "NexusStats", d.get("stats"), w + ".stats")
        else:
            rep.add("WARN", w, f"ok=false: {d.get('message')}")
            if d.get("quick"):
                check_obj(rep, ifaces, "QuickSignal", d["quick"], w + ".quick")
    for direction in ("long", "short"):
        d = call("GET", "/api/gate/check", params={"symbol": sym, "tf": tf, "direction": direction})
        if d:
            check_obj(rep, ifaces, "GateResult", d, f"gate {direction}")
    d = call("GET", "/api/nexus/model", params={"symbol": "SIM:NEXUS", "tf": tf})
    if d:
        check_obj(rep, ifaces, "NexusModel", d, "nexus/model")
    d = call("GET", "/api/indicators/catalog")
    if d:
        cat = d.get("indicators") or []
        check_list(rep, ifaces, "IndicatorSpec", cat, "indicators.catalog", 2)
        rep.add("OK" if len(cat) >= 70 else "FAIL", "indicators.count", str(len(cat)))
        items = [{"uid": f"u{i}", "id": spec["id"], "params": {}} for i, spec in enumerate(cat)]
        d2 = call("POST", "/api/indicators/compute", body={"symbol": "SIM:NEXUS", "tf": tf, "limit": 500, "items": items})
        if d2:
            check_obj(rep, ifaces, "IndicatorComputeResponse", d2, "indicators.compute")
            errs = [r["id"] for r in d2.get("results", []) if r.get("error")]
            rep.add("OK" if not errs else "FAIL", "indicators.compute.errors", str(errs[:20]))

    # sim / paper / scanner / learning
    if not args.skip_slow:
        d = call("POST", "/api/backtest", body={"symbol": "SIM:NEXUS", "tf": tf, "limit": 1000})
        if d:
            check_obj(rep, ifaces, "BacktestResult", d, "backtest")
            check_obj(rep, ifaces, "BacktestMetrics", d.get("metrics"), "backtest.metrics")
            if d.get("ungated"):
                check_obj(rep, ifaces, "BacktestMetrics", d["ungated"], "backtest.ungated")
            check_list(rep, ifaces, "EquityPoint", d.get("equity"), "backtest.equity", 1)
            check_list(rep, ifaces, "Trade", d.get("trades"), "backtest.trades", 1)
        d = call("POST", "/api/montecarlo", body={"mode": "synthetic", "paths": 12, "bars": 300, "seed": 3})
        if d:
            check_obj(rep, ifaces, "MonteCarloResult", d, "montecarlo")
    d = call("GET", "/api/sim/state")
    if d:
        check_obj(rep, ifaces, "SimSnapshot", d, "sim/state")
        check_obj(rep, ifaces, "SimState", d.get("state"), "sim/state.state")
    d = call("GET", "/api/paper/account")
    if d:
        check_obj(rep, ifaces, "PaperSnapshot", d, "paper")
        check_obj(rep, ifaces, "PaperAccount", d.get("account"), "paper.account")
    d = call("GET", "/api/paper/orders", params={"status": "all", "limit": 20})  # pending + the latest closed ones
    if d:
        check_keys(rep, "PaperOrdersResponse", ("orders",), d, "paper.orders")
        check_list(rep, ifaces, "PaperOrder", d.get("orders"), "paper.orders.orders", 3)
    d = call("GET", "/api/paper/risk")
    if d:
        check_obj(rep, ifaces, "PaperRisk", d, "paper.risk")
        if d.get("groups"):
            check_list(rep, ifaces, "PaperRiskGroup", d["groups"], "paper.risk.groups", 2)
    d = call("GET", "/api/scanner/runs")
    if d:
        check_list(rep, ifaces, "ScanRun", d.get("runs"), "scanner.runs", 1)
    d = call("GET", "/api/scanner/results")
    if d:
        if d.get("run"):
            check_obj(rep, ifaces, "ScanRun", d["run"], "scanner.results.run")
        check_list(rep, ifaces, "ScanRow", d.get("results"), "scanner.results", 2)
    d = call("GET", "/api/scanner/quick", params={"symbol": sym, "tf": tf})
    if d:
        check_obj(rep, ifaces, "QuickSignal", d, "scanner.quick")
    d = call("GET", "/api/predictions", params={"limit": 20})
    if d:
        check_obj(rep, ifaces, "PredictionStats", d.get("stats"), "predictions.stats")
        check_list(rep, ifaces, "PredictionRow", d.get("predictions"), "predictions", 1)
    d = call("GET", "/api/learning/summary")
    if d:
        check_obj(rep, ifaces, "LearningSummary", d, "learning")
    for days in (7, 0):  # the UI asks for 7 days, 24 h or every stored row (days=0)
        d = call("GET", "/api/learning/skill", params={"days": days})
        if d:
            w = f"learning.skill days={days}"
            check_obj(rep, ifaces, "SkillReport", d, w)
            check_list(rep, ifaces, "SkillGroup", d.get("groups"), w + ".groups", 1)
            g = next((g for g in d.get("groups") or [] if isinstance(g, dict) and g.get("n")), None)
            if g is not None:
                if g.get("reference"):
                    check_obj(rep, ifaces, "SkillReference", g["reference"], w + ".reference")
                check_list(rep, ifaces, "SkillDirection", g.get("by_direction"), w + ".by_direction", 1)
                check_list(rep, ifaces, "SkillDaily", g.get("daily"), w + ".daily", 1)
    if call("GET", "/api/learning/skill", params={"source": "otra"}, expect=400) is not None:
        rep.add("OK", "learning.skill source=otra", "HTTP 400 (fuente no válida)")

    # scripts
    d = call("GET", "/api/scripts")
    if d:
        check_list(rep, ifaces, "ScriptMeta", d.get("scripts"), "scripts", 2)
        rep.add("OK" if len(d.get("scripts") or []) >= 8 else "FAIL", "scripts.examples", str(len(d.get("scripts") or [])))
    d = call("GET", "/api/scripts/reference")
    if d:
        check_obj(rep, ifaces, "ScriptReference", d, "scripts.reference")
    code = 'indicator("t", overlay=True)\nn = input.int(14, "n")\nplot(ta.ema(close, n), "ema")\nplotshape(ta.crossover(close, ta.ema(close, n)), "x", location="belowbar")\n'
    d = call("POST", "/api/scripts/compile", body={"code": code})
    if d:
        check_obj(rep, ifaces, "CompileResult", d, "scripts.compile")
    d = call("POST", "/api/scripts/run", body={"code": code, "symbol": "SIM:NEXUS", "tf": tf, "limit": 400})
    if d:
        check_obj(rep, ifaces, "ScriptRunResult", d, "scripts.run")
        check_list(rep, ifaces, "ScriptPlot", d.get("plots"), "scripts.run.plots", 1)
    d = call("POST", "/api/scripts/compile", body={"code": "import os\nos.system('echo hi')"})
    if d:
        rep.add("OK" if not d.get("ok") else "FAIL", "scripts.security", "import os rechazado" if not d.get("ok") else "¡import os aceptado!")

    # terminal
    for text in ("help", f"signal {sym}", f"check {sym} long", "status", "positions", "watchlist-signals", "alertas",
                 "orders", "risk", "skill"):
        d = call("POST", "/api/terminal", body={"text": text})
        if d:
            ok = d.get("ok")
            check_obj(rep, ifaces, "CommandResult", d, f"terminal '{text}'")
            if not ok:
                rep.add("FAIL", f"terminal '{text}'", " | ".join(l["text"] for l in d.get("lines", [])[:3]))
    return 1 if rep.print() else 0


if __name__ == "__main__":
    sys.exit(main())
