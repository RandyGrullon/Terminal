"""Validation feature, verifier checks: a job that validates nothing keeps the last good report, the data notes of the
hub (synthetic market, equivalent pair, saved data) reach the terminal, Spanish number agreement in the messages, and
the published walk-forward artefacts stay consistent with the code."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app.sim.common import fmt_num, fmt_r
from app.validation.runner import series_line
from app.validation.verdict import ci_text, count_text

QUICK = {"symbols": ["SIM:NEXUS"], "tfs": ["1h"], "bars": 1200}
DOCS = Path(__file__).resolve().parents[2] / "docs"


def wait_done(client, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get("/api/validation/status").json()
        if not st["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("la validación no terminó a tiempo")


def terminal(client, text: str) -> dict:
    return client.post("/api/terminal", json={"text": text}).json()


def test_a_job_that_validates_nothing_keeps_the_last_report(api_client):
    assert api_client.post("/api/validation/run", json=QUICK).status_code == 200
    first = wait_done(api_client)
    assert first["state"] == "done" and first["job"]["message"].startswith("Validación terminada: 1 serie ·")
    good = api_client.get("/api/validation/latest").json()["report"]
    assert [s["symbol"] for s in good["series"]] == ["SIM:NEXUS"]
    # the hub's data note travels with the series and the terminal says it next to the table
    assert good["series"][0]["note"] and "sintético" in good["series"][0]["note"]
    lines = [line["text"] for line in terminal(api_client, "validar resultado")["lines"]]
    assert any(t.startswith("Datos de SIM:NEXUS 1h: Mercado sintético") for t in lines), lines

    # offline and nothing cached: every series fails -> the job fails, the stored report does not change
    r = api_client.post("/api/validation/run", json={**QUICK, "symbols": ["BINANCE:NOPEUSDT"]})
    assert r.status_code == 200, r.text
    st = wait_done(api_client)
    assert st["state"] == "error" and st["job"]["done"] == st["job"]["total"] == 1
    assert "No se validó ninguna serie" in st["job"]["error"] and "BINANCE:NOPEUSDT" in st["job"]["error"]
    assert st["job"]["errors"] and st["job"]["errors"][0]["series"] == "BINANCE:NOPEUSDT 1h"
    assert api_client.get("/api/validation/latest").json()["report"] == good
    out = terminal(api_client, "validar estado")
    assert out["ok"] and "La última validación falló: No se validó ninguna serie" in out["lines"][0]["text"]
    events = api_client.get("/api/events", params={"source": "validation", "limit": 50}).json()["events"]
    msgs = [e["message"] for e in events]
    assert any("Validación iniciada: 1 símbolo × 1 temporalidad (1200 velas)" in m for m in msgs), msgs
    assert any("La validación falló: No se validó ninguna serie" in m for m in msgs)


def test_spanish_number_agreement_in_the_series_line():
    assert count_text(1, "serie", "series") == "1 serie" and count_text(0, "serie", "series") == "0 series"
    res = {"symbol": "X:Y", "tf": "1h", "verdict": {"label": "Sin evidencia de ventaja"},
           "overall": {"nexus": {"trades": 1, "expectancy_r": -1.19, "ci95": None},
                       "random": {"percentile": 52.0}}}
    assert "X:Y 1h: 1 operación · expectativa" in series_line(res)
    res["overall"]["nexus"]["trades"] = 7
    assert ": 7 operaciones ·" in series_line(res)


def test_published_report_is_consistent_with_its_documentation():
    """docs/validation/*.json is the compact copy of a real run and docs/VALIDATION.md quotes it: every headline number
    of the document is derived here from the newest JSON (not pinned), so a re-run after an engine change only passes
    once the document was rewritten with the new figures, and the conclusion always matches the report's verdict."""
    reports = [(json.loads(p.read_text(encoding="utf-8")), p) for p in (DOCS / "validation").glob("*.json")]
    assert reports, "falta el informe de la validación publicada"
    # the newest run by its own timestamp (older runs stay published for transparency, whatever their file names)
    reports.sort(key=lambda rp: (int(rp[0].get("generated_at") or 0), rp[1].name))
    rep, newest = reports[-1]
    files = [p for _, p in reports]
    assert files[-1] == newest
    doc = (DOCS / "VALIDATION.md").read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", doc.replace("\n> ", " "))  # one line: the quotes may wrap anywhere

    def r_forms(x: float) -> set[str]:
        """'−0,08R': the compact copy keeps 3 decimals, so the 2 the document shows may round either way."""
        return {fmt_r(x - 0.0005, 2), fmt_r(x + 0.0005, 2)}

    def quoted(template: str, *values: float) -> bool:
        options = [r_forms(v) for v in values]
        combos = [[]]
        for opts in options:
            combos = [c + [o] for c in combos for o in sorted(opts)]
        return any(template.format(*c) in flat for c in combos)

    series = rep["series"]
    assert rep["compact"] is True and series and files[-1].name in doc
    n = len(series)
    gated = sum(s["overall"]["nexus"]["trades"] for s in series)
    ungated = sum(s["overall"]["ungated"]["trades"] for s in series)
    longs, shorts = (sum(s["side_mix"][k] for s in series) for k in ("long", "short"))
    assert f"{fmt_num(gated, 0)} operaciones" in flat and f"{fmt_num(ungated, 0)} operaciones" in flat
    assert f"({longs} largos, {shorts} cortos)" in flat and longs + shorts == gated
    assert f"rechazó {fmt_num(sum(s['rejected_signals'] for s in series), 0)} señales" in flat
    top = rep["groups"][-1]
    assert top["trades"] == gated and top["series"] == n
    assert quoted("**{} por operación**", top["expectancy_r"])
    assert quoted("bloques de tiempo {} a {}", *top["ci95_blocks"])  # the pooled interval, by time blocks
    assert quoted("(IC 95 % {} a {})", *top["excess_ci95"])  # the excess over random entries
    u = top["ungated"]
    assert quoted("{} por operación en " + fmt_num(u["trades"], 0), u["expectancy_r"])
    conclusion = "Indicios de ventaja" if rep["summary"]["code"] == "edge" else "Conclusión: sin evidencia de ventaja"
    assert conclusion in flat
    assert re.search(r"commit `[0-9a-f]{7,40}`", doc), "the document names the engine version it measured"
    big = sum(1 for s in series if s["overall"]["nexus"]["trades"] >= 30)
    assert f"Solo {big} de las {n} series llegan a 30 operaciones" in flat
    lose = sum(1 for s in series if (s.get("verdict_ungated") or {}).get("code") == "negative")
    assert f"{lose} de las {n} series tienen el intervalo entero por debajo de 0" in flat
    for s in series:  # the data table lists every series with its bars and dates
        row = f"| {s['symbol']} {s['tf']} | {s['bars']} | {time.strftime('%Y-%m-%d', time.gmtime(s['start_time']))} |"
        assert row in doc, row
    assert "## 4. Qué significa para el uso de NEXUS" in doc
    for old, path in reports[:-1]:  # earlier runs stay cited (link + their trade counts), for transparency
        assert path.name in doc, path.name
        o_series = old["series"]
        o_gated = sum(s["overall"]["nexus"]["trades"] for s in o_series)
        o_long, o_short = (sum(s["side_mix"][k] for s in o_series) for k in ("long", "short"))
        assert f"{fmt_num(o_gated, 0)} ({o_long} / {o_short})" in flat, path.name
