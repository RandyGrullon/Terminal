"""int2-api · terminal commands for the pending demo orders (group 'Cuenta demo'): orders, limit, stop, cancel, risk.

Through the full app (OFFLINE, no background loops) with the fake hub / gate of the BE-SIM tests. Runs on
PostgreSQL too with NEXUS_TEST_PG_URL (api_client).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_sim_fakes import FakeHub, FakeNexus

BTC = "BINANCE:BTCUSDT"
ETH = "BINANCE:ETHUSDT"


@pytest.fixture
def client(api_client):
    from app.runtime import rt

    rt.hub = FakeHub(prices={BTC: 100.0, ETH: 200.0})
    rt.nexus = FakeNexus("TAKE")
    yield api_client


def _term(client, text: str) -> dict:
    r = client.post("/api/terminal", json={"text": text})
    assert r.status_code == 200
    return r.json()


def _text(res: dict) -> str:
    return "\n".join(line["text"] for line in res["lines"])


def test_commands_are_registered_in_the_demo_account_group(client):
    cmds = {c["name"]: c for c in client.get("/api/terminal/commands").json()["commands"]}
    for name in ("orders", "limit", "stop", "cancel", "risk"):
        assert cmds[name]["group"] == "Cuenta demo", name
    assert "ordenes" in cmds["orders"]["aliases"] and "cancelar" in cmds["cancel"]["aliases"]
    assert "riesgo" in cmds["risk"]["aliases"] and "límite" in cmds["limit"]["aliases"]


def test_limit_and_stop_orders_are_placed_listed_and_cancelled(client):
    res = _term(client, "orders")
    assert res["ok"] and "Sin órdenes pendientes" in _text(res) and res["data"] == {"orders": []}

    res = _term(client, "limit btc 95 --sl=90 --tp=110 --exp=12v")
    assert res["ok"], res["lines"]
    o = res["data"]["order"]
    assert (o["symbol"], o["side"], o["kind"], o["price"], o["sl"], o["tp"]) == (BTC, "long", "limit", 95.0, 90.0, 110.0)
    assert o["expiry"] == "bars" and o["expire_bars"] == 12 and o["tf"] == "1h" and o["status"] == "pending"
    text = _text(res)
    assert f"Orden demo #{o['id']}: compra límite {BTC} a 95" in text and "válida 12 velas de 1h" in text
    assert "no garantiza la ejecución" in text and "nunca se envían a un bróker" in text  # honest notes
    assert "se vuelve a consultar el filtro" in text and res["actions"][0]["action"] == "open_chart"

    before = time.time()
    res = _term(client, "stop eth 210 --exp=4h --qty 0.5")  # buy stop above the market (ETH at 200)
    assert res["ok"], res["lines"]
    s = res["data"]["order"]
    assert (s["side"], s["kind"], s["qty"], s["expiry"]) == ("long", "stop", 0.5, "date")
    assert before + 4 * 3600 - 5 <= s["expire_time"] <= time.time() + 4 * 3600 + 5
    assert "no garantiza" not in _text(res)  # the touch note is about limit orders only

    res = _term(client, "limit eth 205,5 corto 4h")  # Spanish decimal comma, side and tf typed without --
    assert res["ok"], res["lines"]
    v = res["data"]["order"]
    assert (v["side"], v["price"], v["tf"], v["expiry"]) == ("short", 205.5, "4h", "gtc")

    when = datetime.now() + timedelta(days=3)
    res = _term(client, f"stop btc 90 --short --exp={when:%Y-%m-%d}")  # a bare date: until the end of that day
    assert res["ok"], res["lines"]
    d = res["data"]["order"]
    assert d["expiry"] == "date" and d["expire_time"] == int(when.replace(hour=23, minute=59, second=0,
                                                                              microsecond=0).timestamp())
    # a date with a time, in the user's zone (ui.timezone) when it is set
    from app.runtime import rt

    client.portal.call(rt.settings.set, "ui.timezone", "UTC")
    res = _term(client, f'stop btc 89 --short --exp="{when:%Y-%m-%d} 14:30"')
    assert res["ok"], res["lines"]
    assert res["data"]["order"]["expire_time"] == int(datetime(when.year, when.month, when.day, 14, 30,
                                                               tzinfo=timezone.utc).timestamp())
    assert _term(client, f"cancel {res['data']['order']['id']}")["ok"]

    res = _term(client, "orders")
    assert res["ok"] and len(res["data"]["orders"]) == 4
    text = _text(res)
    assert "Compra límite" in text and "Venta stop" in text and "TOMAR" in text and "cancel <id|símbolo|all>" in text

    res = _term(client, f"cancel {o['id']}")
    assert res["ok"] and f"Cancelada #{o['id']}: compra límite {BTC}" in _text(res)
    assert [c["id"] for c in res["data"]["cancelled"]] == [o["id"]]
    res = _term(client, f"cancel #{o['id']}")  # no longer pending: never reported as cancelled again
    assert res["ok"] is False and "No hay una orden pendiente" in _text(res)
    res = _term(client, "cancel eth")
    assert res["ok"] and {c["id"] for c in res["data"]["cancelled"]} == {s["id"], v["id"]}
    res = _term(client, "cancel all")
    assert res["ok"] and [c["id"] for c in res["data"]["cancelled"]] == [d["id"]]
    res = _term(client, "cancel all")
    assert res["ok"] and "No había órdenes pendientes" in _text(res)
    assert _term(client, "cancel")["ok"] is False

    res = _term(client, "orders all")
    assert res["ok"] and "Últimas órdenes cerradas" in _text(res) and "Cancelada por el usuario" in _text(res)
    assert {x["status"] for x in res["data"]["orders"]} == {"cancelled"} and len(res["data"]["orders"]) == 5


def test_bad_input_is_refused_never_silently_ignored(client):
    from app.runtime import rt

    cases = [
        ("limit btc 105", "compra stop"),  # a buy limit above the market: the engine suggests the other type
        ("stop btc 95", "compra límite"),
        ("limit btc 95,000", "miles o decimales"),  # ambiguous: 95 000 or 95.0?
        ("limit btc abc", "Valor no válido para el precio"),
        ("limit btc 95 --sl abc", "Valor no válido para el stop (--sl): abc"),
        ("limit btc 95 --sl", "Falta el valor de --sl"),
        ("limit btc 95 --tp=0", "El objetivo (--tp) debe ser un número mayor que 0"),
        ("limit btc 95 ahora", "Argumento no reconocido: ahora"),
        ("limit btc 95 short --long", "un solo lado"),
        ("limit btc 95 --exp=semana", "Caducidad no válida"),
        ("limit btc 95 --exp=2026-02-30", "Fecha no válida"),
        ("limit btc 95 --exp=0v", "velas"),
        ("limit btc", "Uso: limit <símbolo> <precio>"),
        ("stop", "sim stop"),  # the simulator is stopped with 'sim stop'
        ("limit nada 95", "No encontré el símbolo"),
    ]
    for text, needle in cases:
        res = _term(client, text)
        assert res["ok"] is False and needle in _text(res), (text, _text(res))
    assert client.get("/api/paper/orders").json() == {"orders": []}
    assert rt.nexus.gate_calls == []  # nothing reached the gate


def test_gate_and_risk_blocks(client):
    from app.runtime import rt

    rt.nexus.verdict = "AVOID"
    res = _term(client, "limit btc 96")
    assert res["ok"] is False and res["data"]["rejected"] is True
    text = _text(res)
    assert "✖ NO TOMAR — compra límite en BINANCE:BTCUSDT a 96" in text and "--force" in text
    assert "Orden no creada: el filtro dice NO TOMAR" in text
    res = _term(client, "limit btc 96 --force")
    assert res["ok"] and res["data"]["order"]["forced"] is True and "FORZADA" in _text(res)
    res = _term(client, "orders")
    assert "(forzada)" in _text(res)

    rt.nexus.verdict = "TAKE"
    client.portal.call(rt.settings.set, "paper.max_positions_per_group", 1)
    assert _term(client, "buy btc --sl 98")["ok"]
    res = _term(client, "limit eth 190")  # all crypto is one correlated group (max 1 open)
    assert res["ok"] is False and res["data"]["blocked"] is True and res["data"]["rule"] == "group"
    text = _text(res)
    assert "Límite de posiciones por grupo correlacionado" in text and "no se saltan con --force" in text
    assert _term(client, "limit eth 190 --force")["ok"] is False  # never forced

    res = _term(client, "risk")
    assert res["ok"] and res["data"]["open_positions"] == 1 and res["data"]["pending_orders"] == 1
    text = _text(res)
    assert "Riesgo abierto (entrada → stop)" in text and "Cripto: 1 posición de 1" in text and "lleno" in text
    assert "Cuenta demo: órdenes simuladas" in text
    assert _term(client, "riesgo")["ok"]
