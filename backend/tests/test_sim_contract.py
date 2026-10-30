"""BE-SIM adapters against the ENGINE-CORE / DATA-HUB contracts.

The real engine is written concurrently, so these tests install stand-in modules shaped exactly like the contract:
`NexusParams` (dataclass, `from_settings(settings, **overrides)`, bool field `gate_enabled`), `run_nexus(cs, params,
prior=None) -> NexusResult` (`to_response(cs)`, `snapshots: list[{"time", "state"}]`), `GateLearner.from_state /
update / to_state` and `NexusService.reload_gate_state()`; plus a hub whose subscribe / add_listener are async.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import types

import pytest

from app import config as nexus_config
from app.config import DEFAULT_SETTINGS
from app.sim import backtest as bt
from app.sim import evaluator as ev
from app.sim.common import build_nexus_params, normalize_snapshots, params_to_dict, run_engine
from app.sim.synthetic import generate_candleset
from tests.conftest import FIXED_END

TRADE_KEYS = {"id", "side", "entry_time", "entry_price", "exit_time", "exit_price", "sl_initial", "tp1", "tp2", "tp3",
              "exit_reason", "exit_label", "pnl_pct", "r_multiple", "bars", "mfe_pct", "mae_pct", "gate_score"}


@dataclasses.dataclass(frozen=True)
class ContractParams:
    entry_threshold: float = 0.35
    sl_atr: float = 2.0
    horizon: int = 5
    take_score: float = 65.0
    analog_k: int = 30  # engine-only field (no setting)
    gate_enabled: bool = True

    @classmethod
    def from_settings(cls, settings: dict, **overrides):
        base = {
            "entry_threshold": float(settings.get("nexus.entry_threshold", 0.35)),
            "sl_atr": float(settings.get("nexus.sl_atr", 2.0)),
            "horizon": int(settings.get("nexus.horizon", 5)),
            "take_score": float(settings.get("gate.take_score", 65.0)),
            "gate_enabled": bool(settings.get("gate.enabled", True)),
        }
        base.update(overrides)
        return cls(**base)  # unknown names raise TypeError, like a real dataclass


CALLS: list[dict] = []


class ContractResult:
    """NexusResult stand-in: one long trade per 60 bars; the gate removes every second one."""

    def __init__(self, cs, params: ContractParams, prior):
        self.params, self.prior = params, prior
        t = cs.time.tolist()
        idx = sorted(set(range(0, len(t), 100)) | {len(t) - 1})  # like the engine: every 100 bars + the last bar
        self.snapshots = [{"time": int(t[i]), "state": {"upto": int(t[i])}} for i in idx]
        self.trades = []
        for k, i in enumerate(range(160, len(t) - 12, 60)):
            if params.gate_enabled and k % 2:
                continue
            entry = float(cs.open[i + 1])
            win = k % 3 != 2
            exit_price = entry * (1.02 if win else 0.99)
            self.trades.append({
                "id": len(self.trades) + 1, "side": "long", "entry_time": int(t[i + 1]), "entry_price": entry,
                "exit_time": int(t[i + 10]), "exit_price": exit_price, "sl_initial": entry * 0.99,
                "tp1": entry * 1.01, "tp2": entry * 1.02, "tp3": entry * 1.03,
                "exit_reason": "TP3" if win else "SL", "exit_label": "Objetivo 3" if win else "Stop loss",
                "pnl_pct": (exit_price / entry - 1) * 100, "r_multiple": 2.0 if win else -1.0, "bars": 9,
                "mfe_pct": 2.1, "mae_pct": -0.4, "gate_score": 70.0 if params.gate_enabled else None,
            })

    def to_response(self, cs) -> dict:
        last_t, last_c = int(cs.time[-1]), float(cs.close[-1])
        return {
            "ok": True, "message": None, "params": dataclasses.asdict(self.params), "markers": [],
            "trades": list(self.trades), "prediction": None, "bars": {"time": cs.time.tolist()},
            "current": {"time": last_t, "price": last_c, "action": "WAIT", "action_label": "ESPERAR",
                        "score": 0.0, "prob_up": 0.5, "gate": {}},
            "learning": {"updates": len(cs), "hit_rate": None, "brier": None, "weights": {}, "gate_weights": {},
                         "trained_until": None, "memory_bars": len(cs)},
            "stats": {"trades": len(self.trades), "rejected_signals": 3 if self.params.gate_enabled else 0,
                      "rejected_would_win_rate": 0.25 if self.params.gate_enabled else None},
        }


def contract_run_nexus(cs, params, prior=None, *, gate_state=None):
    CALLS.append({"first": int(cs.time[0]), "n": len(cs), "prior": prior, "gate": params.gate_enabled,
                  "gate_state": gate_state})
    return ContractResult(cs, params, prior)


@pytest.fixture
def contract_engine(monkeypatch):
    CALLS.clear()
    mod = types.ModuleType("app.engine.nexus")
    mod.NexusParams = ContractParams
    mod.run_nexus = contract_run_nexus
    monkeypatch.setitem(sys.modules, "app.engine.nexus", mod)
    return mod


def test_build_params_follows_the_dataclass_contract():
    overrides = {"sl_atr": 2.5, "nexus.horizon": 8, "take_score": 70, "analog_k": 50, "bogus": 1}
    params, ignored = build_nexus_params(ContractParams, dict(DEFAULT_SETTINGS), overrides, gate_enabled=False)
    assert isinstance(params, ContractParams) and ignored == ["bogus"]
    assert (params.sl_atr, params.horizon, params.take_score, params.analog_k) == (2.5, 8, 70.0, 50)
    assert params.gate_enabled is False and params_to_dict(params)["gate_enabled"] is False
    gated, _ = build_nexus_params(ContractParams, dict(DEFAULT_SETTINGS), None, gate_enabled=True)
    assert gated.gate_enabled is True and gated.sl_atr == DEFAULT_SETTINGS["nexus.sl_atr"]
    with pytest.raises(ValueError):
        build_nexus_params(ContractParams, dict(DEFAULT_SETTINGS), {"nexus.horizon": "abc"})


def test_run_engine_uses_to_response_and_snapshot_list(contract_engine):
    cs = generate_candleset(n=320, seed=2, end_time=FIXED_END)
    params, _ = build_nexus_params(ContractParams, dict(DEFAULT_SETTINGS), None)
    resp, snaps = run_engine(cs, params, prior={"upto": 1})
    assert resp["ok"] is True and resp["params"]["gate_enabled"] is True and resp["trades"]
    assert CALLS[-1]["prior"] == {"upto": 1}
    norm = normalize_snapshots(snaps)
    assert [t for t, _ in norm] == [int(cs.time[i]) for i in (0, 100, 200, 300, 319)]
    assert norm[1][1] == {"upto": int(cs.time[100])}


def test_backtest_with_contract_engine(contract_engine):
    cs = generate_candleset(n=900, seed=6, end_time=FIXED_END)
    res = bt.run_backtest(cs, {"sl_atr": 2.5, "nope": 3}, include_candles=False)
    json.dumps(res, allow_nan=False)
    assert [c["gate"] for c in CALLS] == [True, False]  # gated run + ungated comparison
    m, u = res["metrics"], res["ungated"]
    assert u is not None and m["trades"] < u["trades"]
    assert m["rejected_signals"] == 3 and m["rejected_would_win_rate"] == 0.25
    assert m["gate_lift_pct"] == pytest.approx(m["total_return_pct"] - u["total_return_pct"])
    assert res["params"]["nexus"]["gate_enabled"] is True and res["params"]["nexus"]["sl_atr"] == 2.5
    assert res["params"]["ignored_params"] == ["nope"]
    assert res["trades"] and all(set(t) == TRADE_KEYS for t in res["trades"])
    assert [t["exit_reason"] for t in res["trades"]][:2] == ["TP3", "SL"]  # gated keeps trades k = 0, 2, …
    assert res["trades"][0]["gate_score"] == 70.0 and res["trades"][0]["exit_label"] == "Objetivo 3"


# ----------------------------------------------------------------------------- simulator / gate learner / hub

BTC = "BINANCE:BTCUSDT"
PAPER_ACCOUNT_KEYS = {"id", "name", "balance", "equity", "initial_balance", "realized_pnl", "unrealized_pnl",
                      "return_pct", "trades", "wins", "losses", "win_rate", "open_positions", "auto_trade",
                      "auto_symbols", "auto_tf"}
POSITION_KEYS = {"id", "account_id", "symbol", "tf", "side", "qty", "qty_open", "entry_price", "entry_time", "sl", "tp1",
                 "tp2", "tp3", "trail", "tp_hits", "status", "price", "exit_price", "exit_time", "pnl", "pnl_pct", "r_multiple",
                 "exit_reason", "exit_label", "origin", "gate_verdict", "gate_score", "advice", "warnings"}


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", True)


def test_simulator_default_engine_passes_a_causal_prior(runtime_factory, contract_engine, offline):
    from app.sim.paper import PaperEngine
    from app.sim.simulator import Simulator
    from tests.test_sim_fakes import FakeHub

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub()
            rt.paper = PaperEngine(rt)
            await rt.paper.start()
            sim = Simulator(rt)
            await sim.start()
            await sim.start_session({"mode": "synthetic", "speed": 0.5, "seed": 8, "warmup": 590})
            sim._paused = True  # drive it by hand
            for _ in range(25):
                await sim.step(sim._market.next_bar())
            assert len(CALLS) == 26 and CALLS[0]["prior"] is None
            assert all(c["n"] <= 600 for c in CALLS)
            assert all(c["prior"] is not None and c["prior"]["upto"] <= c["first"] for c in CALLS[1:])  # no lookahead
            assert CALLS[-1]["first"] > CALLS[0]["first"]  # the window slides once more than 600 bars are revealed
            snap = await sim.snapshot()
            assert snap["nexus"]["params"]["gate_enabled"] is True and snap["nexus"]["provider"] == "synthetic"
            await sim.stop_session(emit=False)
            await rt.paper.stop()

    asyncio.run(scenario())


class ContractLearner:
    def __init__(self, wins: int = 0, n: int = 0):
        self.wins, self.n = wins, n

    @classmethod
    def from_state(cls, state: dict):
        if state is None:
            raise TypeError("from_state necesita un dict")
        return cls(int(state.get("wins", 0)), int(state.get("n", 0)))

    def update(self, checks: dict, won: bool) -> None:
        assert set(checks) == {"trend_alignment", "volume"}
        self.n += 1
        self.wins += int(bool(won))

    def to_state(self) -> dict:
        return {"wins": self.wins, "n": self.n}


class ReloadingNexus:
    def __init__(self):
        self.reloads = 0

    async def reload_gate_state(self) -> None:
        self.reloads += 1


def test_gate_learner_contract_and_service_reload(runtime_factory, monkeypatch, offline):
    mod = types.ModuleType("app.engine.gate")
    mod.GateLearner = ContractLearner
    monkeypatch.setitem(sys.modules, "app.engine.gate", mod)
    samples = [({"trend_alignment": 1, "volume": None}, True), ({"trend_alignment": 0, "volume": 1}, False)]

    async def scenario():
        async with runtime_factory() as rt:
            rt.nexus = ReloadingNexus()
            evaluator = ev.PredictionEvaluator(rt)
            assert await evaluator._update_gate_learner(samples) == "engine"  # no stored state yet
            assert await evaluator._update_gate_learner(samples[:1]) == "engine"
            row = await rt.db.fetchone("SELECT state, updates FROM model_state WHERE kind='gate' AND key='global'")
            assert json.loads(row["state"]) == {"wins": 2, "n": 3} and row["updates"] == 3
            assert rt.nexus.reloads == 2

    asyncio.run(scenario())


def test_paper_with_async_hub_methods_and_ws_shapes(runtime_factory, monkeypatch):
    from app.sim.paper import PaperEngine
    from app.util import now_s
    from tests.test_sim_fakes import FakeHub, FakeNexus

    monkeypatch.setattr(nexus_config, "BACKGROUND", True)  # subscriptions are managed only with background on
    monkeypatch.setattr(nexus_config, "OFFLINE", True)

    class AsyncHub(FakeHub):
        async def subscribe(self, symbol, tf, owner):
            FakeHub.subscribe(self, symbol, tf, owner)

        async def unsubscribe(self, symbol, tf, owner):
            FakeHub.unsubscribe(self, symbol, tf, owner)

        async def add_listener(self, cb):
            FakeHub.add_listener(self, cb)

    async def scenario():
        async with runtime_factory() as rt:
            published = []
            monkeypatch.setattr(rt.ws, "publish", lambda topic, msg: published.append((topic, msg)))
            rt.hub = AsyncHub(prices={BTC: 100.0})
            rt.nexus = FakeNexus("TAKE")
            paper = rt.paper = PaperEngine(rt)
            await paper.start()
            assert rt.hub.listeners == [paper._on_candle]
            res = await paper.open_position(1, BTC, "long", "1h")
            assert set(res) == {"position", "gate"} and set(res["position"]) == POSITION_KEYS
            assert (BTC, "1m", "paper") in rt.hub.subs
            bar = {"time": now_s(), "open": 100.0, "high": 100.4, "low": 99.9, "close": 100.2, "volume": 3.0}
            await rt.hub.push_candle(BTC, "1m", bar, False)
            assert paper.cached_price(BTC) == 100.2
            msgs = [m for t, m in published if t == "paper"]
            assert msgs and set(msgs[-1]) == {"type", "account", "positions"} and msgs[-1]["type"] == "paper"
            assert set(msgs[-1]["account"]) == PAPER_ACCOUNT_KEYS
            assert all(set(p) == POSITION_KEYS for p in msgs[-1]["positions"])
            await paper.close_position(all=True)
            assert rt.hub.subs == []
            snap = await paper.snapshot(1)
            assert set(snap) == {"account", "positions", "history"} and set(snap["history"][0]) == POSITION_KEYS
            await paper.stop()

    asyncio.run(scenario())


def test_scanner_respects_hub_tv_online(runtime_factory, monkeypatch):
    from app.sim import scanner as sc
    from tests.test_sim_fakes import FakeHub, scan_row

    monkeypatch.setattr(nexus_config, "BACKGROUND", False)
    monkeypatch.setattr(nexus_config, "OFFLINE", False)

    class OfflineTvHub(FakeHub):
        def tv_online(self) -> bool:
            return False

    async def scenario():
        async with runtime_factory() as rt:
            cs = generate_candleset(n=200, seed=5, end_time=FIXED_END, symbol=BTC)
            rt.hub = OfflineTvHub(scan_rows=[scan_row(BTC)], candles={(BTC, "1h"): cs})
            svc = rt.scanner = sc.ScannerService(rt)
            await svc.start()
            run_id = await svc.run_scan("crypto", "1h", 20, wait=True)
            run, results = await svc.results(run_id)
            assert run["status"] == "error" and "TradingView no está disponible" in run["message"] and results == []
            quick = await svc.quick_signal(BTC, "1h")
            assert quick is not None and quick["reasons"][0].startswith("Lectura local")  # candles fallback
            assert rt.hub.scan_calls == []  # no pointless requests while TradingView is off
            await svc.stop()

    asyncio.run(scenario())


def test_learning_metrics_from_engine_model_states():
    assert ev._state_metric({"hit_rate": 0.6, "brier": 0.2}, "hit_rate") == 0.6  # bare NexusLearner.to_state()
    wrapped = {"snapshots": [{"time": 1, "state": {"hit_rate": 0.5}},
                             {"time": 2, "state": {"hit_rate": 0.55, "brier": 0.24}}]}
    assert ev._state_metric(wrapped, "hit_rate") == 0.55 and ev._state_metric(wrapped, "brier") == 0.24
    assert ev._state_metric({"snapshots": {"10": {"hit_rate": 0.4}, "20": {"hit_rate": 0.7}}}, "hit_rate") == 0.7
    assert ev._state_metric({"learner": {"brier": 0.21}}, "brier") == 0.21
    assert ev._state_metric({"snapshots": []}, "hit_rate") is None and ev._state_metric(None, "hit_rate") is None


def test_real_gate_learner_through_the_evaluator(runtime_factory, offline):
    gate_mod = pytest.importorskip("app.engine.gate")
    samples = [({"trend_alignment": 1, "risk_reward": 1, "volume": None}, True),
               ({"trend_alignment": 0, "risk_reward": 1, "volume": 0}, False)] * 3

    async def scenario():
        async with runtime_factory() as rt:
            evaluator = ev.PredictionEvaluator(rt)
            assert await evaluator._update_gate_learner(samples) == "engine"
            row = await rt.db.fetchone("SELECT state, updates FROM model_state WHERE kind='gate' AND key='global'")
            learner = gate_mod.GateLearner.from_state(json.loads(row["state"]))
            assert learner.updates == row["updates"] == 6 and learner.wins == 3
            good = learner.predict({"trend_alignment": 1, "risk_reward": 1})
            assert good > learner.predict({"trend_alignment": 0, "risk_reward": 1})

    asyncio.run(scenario())


def test_provider_errors_become_clean_spanish_errors(api_client, monkeypatch):
    from app.api import sim as api_sim
    from app.data.base import ProviderError
    from app.runtime import rt
    from tests.test_sim_fakes import FakeHub

    class RaisingHub(FakeHub):
        async def get_candles(self, symbol, tf, limit=500):
            raise ProviderError("Temporalidad no soportada por el proveedor")

    rt.hub = RaisingHub()
    monkeypatch.setattr(api_sim, "load_engine", lambda: (None, None))  # reach the data step without the engine
    r = api_client.post("/api/backtest", json={"symbol": BTC, "tf": "1h"})
    assert r.status_code == 400 and "Temporalidad no soportada" in r.json()["detail"]
    r = api_client.post("/api/montecarlo", json={"mode": "bootstrap", "symbol": BTC, "tf": "1h"})
    assert r.status_code in (400, 503)  # 503 only while the real engine is not installed
    if r.status_code == 400:
        assert "No se pudieron obtener las velas" in r.json()["detail"]
    r = api_client.post("/api/sim/start", json={"mode": "replay", "symbol": BTC, "tf": "1h"})
    assert r.status_code == 400 and "No se pudo cargar el historial" in r.json()["detail"]
    res = api_client.post("/api/terminal", json={"text": f"backtest {BTC} 1h"}).json()
    assert res["ok"] is False


def test_simulator_uses_the_learned_gate_only_on_synthetic_markets(runtime_factory, contract_engine, offline):
    from app.sim.paper import PaperEngine
    from app.sim.simulator import Simulator
    from tests.test_sim_fakes import FakeHub

    async def scenario():
        async with runtime_factory() as rt:
            await rt.db.execute(
                "INSERT INTO model_state(kind, key, state, updates, trained_until, updated_at) "
                "VALUES ('gate', 'global', ?, 7, NULL, 1)", (json.dumps({"version": 1, "updates": 7}),))
            rt.hub = FakeHub(candles={(BTC, "1h"): generate_candleset(n=400, seed=9, end_time=FIXED_END, symbol=BTC)})
            rt.paper = PaperEngine(rt)
            await rt.paper.start()
            sim = Simulator(rt)
            await sim.start()
            await sim.start_session({"mode": "synthetic", "speed": 0.5, "warmup": 200})
            sim._paused = True
            await sim.step(sim._market.next_bar())
            assert CALLS[-1]["gate_state"] == {"version": 1, "updates": 7}
            await sim.start_session({"mode": "replay", "symbol": BTC, "speed": 0.5, "warmup": 300})
            sim._paused = True
            await sim.step(sim._next_bar())
            assert CALLS[-1]["gate_state"] is None  # real history stays out-of-sample
            await sim.stop_session(emit=False)
            await rt.paper.stop()

    asyncio.run(scenario())


def test_run_engine_legacy_signature_and_incomplete_engine(monkeypatch):
    from app.sim.common import EngineUnavailable

    seen = []

    def legacy(cs, params, prior=None):  # an engine build without the gate_state keyword
        seen.append(prior)
        return ContractResult(cs, params, prior)

    mod = types.ModuleType("app.engine.nexus")
    mod.NexusParams, mod.run_nexus = ContractParams, legacy
    monkeypatch.setitem(sys.modules, "app.engine.nexus", mod)
    cs = generate_candleset(n=300, seed=1, end_time=FIXED_END)
    resp, _ = run_engine(cs, ContractParams(), None, gate_state={"version": 1})
    assert resp["ok"] is True and seen == [None]

    def incomplete(cs, params, prior=None, *, gate_state=None):
        raise ModuleNotFoundError("No module named 'app.engine.nexus_loop'")

    mod.run_nexus = incomplete
    with pytest.raises(EngineUnavailable, match="Motor NEXUS no disponible"):
        run_engine(cs, ContractParams())


def test_long_session_keeps_a_recent_prior(runtime_factory, contract_engine, offline):
    """The engine snapshots the last bar of every run: the store must stay sparse so the window always finds a
    snapshot at or shortly before its first bar (long memory without lookahead)."""
    from app.sim.paper import PaperEngine
    from app.sim.simulator import SNAPSHOT_SPACING_BARS, Simulator
    from tests.test_sim_fakes import FakeHub

    async def scenario():
        async with runtime_factory() as rt:
            rt.hub = FakeHub()
            rt.paper = PaperEngine(rt)
            await rt.paper.start()
            sim = Simulator(rt)
            await sim.start()
            await sim.start_session({"mode": "synthetic", "speed": 0.5, "seed": 3, "warmup": 600})
            sim._paused = True
            for _ in range(160):
                await sim.step(sim._market.next_bar())
            later = CALLS[-40:]
            assert all(c["prior"] is not None for c in later)
            assert all(0 <= c["first"] - c["prior"]["upto"] <= 100 * 3600 for c in later)
            times = [t for t, _ in sim._snapshots]
            assert all(b - a >= SNAPSHOT_SPACING_BARS * 3600 for a, b in zip(times, times[1:]))
            await sim.stop_session(emit=False)
            await rt.paper.stop()

    asyncio.run(scenario())
