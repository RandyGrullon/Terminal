// "Mercado simulado" tab: live synthetic market or historical replay, with NEXUS + Trade Gate bar by bar.
import { useCallback, useEffect } from 'react'
import type { SimState, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { useApp } from '../../store'
import { ErrorBox } from '../ui'
import { SimChart } from './SimChart'
import { SimControls } from './SimControls'
import { SimPanel } from './SimPanel'
import { SimPositions } from './SimPositions'
import { SimStatusBar } from './SimStatusBar'
import { useSimFeed } from './simFeed'
import { clampSpeed, useSimStore, type LiveForm } from './simStore'

export function LiveSimulator() {
  const patchLive = useSimStore((s) => s.patchLive)
  const loadSettingsDefaults = useSimStore((s) => s.loadSettingsDefaults)
  const wsStatus = useApp((s) => s.wsStatus)

  // When a session is running (started here, from the terminal or before reloading the page),
  // mirror its parameters in the control bar so "Reiniciar" starts from what the user is looking at.
  const onSession = useCallback(
    (st: SimState) => {
      if (!st.running) return
      const patch: Partial<LiveForm> = {}
      if (st.mode === 'synthetic' || st.mode === 'replay') patch.mode = st.mode
      if (st.tf && (TIMEFRAMES as string[]).includes(st.tf)) patch.tf = st.tf as Timeframe
      if (st.mode === 'replay' && st.symbol) patch.symbol = st.symbol
      if (Number.isFinite(st.speed) && st.speed > 0) patch.speed = clampSpeed(st.speed)
      patch.autoTrade = !!st.auto_trade
      patchLive(patch, false)
    },
    [patchLive],
  )

  const { feed, view, loading, error } = useSimFeed(onSession)
  const state = view.state

  useEffect(() => {
    void loadSettingsDefaults()
  }, [loadSettingsDefaults])

  return (
    <div className="sim-live">
      <SimControls feed={feed} state={state} />
      <div className="sim-intro" title="Cómo funciona el simulador">
        Revela un mercado vela a vela (sintético o historial real): NEXUS solo ve las velas ya reveladas, el filtro decide TOMAR /
        ESPERAR / NO TOMAR en cada señal y, con auto-trading, solo se abren las operaciones TOMAR en la cuenta «Simulación» (papel).
      </div>
      {error && (
        <div className="sim-errorbar">
          <ErrorBox error={`No se pudo leer el estado del simulador: ${error}`} onRetry={() => void feed.resync('hard')} />
        </div>
      )}
      <div className="sim-main">
        <div className="sim-chart-col">
          <SimChart
            feed={feed}
            emas={view.emas}
            lastBar={view.lastBar}
            bars={view.bars}
            loading={loading}
            symbol={state?.symbol ?? null}
            tf={state?.tf ?? null}
          />
          <SimStatusBar state={state} wsStatus={wsStatus} />
        </div>
        <aside className="sim-side" aria-label="Señal y filtro">
          <SimPanel current={view.current} state={state} tf={state?.tf ?? null} />
        </aside>
      </div>
      <SimPositions open={view.open} closed={view.closed} tf={state?.tf ?? null} autoTrade={!!state?.auto_trade} />
    </div>
  )
}
