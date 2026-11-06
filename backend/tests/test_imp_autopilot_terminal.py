"""Terminal command `autopilot` (group 'Cuenta demo', aliases piloto / automatica): NEXUS's own demo account (3) from
the terminal. Status with what it watches and NEXUS's read of each symbol's last bar, on / off with a list or the
scanner and its limits, bad input refused in Spanish, its positions, decision log, closing by hand and a reset that
needs --yes; the user's account is never touched.

Through the full app (OFFLINE, no background loops) with the fake hub / NEXUS of the BE-SIM tests. Runs on PostgreSQL
too with NEXUS_TEST_PG_URL (api_client).
"""
from __future__ import annotations

import asyncio

import pytest

from tests.test_sim_fakes import FakeHub, FakeNexus, make_current

BTC = "BINANCE:BTCUSDT"
ETH = "BINANCE:ETHUSDT"


@pytest.fixture
def client(api_client):
    from app.runtime import rt

    rt.hub = FakeHub(prices={BTC: 100.0, ETH: 200.0})
    rt.nexus = FakeNexus("TAKE", current=make_current("WAIT"))
    yield api_client


def _term(client, text: str) -> dict:
    r = client.post("/api/terminal", json={"text": text})
    assert r.status_code == 200
    return r.json()


def _text(res: dict) -> str:
    return "\n".join(line["text"] for line in res["lines"])


