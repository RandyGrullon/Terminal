// Control bar of the live market simulator (mode, symbol, timeframe, speed, regime, seed, auto-trading, actions).
import { useEffect, useRef, useState } from 'react'
import { Pause, Play, RotateCcw, SlidersHorizontal, Square } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { SimRegime, SimStartRequest, SimState, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { useApp } from '../../store'
import { Button, IconButton, NumberInput, Select, TextInput, Toggle } from '../ui'
import { SIM_REGIME_OPTIONS, fmtSpeed } from './labels'
import type { SimFeed } from './simFeed'
import { SPEED_MAX, SPEED_MIN, clampSpeed, useSimStore } from './simStore'

type Busy = 'start' | 'pause' | 'stop' | null

const TF_OPTIONS = TIMEFRAMES.map((t) => ({ value: t, label: t }))
const SPEED_DEBOUNCE_MS = 220

export function SimControls(props: { feed: SimFeed; state: SimState | null }) {
  const { feed, state } = props
  const live = useSimStore((s) => s.live)
  const patchLive = useSimStore((s) => s.patchLive)
  const appSymbol = useApp((s) => s.symbol)
  const notify = useApp((s) => s.notify)
  const [busy, setBusy] = useState<Busy>(null)
  const [showMore, setShowMore] = useState(false)
  const speedTimer = useRef<number | null>(null)
  const speedPending = useRef(false)

  const running = !!state?.running
  const paused = !!state?.paused
  const synthetic = live.mode === 'synthetic'
  const sessionSynthetic = running ? state?.mode !== 'replay' : synthetic
  const symbolText = live.symbol ?? appSymbol

  // Follow speed / auto-trading changes made elsewhere (terminal "sim ..." commands) while a session runs and the user
  // is idle. A stopped simulator keeps the user's own choice for the next start.
  useEffect(() => {
    if (!state || !state.running || speedPending.current) return
    if (Number.isFinite(state.speed) && state.speed > 0 && clampSpeed(state.speed) !== live.speed) {
      patchLive({ speed: clampSpeed(state.speed) }, false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.speed, state?.running])
  useEffect(() => {
    if (!state || !state.running) return
    if (state.auto_trade !== live.autoTrade) patchLive({ autoTrade: state.auto_trade }, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.auto_trade, state?.running])

  useEffect(
    () => () => {
      if (speedTimer.current !== null) window.clearTimeout(speedTimer.current)
    },
    [],
  )

  const configure = async (body: { speed?: number; regime?: string; paused?: boolean; auto_trade?: boolean }) => {
    try {
      const r = await api.sim.config(body)
      feed.applyState(r?.state)
      return true
    } catch (err) {
      notify(`No se pudo aplicar el cambio al simulador: ${errorMessage(err)}`, 'error')
      return false
    }
  }

  const onSpeed = (raw: number) => {
    const v = clampSpeed(raw)
    patchLive({ speed: v })
    if (!running) return
    speedPending.current = true
    if (speedTimer.current !== null) window.clearTimeout(speedTimer.current)
    speedTimer.current = window.setTimeout(() => {
      speedTimer.current = null
      void configure({ speed: v }).finally(() => {
        speedPending.current = false
      })
    }, SPEED_DEBOUNCE_MS)
  }

  const onRegime = (regime: SimRegime) => {
    patchLive({ regime })
    if (running && sessionSynthetic) {
      void configure({ regime }).then((ok) => ok && notify(`Régimen del mercado sintético: ${SIM_REGIME_OPTIONS.find((o) => o.value === regime)?.label ?? regime}`, 'info'))
    }
  }

  const onAutoTrade = (v: boolean) => {
    patchLive({ autoTrade: v })
    if (running) void configure({ auto_trade: v })
  }

  const start = async () => {
    if (!synthetic && !symbolText.trim()) {
      notify('Indica el símbolo que quieres reproducir (por ejemplo: btc, aapl, BINANCE:ETHUSDT).', 'warn')
      return
    }
    const body: SimStartRequest = {
      mode: live.mode,
      tf: live.tf,
      speed: live.speed,
      auto_trade: live.autoTrade,
    }
    if (synthetic) {
      body.regime = live.regime
      if (live.startPrice !== null && live.startPrice > 0) body.start_price = live.startPrice
    } else {
      body.symbol = symbolText.trim()
    }
    if (live.seed !== null && Number.isFinite(live.seed)) body.seed = Math.round(live.seed)
    if (live.warmup !== null && live.warmup >= 0) body.warmup = Math.round(live.warmup)
    if (live.initialBalance !== null && live.initialBalance > 0) body.initial_balance = live.initialBalance
    setBusy('start')
    try {
      // POST /api/sim/start replaces a running session by itself (one session at a time).
      const r = await api.sim.start(body)
      feed.applyState(r?.state, true)
      notify(synthetic ? 'Mercado sintético iniciado' : `Replay de ${body.symbol} iniciado`, 'success')
    } catch (err) {
      notify(`No se pudo iniciar la simulación: ${errorMessage(err)}`, 'error')
    } finally {
      setBusy(null)
    }
  }

  const togglePause = async () => {
    setBusy('pause')
    await configure({ paused: !paused })
    setBusy(null)
  }

  const stop = async () => {
    setBusy('stop')
    try {
      const r = await api.sim.stop()
      feed.applyState(r?.state)
    } catch (err) {
      notify(`No se pudo detener la simulación: ${errorMessage(err)}`, 'error')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="sim-controls-wrap">
      <div className="sim-controls">
        <div className="sim-ctl">
          <span>Modo</span>
          <div className="btn-group" role="group" aria-label="Modo de simulación">
            <button
              type="button"
              className={`btn btn-sm ${synthetic ? 'active' : ''}`}
              onClick={() => patchLive({ mode: 'synthetic' })}
              title="Mercado generado (régimen, volatilidad y saltos configurables)"
            >
              Sintético
            </button>
            <button
              type="button"
              className={`btn btn-sm ${!synthetic ? 'active' : ''}`}
              onClick={() => patchLive({ mode: 'replay' })}
              title="Historial real revelado vela a vela: NEXUS solo ve el pasado"
            >
              Replay histórico
            </button>
          </div>
        </div>

        {!synthetic && (
          <div className="sim-ctl sim-ctl-symbol">
            <span>Símbolo</span>
            <TextInput
              value={symbolText}
              onChange={(v) => patchLive({ symbol: v })}
              placeholder="btc, aapl, BINANCE:ETHUSDT…"
              mono
              onEnter={() => void start()}
            />
          </div>
        )}

        <div className="sim-ctl">
          <span>Temporalidad</span>
          <Select<Timeframe> value={live.tf} onChange={(tf) => patchLive({ tf })} options={TF_OPTIONS} title="Temporalidad de las velas" />
        </div>

        <div className="sim-ctl sim-ctl-speed">
          <span>
            Velocidad <b className="mono">{fmtSpeed(live.speed)}</b>
          </span>
          <input
            className="sim-range"
            type="range"
            min={SPEED_MIN}
            max={SPEED_MAX}
            step={0.5}
            value={live.speed}
            aria-label="Velocidad en velas por segundo"
            onChange={(e) => onSpeed(Number(e.target.value))}
          />
        </div>

        <div className="sim-ctl">
          <span>Régimen</span>
          <Select<SimRegime>
            value={live.regime}
            onChange={onRegime}
            options={SIM_REGIME_OPTIONS}
            disabled={!synthetic && !(running && sessionSynthetic)}
            title={synthetic ? 'Régimen del mercado sintético (se aplica en vivo)' : 'Solo para el modo sintético'}
          />
        </div>

        <div className="sim-ctl sim-ctl-seed">
          <span>Semilla</span>
          <NumberInput value={live.seed} onChange={(v) => patchLive({ seed: v })} step={1} placeholder="aleatoria" />
        </div>

        <div className="sim-ctl sim-ctl-toggle">
          <span>Cuenta «Simulación»</span>
          <Toggle checked={live.autoTrade} onChange={onAutoTrade} label="Auto-trading (solo TOMAR)" />
        </div>

        <IconButton
          icon={<SlidersHorizontal size={15} />}
          title={showMore ? 'Ocultar opciones avanzadas' : 'Opciones avanzadas (calentamiento, capital, precio inicial)'}
          active={showMore}
          onClick={() => setShowMore((v) => !v)}
        />

        <div className="sim-actions">
          <Button
            variant="primary"
            icon={running ? <RotateCcw size={14} /> : <Play size={14} />}
            loading={busy === 'start'}
            disabled={busy !== null && busy !== 'start'}
            onClick={() => void start()}
            title={running ? 'Detiene la sesión actual y empieza otra con estos parámetros' : 'Iniciar la simulación'}
          >
            {running ? 'Reiniciar' : 'Iniciar'}
          </Button>
          <Button
            icon={paused ? <Play size={14} /> : <Pause size={14} />}
            loading={busy === 'pause'}
            disabled={!running || (busy !== null && busy !== 'pause')}
            onClick={() => void togglePause()}
          >
            {paused ? 'Reanudar' : 'Pausar'}
          </Button>
          <Button
            variant="danger"
            icon={<Square size={13} />}
            loading={busy === 'stop'}
            disabled={!running || (busy !== null && busy !== 'stop')}
            onClick={() => void stop()}
          >
            Detener
          </Button>
        </div>
      </div>

      {showMore && (
        <div className="sim-controls sim-controls-more">
          <div className="sim-ctl sim-ctl-num">
            <span>Calentamiento (velas)</span>
            <NumberInput value={live.warmup} onChange={(v) => patchLive({ warmup: v })} min={0} step={10} placeholder="auto" />
          </div>
          <div className="sim-ctl sim-ctl-num">
            <span>Capital inicial</span>
            <NumberInput value={live.initialBalance} onChange={(v) => patchLive({ initialBalance: v })} min={0} step={500} placeholder="por defecto" />
          </div>
          {synthetic && (
            <div className="sim-ctl sim-ctl-num">
              <span>Precio inicial</span>
              <NumberInput value={live.startPrice} onChange={(v) => patchLive({ startPrice: v })} min={0} step={1} placeholder="100" />
            </div>
          )}
          <span className="tiny muted sim-more-hint">
            Se aplican al pulsar {running ? '«Reiniciar»' : '«Iniciar»'}. La velocidad, el régimen y el auto-trading cambian en vivo.
          </span>
        </div>
      )}
    </div>
  )
}
