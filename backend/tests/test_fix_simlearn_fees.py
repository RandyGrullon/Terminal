"""Fix round (simlearn): global-fee-low-vol (backtest part).

Backtests and the Monte Carlo bootstrap charge the commission of the symbol's asset class
(`risk.fee_pct_by_class`, falling back to `risk.fee_pct`) unless the request gives an explicit fee, and the engine's
gate sees that same cost.
"""
from __future__ import annotations

import copy

import pytest

from app.config import DEFAULT_SETTINGS
from app.data.base import CandleSet
from app.sim import backtest as bt
from tests.test_sim_metrics import trade


class _Params:
    seen: list[dict] = []

    def __init__(self, gate_enabled: bool, fee_pct: float):
        self.gate_enabled = gate_enabled
        self.fee_pct = fee_pct

    @classmethod
    def from_settings(cls, settings: dict, **kw):
        if kw:
            raise TypeError(f"unexpected {sorted(kw)}")
        _Params.seen.append(dict(settings))
        return cls(bool(settings.get("gate.enabled", True)), float(settings["risk.fee_pct"]))

    def to_dict(self) -> dict:
        return {"gate_enabled": self.gate_enabled, "fee_pct": self.fee_pct}


@pytest.fixture
def engine(monkeypatch):
    _Params.seen = []

    def run_engine(cs, params, prior=None):
        return {"trades": [trade(160, 170, r=2.0)], "markers": [],
                "stats": {"rejected_signals": 3, "rejected_would_win_rate": 0.5, "rejected_resolved": 2}}, None

    monkeypatch.setattr(bt, "load_engine", lambda: (None, _Params))
    monkeypatch.setattr(bt, "run_engine", run_engine)
    return _Params


def _candles(symbol: str, n: int = 260) -> CandleSet:
    rows = [{"time": 1_700_000_000 + i * 3600, "open": 1.08, "high": 1.0805, "low": 1.0795, "close": 1.08,
             "volume": 1.0} for i in range(n)]
    return CandleSet.from_rows(rows, symbol, "1h")


def _settings() -> dict:
    return copy.deepcopy(DEFAULT_SETTINGS)


def test_backtest_fee_comes_from_the_asset_class(engine):
    fx = bt.run_backtest(_candles("FX:EURUSD"), settings=_settings(), include_candles=False)
    assert fx["params"]["fee_pct"] == pytest.approx(0.005)
    assert {s["risk.fee_pct"] for s in engine.seen} == {0.005}  # gated and ungated engine runs: same cost
    assert any(n.startswith("Comisión 0,005 % por lado: la de las divisas") for n in fx["params"]["notes"])
    engine.seen.clear()
    crypto = bt.run_backtest(_candles("BINANCE:BTCUSDT"), settings=_settings(), include_candles=False)
    assert crypto["params"]["fee_pct"] == pytest.approx(0.1)
    custom = _settings()
    custom["risk.fee_pct_by_class"] = {**custom["risk.fee_pct_by_class"], "stock": 0.05}
    stock = bt.run_backtest(_candles("NASDAQ:AAPL"), settings=custom, include_candles=False)
    assert stock["params"]["fee_pct"] == pytest.approx(0.05)


def test_explicit_fee_wins(engine):
    res = bt.run_backtest(_candles("FX:EURUSD"), settings=_settings(), fee_pct=0.2, include_candles=False)
    assert res["params"]["fee_pct"] == pytest.approx(0.2)
    assert {s["risk.fee_pct"] for s in engine.seen} == {0.2}
    assert any("indicada en la petición" in n for n in res["params"]["notes"])
    with pytest.raises(bt.BacktestError):
        bt.run_backtest(_candles("FX:EURUSD"), settings=_settings(), fee_pct=-1)


def test_rejected_resolved_is_reported_with_its_rate(engine):
    res = bt.run_backtest(_candles("BINANCE:BTCUSDT"), settings=_settings(), include_candles=False)
    m, u = res["metrics"], res["ungated"]
    assert (m["rejected_signals"], m["rejected_would_win_rate"], m["rejected_resolved"]) == (3, 0.5, 2)
    assert (u["rejected_signals"], u["rejected_would_win_rate"], u["rejected_resolved"]) == (0, None, 0)


def test_montecarlo_bootstrap_uses_the_symbol_fee(engine):
    bt.equity_curve_pct(_candles("FX:EURUSD"), settings=_settings())
    assert engine.seen[-1]["risk.fee_pct"] == pytest.approx(0.005)
    bt.equity_curve_pct(_candles("SIM:NEXUS"), settings=_settings())
    assert engine.seen[-1]["risk.fee_pct"] == pytest.approx(0.1)


def test_backtest_endpoint_leaves_the_fee_to_the_asset_class_unless_given(api_client, monkeypatch):
    from app.api import sim as api_sim
    from app.runtime import rt
    from tests.test_sim_fakes import FakeHub

    calls: list[dict] = []

    def fake_run_backtest(cs, params=None, **kw):
        calls.append(kw)
        return {"symbol": cs.symbol, "tf": cs.tf, "metrics": {"trades": 0}, "params": {}}

    rt.hub = FakeHub(candles={("FX:EURUSD", "1h"): _candles("FX:EURUSD", 400)})
    monkeypatch.setattr(api_sim, "load_engine", lambda: (None, None))
    monkeypatch.setattr(api_sim, "run_backtest", fake_run_backtest)
    assert api_client.post("/api/backtest", json={"symbol": "FX:EURUSD", "tf": "1h"}).status_code == 200
    assert api_client.post("/api/backtest", json={"symbol": "FX:EURUSD", "tf": "1h", "fee_pct": 0.03}).status_code == 200
    assert [c["fee_pct"] for c in calls] == [None, 0.03]
