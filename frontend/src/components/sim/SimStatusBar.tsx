// Status bar of the live simulator: session state, bars processed, speed, regime and price.
import { memo } from 'react'
import { WifiOff } from 'lucide-react'
import type { NexusAction, SimState } from '../../api/types'
import type { WsStatus } from '../../api/ws'
import { ACTION_LABEL, fmtAgo, fmtInt, fmtPrice } from '../../lib/format'
import { Badge } from '../ui'
import { SIM_MODE_LABEL, fmtSpeed, simRegimeLabel, simRegimeTone, simStatus } from './labels'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Fields the simulator sends beyond the SimState contract (read defensively). */
type SimStateExtra = SimState & { market_regime?: string | null; seed?: number | null }

export const SimStatusBar = memo(function SimStatusBar(props: { state: SimState | null; wsStatus: WsStatus }) {
  const st = props.state as SimStateExtra | null
  const status = simStatus(st)
  // `regime` is the configured one ("auto"); `market_regime` is what the synthetic market is doing right now.
  const marketRegime = typeof st?.market_regime === 'string' && st.market_regime ? st.market_regime : null
  const regimeNow = marketRegime ?? st?.regime ?? null
  const regimeSuffix = marketRegime && st?.regime && st.regime !== marketRegime ? ` (${simRegimeLabel(st.regime).toLowerCase()})` : ''
  const done = isNum(st?.bars_done) ? st.bars_done : 0
  const total = st?.bars_total
  const progress = isNum(total) && total > 0 ? Math.min(1, Math.max(0, done / total)) : null
  const lastAction = st?.last_action ? ACTION_LABEL[st.last_action as NexusAction] ?? st.last_action : null

  return (
    <div className="sim-status" role="status">
      <span
        className="sim-status-item"
        title={st?.running && isNum(st.started_at) ? `Sesión iniciada ${fmtAgo(st.started_at)}` : undefined}
      >
        <span className={`sim-dot ${status.key}`} />
        <b className={status.tone}>{status.label}</b>
      </span>
      {st?.mode && <span className="sim-status-item">{SIM_MODE_LABEL[st.mode] ?? st.mode}</span>}
      {st?.symbol && (
        <span className="sim-status-item mono">
          {st.symbol}
          {st.tf ? ` · ${st.tf}` : ''}
        </span>
      )}
      <span className="sim-status-item" title="Velas reveladas y procesadas por NEXUS en esta sesión">
        <span className="muted">Barras</span>
        <span className="mono">
          {fmtInt(done)} / {isNum(total) ? fmtInt(total) : '∞'}
        </span>
        {progress !== null && (
          <span className="sim-progress" aria-label={`Progreso ${Math.round(progress * 100)} %`}>
            <span style={{ width: `${progress * 100}%` }} />
          </span>
        )}
      </span>
      <span className="sim-status-item" title="Velocidad de reproducción">
        <span className="mono">{fmtSpeed(st?.speed)}</span>
      </span>
      {(regimeNow || st?.mode !== 'replay') && (
        <span className="sim-status-item" title="Régimen actual del mercado simulado (entre paréntesis, el configurado)">
          <span className="muted">Régimen</span>
          <Badge tone={simRegimeTone(regimeNow)}>
            {simRegimeLabel(regimeNow)}
            {regimeSuffix}
          </Badge>
        </span>
      )}
      {st?.mode === 'synthetic' && isNum(st.seed) && (
        <span className="sim-status-item" title="Con la misma semilla se repite exactamente el mismo mercado">
          <span className="muted">Semilla</span>
          <span className="mono">{st.seed}</span>
        </span>
      )}
      <span className="sim-status-item">
        <span className="muted">Precio</span>
        <span className="mono">{fmtPrice(st?.price)}</span>
      </span>
      {lastAction && (
        <span className="sim-status-item" title="Última acción de NEXUS">
          <span className="muted">Acción</span>
          <span>{lastAction}</span>
        </span>
      )}
      <span className="grow" />
      {props.wsStatus !== 'open' && (
        <Badge tone="warn" title="Sin conexión WebSocket: el gráfico se actualizará al reconectar">
          <WifiOff size={11} /> Sin tiempo real
        </Badge>
      )}
    </div>
  )
})