async def _settle(engine) -> None:
    for _ in range(50):
        pending = [t for t in engine._tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _decided(client) -> None:
    """Wait for the decisions the command started (a new candidate is decided at once on its last closed bar)."""
    from app.runtime import rt

    client.portal.call(_settle, rt.paper)


def test_registered_in_the_demo_account_group(client):
    cmds = {c["name"]: c for c in client.get("/api/terminal/commands").json()["commands"]}
    ap = cmds["autopilot"]
    assert ap["group"] == "Cuenta demo" and {"piloto", "automatica", "automática"} <= set(ap["aliases"])
    assert "lista <símbolos…>" in ap["usage"] and "reset [saldo] --yes" in ap["usage"]


def test_status_on_with_a_list_and_off(client):
    res = _term(client, "autopilot")
    assert res["ok"], res["lines"]
    text = _text(res)
    assert "NEXUS automática (cuenta demo 3, simulada) · PAUSADA" in text
    assert "Qué vigila: escáner: hasta 10 oportunidades TOMAR del último escaneo · decide en velas de 1h" in text
    assert "Sin escaneos terminados todavía" in text and "ganadoras sin operaciones cerradas todavía" in text
    assert "Avisos por Telegram o correo: desactivados (autopilot avisos on)" in text
    assert "no encontró ventaja" in text and "bróker" in text  # the honest note, verbatim
    assert res["data"]["account"]["id"] == 3 and res["data"]["autopilot"]["enabled"] is False

    res = _term(client, "autopilot on btc eth --tf 4h --max 3 --risk 0,5")
    assert res["ok"], res["lines"]
    st = res["data"]["autopilot"]
    assert (st["enabled"], st["source"], st["symbols"], st["tf"]) == (True, "list", [BTC, ETH], "4h")
    assert (st["max_positions"], st["risk_pct"]) == (3, 0.5)
    text = _text(res)
    assert (f"✔ NEXUS automática activada: opera sola en su cuenta demo (simulada) con su lista fija ({BTC}, {ETH}). "
            "Solo entra con señal de NEXUS y filtro TOMAR, al cierre de cada vela de 4h") in text
    assert "solo mientras el servidor está en marcha" in text and "ACTIVA: opera sola" in text
    _decided(client)
    res = _term(client, "piloto")  # an alias; ESPERAR on both: nothing opened, NEXUS's read shown per symbol
    assert res["ok"] and res["command"] == "autopilot"
    text = _text(res)
    assert "Última vela" in text and text.count("ESPERAR · vela de") == 2, text
    assert res["data"]["positions"] == [] and _term(client, "positions")["data"] == []

    res = _term(client, "autopilot off")
    assert res["ok"] and res["data"]["autopilot"]["enabled"] is False
    assert "NEXUS automática pausada: no abre posiciones nuevas; las abiertas siguen con su stop y sus objetivos" in \
        _text(res)


def test_scanner_source_and_bad_input_refused_in_spanish(client):
    res = _term(client, "autopilot escaner 5")
    assert res["ok"], res["lines"]
    assert (res["data"]["autopilot"]["source"], res["data"]["autopilot"]["scanner_top"]) == ("scanner", 5)
    assert "✔ Cambios de NEXUS automática guardados" in _text(res)
    bad = {
        "autopilot escaner 2,5": "debe ser un número entero entre 1 y 30",
        "autopilot escaner 5 6": "Sobra «6»",
        "autopilot --risk 9": "El riesgo por operación (%) debe ser un número entre 0,1 y 5,0",
        "autopilot --max": "Falta el valor de --max",
        "autopilot --max 0": "debe ser un número mayor que 0",
        "autopilot --top 31": "entre 1 y 30",
        "autopilot --tf 7x": "",
        "autopilot btc": "No entiendo «btc»",
        "autopilot lista": "Faltan los símbolos de la lista",
        "autopilot close": "Indica qué cerrar",
        "autopilot avisos": "Indica on u off",
        "autopilot avisos quizas": "Indica on u off",
    }
    for line, why in bad.items():
        res = _term(client, line)
        assert res["ok"] is False, line
        assert why in _text(res), (line, _text(res))
    st = _term(client, "autopilot")["data"]["autopilot"]
    assert (st["enabled"], st["source"], st["scanner_top"], st["risk_pct"]) == (False, "scanner", 5, 1.0)  # unchanged


def test_cash_account_and_brake_from_the_terminal(client):
    res = _term(client, "autopilot")
    text = _text(res)
    assert "Cuenta de efectivo: hasta el 100 % de su capital invertido · freno de pérdidas: 20 % desde el saldo inicial" \
        in text and res["data"]["account"]["balance"] == 1000.0
    res = _term(client, "autopilot ambos 5 --expo 80 --freno 0")
    assert res["ok"], res["lines"]
    st = res["data"]["autopilot"]
    assert (st["source"], st["scanner_top"], st["max_exposure_pct"], st["max_drawdown_pct"]) == ("both", 5, 80.0, 0.0)
    assert "freno de pérdidas: desactivado" in _text(res) and "y hasta 5 oportunidades TOMAR" in _text(res)
    assert _term(client, "autopilot --freno 95")["ok"] is False
    assert _term(client, "autopilot --expo")["ok"] is False


def test_stocks_with_options_from_the_terminal(client):
    text = _text(_term(client, "autopilot"))
    assert ("Acciones: NEXUS elige, acciones para apostar al alza y un put a la baja (una cuenta de efectivo no vende "
            "en corto); opciones teóricas con hasta el 10 % del capital en primas por operación") in text
    res = _term(client, "autopilot --instrumento opciones --prima 5")
    assert res["ok"], res["lines"]
    st = res["data"]["autopilot"]
    assert (st["stock_instrument"], st["options_premium_pct"]) == ("options", 5.0)
    assert "Acciones: solo opciones, un call para apostar al alza y un put a la baja; opciones teóricas con hasta el " \
           "5 % del capital" in _text(res)
    res = _term(client, "piloto --instrumento acciones --prima 0")
    assert res["ok"] and res["data"]["autopilot"]["stock_instrument"] == "shares"
    assert "compra acciones y también vende en corto (simulado)" in _text(res)
    bad = _term(client, "autopilot --instrumento futuros")
    assert bad["ok"] is False and "Indica --instrumento auto (NEXUS elige), acciones u opciones" in _text(bad)
    assert _term(client, "autopilot --prima 70")["ok"] is False


def test_alerts_switch_from_the_terminal(client):
    res = _term(client, "autopilot avisos on")
    assert res["ok"] and res["data"]["autopilot"]["alerts"] is True
    text = _text(res)
    assert "⚠ Avisos por Telegram o correo: activados, pero ningún canal está activo (configúralo en Ajustes → " \
           "Alertas)" in text and "aperturas, cierres y stops" in text
    assert client.get("/api/settings").json()["settings"]["alerts.autopilot"] is True
    assert "NEXUS automática" in _text(_term(client, "alertas"))  # the alerts status lists the category
    res = _term(client, "piloto avisos off")
    assert res["ok"] and res["data"]["autopilot"]["alerts"] is False
    assert "Avisos por Telegram o correo: desactivados" in _text(res)


def test_trades_positions_log_close_and_a_reset_that_needs_yes(client):
    from app.runtime import rt

    rt.nexus.current_value = make_current("BUY")  # NEXUS signals COMPRAR and the gate says TOMAR
    res = _term(client, "autopilot on lista btc")
    assert res["ok"], res["lines"]
    _decided(client)
    res = _term(client, "autopilot pos")
    assert res["ok"] and [(p["symbol"], p["side"], p["origin"]) for p in res["data"]] == [(BTC, "long", "autopilot")]
    assert BTC in _text(res) and "autopilot close <id|símbolo|all>" in _text(res)
    assert _term(client, "positions")["data"] == []  # the user's account: untouched
    pid = res["data"][0]["id"]

    res = _term(client, "autopilot log")
    text = _text(res)
    assert res["ok"] and "Nota: Activada" in text and f"Abre {BTC}: Abre largo a " in text
    assert text.index("Activada") < text.index("Abre largo")  # oldest first, like the rest of the terminal

    res = _term(client, "autopilot close btc")
    assert res["ok"], res["lines"]
    assert [p["id"] for p in res["data"]["closed"]] == [pid] and f"Cerrado #{pid} {BTC} largo de NEXUS" in _text(res)
    assert "Cerrada a mano a " in _text(_term(client, "autopilot log"))
    res = _term(client, "autopilot close all")
    assert res["ok"] and "NEXUS no tenía posiciones abiertas" in _text(res)
    assert _term(client, "autopilot close 999")["ok"] is False

    res = _term(client, "autopilot reset 5000")
    assert res["ok"] and "Repite con --yes para confirmar" in _text(res)
    assert _term(client, "autopilot")["data"]["account"]["balance"] != 5000.0
    res = _term(client, "autopilot reset 5000 --yes")
    assert res["ok"], res["lines"]
    assert "✔ Cuenta de NEXUS reiniciada con saldo 5.000,00" in _text(res)
    assert res["data"]["account"]["balance"] == 5000.0 and res["data"]["history"] == []
    assert res["data"]["autopilot"]["log"] == []
    assert _term(client, "account")["data"]["balance"] == 10000.0  # the user's account keeps its balance
