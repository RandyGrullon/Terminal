"""int2-api · tools/contract_check.py: it reads types.ts AND the feature files next to it (types-*.ts), so the new
endpoints (alerts, validation, pending orders and portfolio risk, skill report, real time, calendar coverage) are
checked against their interfaces; every calendar item is checked, one report row per distinct result."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tool():
    spec = importlib.util.spec_from_file_location("contract_check_int2", ROOT / "tools" / "contract_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_feature_type_files_are_parsed_with_types_ts():
    cc = _tool()
    files = [p.name for p in cc.type_files()]
    assert files[0] == "types.ts" and {"types-alerts.ts", "types-paper-orders.ts", "types-realtime.ts",
                                       "types-validation.ts"} <= set(files)
    ifaces, _notes = cc.load_interfaces()
    for name in ("Health", "AlertsStatus", "AlertChannelStatus", "ValidationStatus", "ValidationLatestResponse",
                 "PaperOrder", "PaperRisk", "PaperRiskGroup", "RealtimeStatus", "RealtimeProvider", "SkillReport",
                 "SkillGroup", "MacroEvent"):
        assert name in ifaces, name
    assert ifaces["PaperOrder"]["expiry_text"] is False and ifaces["SkillGroup"].get("window") is True


def test_a_response_type_still_next_to_its_ui_is_used_and_named(tmp_path, monkeypatch):
    cc = _tool()
    api = tmp_path / "api"
    api.mkdir()
    (api / "types.ts").write_text("export interface Health { ok: boolean }\n", encoding="utf-8")
    (api / "types-extra.ts").write_text("export interface Extra {\n  a: number\n  b?: string\n}\n", encoding="utf-8")
    ui = tmp_path / "skill.ts"
    ui.write_text("export interface SkillReport { groups: SkillGroup[] }\nexport interface SkillGroup { id: string }\n",
                  encoding="utf-8")
    monkeypatch.setattr(cc, "UI_TYPE_FILES", {"SkillReport": ui, "Missing": tmp_path / "nope.ts"})
    ifaces, notes = cc.load_interfaces(api / "types.ts")
    assert ifaces["Extra"] == {"a": False, "b": True} and ifaces["SkillGroup"] == {"id": False}
    assert len(notes) == 1 and notes[0].startswith("SkillReport (y 1 tipos más) aún en") and "regla 6" in notes[0]
    (api / "types-skill.ts").write_text("export interface SkillReport { groups: number }\n", encoding="utf-8")
    ifaces, notes = cc.load_interfaces(api / "types.ts")
    assert notes == [] and "SkillGroup" not in ifaces  # once canonical, the UI file is not read


def test_check_all_reports_each_distinct_result_once():
    cc = _tool()
    ifaces = {"MacroEvent": {"id": False, "source": False, "countdown_s": False}}
    events = [{"id": "a", "source": "bls", "countdown_s": 1}, {"id": "b", "source": "bls", "countdown_s": 2},
              {"id": "c", "source": "bea"}, {"id": "d", "source": "ecb", "countdown_s": 3, "extra": 1}]
    rep = cc.Report()
    cc.check_all(rep, ifaces, "MacroEvent", events, "calendar.events", group="source")
    rows = {(status, where): detail for status, where, detail in rep.rows}
    assert rows[("OK", "calendar.events (2 de 4: bls)")] == "MacroEvent"
    assert "faltan ['countdown_s']" in rows[("FAIL", "calendar.events (1 de 4: bea)")]
    assert "claves extra ['extra']" in rows[("WARN", "calendar.events (1 de 4: ecb)")]
    empty = cc.Report()
    cc.check_all(empty, ifaces, "MacroEvent", [], "calendar.events")
    assert empty.rows == [("WARN", "calendar.events", "lista vacía (MacroEvent)")]
    keys = cc.Report()
    cc.check_keys(keys, "CalendarCoverage", cc.CALENDAR_COVERAGE_KEYS, {"weekly_until": 1, "sources": []}, "cov")
    assert keys.rows[0][0] == "FAIL" and "weekly_stale" in keys.rows[0][2]


def test_the_checker_stays_read_only():
    text = (ROOT / "tools" / "contract_check.py").read_text(encoding="utf-8")
    assert '"/api/alerts/test"' not in text.replace("POST /api/alerts/test", "")  # never messages the user
    assert '"/api/validation/run"' not in text and '"/api/paper/orders/cancel"' not in text
    assert 'call("POST", "/api/paper/orders"' not in text
