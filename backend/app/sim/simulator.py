"""Market simulator (`rt.sim`): one session at a time, synthetic or replay.

- synthetic: SyntheticMarket ("SIM:NEXUS"), regime switchable live (auto bull bear range volatile crash).
- replay: real history of any symbol revealed bar by bar; NEXUS only ever sees the revealed bars.

Each bar: push through the hub (charts on market:SIM:*:tf update too) → NEXUS on the last ≤ 600 revealed bars
(worker thread) → paper account 2 managed with the bar (SL/TP/partials/trailing) and auto-trading of TAKE
verdicts only, filled at the bar close → WS `sim` message (bar, current, new markers, positions) + events.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from app.data.base import CandleSet
from app.sim.common import (
    ACTION_LABEL,
    MARKER_EXIT_REASON,
    EngineUnavailable,
    build_nexus_params,
    current_settings,
    fmt_num,
    fmt_pct,
    fmt_prob,
    gate_line,
    load_engine,
    normalize_snapshots,
    run_engine,
)
from app.sim.synthetic import REGIMES, SyntheticConfig, SyntheticMarket
from app.util import RateLimitedLog, clamp, floor_time, normalize_tf, now_ms, now_s, safe_float, tf_seconds

NEXUS_WINDOW = 600
MAX_REVEALED = 1500
MAX_MARKERS = 2000
MIN_SPEED, MAX_SPEED = 0.5, 20.0
SYNTH_SYMBOL = "SIM:NEXUS"
REPLAY_PUSH_SYMBOL = "SIM:REPLAY"
DECISION_EXITS = {"EXIT_EARLY", "EXIT_SIGNAL", "EXIT_REVERSE", "EXIT_TIME"}
VALID_REGIMES = ("auto", *REGIMES.keys())


class SimError(ValueError):
    """Invalid simulator request (Spanish message)."""


def _default_nexus(cs: CandleSet, prior: Any) -> tuple[dict, Any]:
    _, nexus_params_cls = load_engine()
    params, _ = build_nexus_params(nexus_params_cls, current_settings(), None)
    return run_engine(cs, params, prior)


class Simulator:
    def __init__(self, rt: Any):
        self.rt = rt
        self._task: asyncio.Task | None = None
        self._control = asyncio.Lock()
        self._active = False
        self._paused = False
        self.mode: str | None = None
        self.symbol: str | None = None
        self.push_symbol: str | None = None
        self.tf: str | None = None
        self.speed = 4.0
        self.regime: str | None = None
        self.seed: int | None = None
        self.bars_done = 0
        self.bars_total: int | None = None
        self.started_at: int | None = None
        self.auto_trade = True
        self._market: SyntheticMarket | None = None
        self._replay: list[dict] = []
        self._replay_idx = 0
        self._revealed: list[dict] = []
        self._last_nexus: dict | None = None
        self._snapshots: list[tuple[int, Any]] = []
        self._prior_ok = True
        self._markers_seen: set[tuple[int, str]] = set()
        self._markers: list[dict] = []
        self._account: dict | None = None
        self._last_action: str | None = None
        self._event_window = (0.0, 0)
        self._log = RateLimitedLog()
        # Overridable for tests: (CandleSet, prior) -> (NexusResponse dict | None, snapshots)
        self.nexus_fn: Callable[[CandleSet, Any], tuple[dict | None, Any]] = _default_nexus

    # ------------------------------------------------------------------ service protocol

    async def start(self) -> None:
        self.speed = float(clamp(float(self._setting("sim.default_speed", 4.0) or 4.0), MIN_SPEED, MAX_SPEED))

    async def stop(self) -> None:
        await self.stop_session(emit=False)

    def status(self) -> dict:
        return {"running": self._active, "mode": self.mode, "bars_done": self.bars_done}

    # ------------------------------------------------------------------ helpers

    def _setting(self, key: str, default: Any = None) -> Any:
        if self.rt.settings is None:
            from app.config import DEFAULT_SETTINGS

            return DEFAULT_SETTINGS.get(key, default)
        return self.rt.settings.get(key, default)

    def _emit(self, level: str, message: str, data: Any = None, *, throttle: bool = False) -> None:
        if self.rt.bus is None:
            return
        if throttle:  # at high speed keep the terminal readable: ≤ 4 throttled events per second
            start, count = self._event_window
            now = time.monotonic()
            if now - start >= 1.0:
                start, count = now, 0
            if count >= 4:
                self._event_window = (start, count)
                return
            self._event_window = (start, count + 1)
        self.rt.bus.emit(level, "sim", message, data)

    def _publish(self, message: dict) -> None:
        if self.rt.ws is not None:
            try:
                self.rt.ws.publish("sim", {"type": "sim", **message})
            except Exception:
                pass

    @property
    def price(self) -> float | None:
        return float(self._revealed[-1]["close"]) if self._revealed else None

    # ------------------------------------------------------------------ state

    def state(self) -> dict:
        acct = self._account
        stats = None
        if acct is not None:
            stats = {"trades": int(acct.get("trades") or 0), "win_rate": acct.get("win_rate"),
                     "pnl_pct": float(acct.get("return_pct") or 0.0)}
        return {
            "running": self._active,
            "paused": self._paused,
            "mode": self.mode,
            "symbol": self.symbol,
            "tf": self.tf,
            "speed": self.speed,
            "regime": self.regime,
            "market_regime": self._market.regime if self._market is not None else None,
            "bars_done": self.bars_done,
            "bars_total": self.bars_total,
            "price": self.price,
            "started_at": self.started_at,
            "auto_trade": self.auto_trade,
            "account": acct,
            "last_action": self._last_action,
            "stats": stats,
            "seed": self.seed,
        }

    async def positions(self) -> list[dict]:
        paper = self.rt.paper
        if paper is None:
            return []
        open_pos = paper.sim_positions(None)
        history = await paper.history(2, 20)
        return open_pos + history

    async def snapshot(self) -> dict:
        return {
            "state": self.state(),
            "candles": list(self._revealed[-MAX_REVEALED:]),
            "nexus": self._last_nexus,
            "positions": await self.positions(),
        }

    async def _refresh_account(self) -> None:
        if self.rt.paper is not None:
            try:
                self._account = await self.rt.paper.account(2)
            except Exception:
                pass

    # ------------------------------------------------------------------ session control

    async def start_session(self, req: dict | None = None) -> dict:
        req = dict(req or {})
        async with self._control:
            mode = str(req.get("mode") or "synthetic").lower()
            if mode in ("sintetico", "sintético", "synth"):
                mode = "synthetic"
            if mode not in ("synthetic", "replay"):
                raise SimError("Modo no válido: usa 'synthetic' (sintético) o 'replay'")
            try:
                tf = normalize_tf(req.get("tf") or "1h")
            except ValueError as exc:
                raise SimError(str(exc)) from None
            speed = safe_float(req.get("speed"))
            speed = float(clamp(speed if speed is not None else float(self._setting("sim.default_speed", 4.0) or 4.0),
                                MIN_SPEED, MAX_SPEED))
            regime = str(req.get("regime") or "auto").lower()
            if regime not in VALID_REGIMES:
                raise SimError(f"Régimen desconocido: {regime} (usa {', '.join(VALID_REGIMES)})")
            seed_v = safe_float(req.get("seed"))
            seed = int(seed_v) if seed_v is not None else int(time.time()) % 1_000_000
            warmup = int(clamp(int(safe_float(req.get("warmup")) or 300), 50, 1000))
            start_price = safe_float(req.get("start_price")) or 100.0
            if start_price <= 0:
                raise SimError("El precio inicial debe ser mayor que 0")
            auto_trade = bool(req.get("auto_trade", True))
            initial_balance = safe_float(req.get("initial_balance"))
            if initial_balance is not None and initial_balance <= 0:
                raise SimError("El saldo inicial debe ser mayor que 0")

            replay: list[dict] = []
            market: SyntheticMarket | None = None
            if mode == "synthetic":
                symbol = push_symbol = SYNTH_SYMBOL
                tf_s = tf_seconds(tf)
                market = SyntheticMarket(SyntheticConfig(
                    seed=seed, start_price=start_price, tf=tf, regime=regime,
                    start_time=floor_time(now_s(), tf) - warmup * tf_s, symbol=symbol,
                ))
                warm = market.history(warmup)
                bars_total = None
            else:
                symbol = str(req.get("symbol") or "").strip().upper()
                if not symbol:
                    raise SimError("El replay necesita un símbolo (por ejemplo BINANCE:BTCUSDT)")
                if self.rt.hub is None:
                    raise SimError("Servicio de datos no disponible para el replay")
                try:
                    cs = await asyncio.wait_for(self.rt.hub.get_candles(symbol, tf, MAX_REVEALED), timeout=60)
                except asyncio.TimeoutError:
                    raise SimError(f"Tiempo agotado descargando {symbol} {tf}") from None
                candles = cs.to_list() if cs is not None else []
                if len(candles) < 80:
                    note = getattr(cs, "note", None)
                    raise SimError(f"Historial insuficiente para el replay de {symbol} {tf}: {len(candles)} velas"
                                   + (f" ({note})" if note else ""))
                warmup = int(min(warmup, len(candles) - 30))
                warm, replay = candles[:warmup], candles[warmup:]
                push_symbol = REPLAY_PUSH_SYMBOL
                bars_total = len(replay)
                regime = None

            await self._stop_locked(emit=False)
            self.mode, self.symbol, self.push_symbol, self.tf = mode, symbol, push_symbol, tf
            self.speed, self.regime, self.seed = speed, regime, seed
            self.auto_trade = auto_trade
            self._market, self._replay, self._replay_idx = market, replay, 0
            self._revealed = list(warm)[-MAX_REVEALED:]
            self.bars_done, self.bars_total = 0, bars_total
            self.started_at = now_ms()
            self._paused = False
            self._last_nexus = None
            self._snapshots = []
            self._prior_ok = True
            self._markers_seen, self._markers = set(), []
            self._last_action = None

            paper = self.rt.paper
            if paper is not None:
                await paper.reset(initial_balance, account_id=2)
                paper.set_sim_info(auto_trade=auto_trade, symbols=[symbol], tf=tf)
            await self._refresh_account()
            await self._push_bars(warm)
            resp = await self._compute_nexus()
            if resp:
                for m in resp.get("markers") or []:
                    key = (int(safe_float(m.get("time"), 0) or 0), str(m.get("kind")))
                    if key not in self._markers_seen:
                        self._markers_seen.add(key)
                        self._markers.append(m)
                cur = resp.get("current") or {}
                self._last_action = cur.get("action_label") or ACTION_LABEL.get(cur.get("action"), None)
            self._active = True
            self._task = asyncio.create_task(self._run(), name="sim-loop")

        desc = (f"sintético {symbol} {tf} · régimen {regime} · semilla {seed}" if mode == "synthetic"
                else f"replay de {symbol} {tf} · {bars_total} velas por revelar")
        self._emit("info", f"Simulación iniciada: {desc} · {fmt_num(speed, 1)} velas/s"
                           + (" · auto-trading de señales TOMAR" if auto_trade else ""))
        self._publish({"event": "started", "state": self.state()})
        return self.state()

    async def stop_session(self, *, emit: bool = True, reason: str | None = None) -> dict:
        async with self._control:
            return await self._stop_locked(emit=emit, reason=reason)

    async def _stop_locked(self, *, emit: bool = True, reason: str | None = None, from_loop: bool = False) -> dict:
        task = self._task
        was_active = self._active
        self._active = False
        self._task = None
        if task is not None and not from_loop and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if not was_active:
            return self.state()
        paper = self.rt.paper
        if paper is not None and self.price is not None:
            try:
                last_time = int(self._revealed[-1]["time"]) + tf_seconds(self.tf or "1h")
                if paper.sim_positions(None):
                    await paper.close_position(all=True, account_id=2, reason="MANUAL", label="Fin de la simulación",
                                               price=self.price, time_s=last_time)
            except Exception as exc:
                self._emit("warn", f"No se pudieron cerrar las posiciones simuladas: {exc}")
        await self._refresh_account()
        if emit:
            acct = self._account or {}
            trades = int(acct.get("trades") or 0)
            wr = acct.get("win_rate")
            prefix = reason or "Simulación detenida"
            self._emit(
                "success",
                f"{prefix}: {trades} operaciones, {fmt_num((wr or 0) * 100, 0)} % aciertos, "
                f"{fmt_pct(acct.get('return_pct'))} · {self.bars_done} velas",
                {"stats": acct},
            )
        self._publish({"event": "stopped", "state": self.state()})
        return self.state()

    async def configure(self, speed: float | None = None, regime: str | None = None, paused: bool | None = None,
                        auto_trade: bool | None = None) -> dict:
        async with self._control:
            if speed is not None:
                sp = safe_float(speed)
                if sp is None:
                    raise SimError("Velocidad no válida")
                self.speed = float(clamp(sp, MIN_SPEED, MAX_SPEED))
            if regime is not None:
                r = str(regime).lower()
                if not self._active or self.mode != "synthetic" or self._market is None:
                    raise SimError("El régimen solo se puede cambiar con una simulación sintética en curso")
                if r not in VALID_REGIMES:
                    raise SimError(f"Régimen desconocido: {r} (usa {', '.join(VALID_REGIMES)})")
                self._market.set_regime(r)
                self.regime = r
                self._emit("info", f"Régimen del mercado sintético cambiado a '{r}'")
            if paused is not None:
                if not self._active:
                    raise SimError("No hay ninguna simulación en curso")
                self._paused = bool(paused)
                self._emit("info", "Simulación en pausa" if self._paused else "Simulación reanudada")
            if auto_trade is not None:
                self.auto_trade = bool(auto_trade)
                if self.rt.paper is not None and self.symbol:
                    self.rt.paper.set_sim_info(auto_trade=self.auto_trade, symbols=[self.symbol], tf=self.tf or "1h")
            self._publish({"event": "state", "state": self.state()})
            return self.state()

    # ------------------------------------------------------------------ loop

    async def _run(self) -> None:
        try:
            while self._active:
                if self._paused:
                    await asyncio.sleep(0.1)
                    continue
                t0 = time.monotonic()
                bar = self._next_bar()
                if bar is None:
                    async with self._control:
                        await self._stop_locked(emit=True, reason="Replay terminado", from_loop=True)
                    return
                await self.step(bar)
                delay = 1.0 / max(self.speed, MIN_SPEED) - (time.monotonic() - t0)
                await asyncio.sleep(max(delay, 0.0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit("error", f"Simulación detenida por un error: {type(exc).__name__}: {exc}")
            async with self._control:
                await self._stop_locked(emit=False, from_loop=True)

    def _next_bar(self) -> dict | None:
        if self.mode == "synthetic" and self._market is not None:
            return self._market.next_bar()
        if self._replay_idx < len(self._replay):
            bar = self._replay[self._replay_idx]
            self._replay_idx += 1
            return bar
        return None

    async def _push_bars(self, bars: list[dict]) -> None:
        hub = self.rt.hub
        if hub is None or not hasattr(hub, "push_candle") or not self.push_symbol:
            return
        for bar in bars:
            try:
                await hub.push_candle(self.push_symbol, self.tf, bar, True)
            except Exception as exc:
                if self._log.should_log("push", 120):
                    self._emit("warn", f"No se pudo publicar la vela simulada en el hub: {exc}")
                return

    async def _compute_nexus(self) -> dict | None:
        window = self._revealed[-NEXUS_WINDOW:]
        if not window:
            return None
        cs = CandleSet.from_rows(window, self.symbol or SYNTH_SYMBOL, self.tf or "1h",
                                 provider="synthetic" if self.mode == "synthetic" else "replay",
                                 history="synthetic" if self.mode == "synthetic" else "full")
        prior = None
        if self._prior_ok and self._snapshots:
            first = int(cs.time[0])
            for t, state in reversed(self._snapshots):
                if t <= first:
                    prior = state
                    break
        try:
            resp, snaps = await asyncio.to_thread(self.nexus_fn, cs, prior)
        except EngineUnavailable as exc:
            if self._log.should_log("engine", 3600):
                self._emit("warn", f"{exc}: la simulación solo mostrará el precio")
            return None
        except Exception as exc:
            if prior is not None:
                self._prior_ok = False
                try:
                    resp, snaps = await asyncio.to_thread(self.nexus_fn, cs, None)
                except Exception as exc2:
                    exc = exc2
                    resp = None
            else:
                resp = None
            if resp is None:
                if self._log.should_log("nexus-error", 60):
                    self._emit("error", f"NEXUS falló en la simulación: {type(exc).__name__}: {exc}")
                return None
        if not isinstance(resp, dict):
            return None
        for t, state in normalize_snapshots(snaps):
            if not self._snapshots or t > self._snapshots[-1][0]:
                self._snapshots.append((t, state))
        if len(self._snapshots) > 40:
            self._snapshots = self._snapshots[-40:]
        resp.setdefault("symbol", self.symbol)
        resp.setdefault("tf", self.tf)
        resp.setdefault("provider", "synthetic" if self.mode == "synthetic" else "replay")
        resp.setdefault("history", "synthetic" if self.mode == "synthetic" else "full")
        resp.setdefault("note", None)
        resp.setdefault("quick", None)
        resp.setdefault("ok", True)
        resp.setdefault("message", None)
        self._last_nexus = resp
        return resp

    async def step(self, bar: dict) -> None:
        """Reveal one bar (public so tests can drive the simulator deterministically)."""
        prev_time = int(self._revealed[-1]["time"]) if self._revealed else None
        self._revealed.append(bar)
        if len(self._revealed) > MAX_REVEALED:
            del self._revealed[: len(self._revealed) - MAX_REVEALED]
        self.bars_done += 1
        await self._push_bars([bar])
        resp = await self._compute_nexus()
        current = (resp or {}).get("current") or None
        new_markers: list[dict] = []
        for m in (resp or {}).get("markers") or []:
            t = int(safe_float(m.get("time"), 0) or 0)
            key = (t, str(m.get("kind")))
            if key in self._markers_seen or (prev_time is not None and t < prev_time):
                continue
            self._markers_seen.add(key)
            new_markers.append(m)
        if new_markers:
            self._markers.extend(new_markers)
            if len(self._markers) > MAX_MARKERS:
                del self._markers[: len(self._markers) - MAX_MARKERS]
        paper = self.rt.paper
        if paper is not None:
            await paper.sim_bar(self.symbol, self.tf, bar)
            if self.auto_trade and current:
                await self._auto_manage(bar, current, new_markers)
            if current:
                self._attach_advice(current)
        if current:
            self._last_action = current.get("action_label") or ACTION_LABEL.get(current.get("action"))
        self._signal_events(new_markers, current)
        await self._refresh_account()
        self._publish({
            "event": "bar",
            "state": self.state(),
            "bar": bar,
            "current": current,
            "markers": new_markers,
            "positions": await self.positions(),
        })

    async def _auto_manage(self, bar: dict, current: dict, new_markers: list[dict]) -> None:
        paper = self.rt.paper
        tf_s = tf_seconds(self.tf or "1h")
        price = float(bar["close"])
        fill_time = int(bar["time"]) + tf_s
        action = str(current.get("action") or "")
        exits = [m for m in new_markers if str(m.get("kind")) in DECISION_EXITS]
        for pos in paper.sim_positions(self.symbol):
            against = action in (("EXIT_LONG", "SELL") if pos["side"] == "long" else ("EXIT_SHORT", "BUY"))
            if not against and not exits:
                continue
            kind = str(exits[0].get("kind")) if exits else ("EXIT_REVERSE" if action in ("SELL", "BUY") else "EXIT_SIGNAL")
            reason = MARKER_EXIT_REASON.get(kind, "SIGNAL")
            label = (exits[0].get("text") if exits else None) or {"EARLY": "Salida anticipada (NEXUS)",
                                                                  "REVERSE": "Señal contraria",
                                                                  "SIGNAL": "Señal de salida"}.get(reason)
            try:
                await paper.close_position(pos["id"], account_id=2, reason=reason, label=label, price=price,
                                           time_s=fill_time)
            except Exception as exc:
                self._emit("warn", f"No se pudo cerrar la posición simulada #{pos['id']}: {exc}")
        if paper.sim_positions(self.symbol):
            return
        if action == "BUY":
            side = "long"
        elif action == "SELL" and self._setting("nexus.allow_short", True):
            side = "short"
        else:
            return
        gate = ((current.get("gate") or {}).get(side)) or None
        if not isinstance(gate, dict) or gate.get("verdict") != "TAKE":
            return
        try:
            await paper.open_position(2, self.symbol, side, self.tf, origin="sim", price=price, gate=gate,
                                      check_gate=False, time_s=fill_time)
        except Exception as exc:
            if self._log.should_log("open", 30):
                self._emit("warn", f"No se pudo abrir la operación simulada: {exc}")

    def _attach_advice(self, current: dict) -> None:
        paper = self.rt.paper
        for pos in paper.sim_positions(self.symbol):
            paper.advise(pos["id"], current, self.price)

    def _signal_events(self, new_markers: list[dict], current: dict | None) -> None:
        sym, tf = self.symbol, self.tf
        for m in new_markers:
            kind = str(m.get("kind") or "")
            text = m.get("tooltip") or m.get("text") or ""
            if kind in ("BUY", "SELL") and current:
                side = "long" if kind == "BUY" else "short"
                gate = ((current.get("gate") or {}).get(side)) or None
                self._emit(
                    "signal",
                    f"{sym} {tf} → {ACTION_LABEL[kind]} · score {fmt_num(current.get('score'), 2, True)} · "
                    f"p↑ {fmt_prob(current.get('prob_up'))} · Filtro: {gate_line(gate)}",
                    {"marker": m},
                    throttle=True,
                )
            elif kind in ("REJECTED_LONG", "REJECTED_SHORT"):
                side = "compra" if kind == "REJECTED_LONG" else "venta"
                self._emit("info", f"{sym}: señal de {side} rechazada por el filtro" + (f" — {text}" if text else ""),
                           {"marker": m}, throttle=True)
            elif kind.startswith("EXIT_") or kind in ("TP1", "TP2", "TP3"):
                self._emit("signal", f"{sym}: {text or kind}", {"marker": m}, throttle=True)
