// Scanner controls (market, timeframe, limit, deep mode, top N) plus the live progress of the current run.
import { memo, useMemo } from 'react'
import { CircleCheck, Radar } from 'lucide-react'
import type { MarketInfo, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { fmtInt } from '../../lib/format'
import { Button, ErrorBox, Field, Meter, NumberInput, Select, Spinner, Toggle } from '../ui'
import { marketLabel } from '../markets/opsUtils'
import { clamp01 } from './scanUtils'

export interface ScanParams {
  market: string
  tf: Timeframe
  /** null = server default (scanner.limit) */
  limit: number | null
  deep: boolean
  /** null = server default (scanner.deep_top) */
  deepTop: number | null
}

export const DEFAULT_SCAN_PARAMS: ScanParams = { market: 'crypto', tf: '1h', limit: 300, deep: true, deepTop: 12 }

export function isScanParams(v: unknown): v is ScanParams {
  if (!v || typeof v !== 'object') return false
  const o = v as Record<string, unknown>
  const optNum = (x: unknown) => x === null || (typeof x === 'number' && Number.isFinite(x))
  return (
    typeof o.market === 'string' &&
    o.market !== '' &&
    typeof o.tf === 'string' &&
    (TIMEFRAMES as string[]).includes(o.tf) &&
    optNum(o.limit) &&
    typeof o.deep === 'boolean' &&
    optNum(o.deepTop)
  )
}

export interface ActiveRun {
  id: number
  market: string
  tf: string
  progress: number // 0..1
  message: string | null
  status: 'running' | 'done' | 'error'
  /** Launched from this page ("Escanear"); false for automatic background scans that are only followed. */
  startedByUser: boolean
}

interface Props {
  value: ScanParams
  onChange: (v: ScanParams) => void
  markets: MarketInfo[]
  marketsLoading: boolean
  starting: boolean
  active: ActiveRun | null
  error: string | null
  onRun: () => void
}

const TF_OPTIONS = TIMEFRAMES.map((t) => ({ value: t, label: t }))

function ScanControls({ value, onChange, markets, marketsLoading, starting, active, error, onRun }: Props) {
  const marketOptions = useMemo(() => {
    const opts = markets.map((m) => ({
      value: m.id,
      label: `${marketLabel(m.id, markets)}${typeof m.count === 'number' && m.count > 0 ? ` · ${fmtInt(m.count)}` : ''}`,
    }))
    if (!opts.some((o) => o.value === value.market)) opts.unshift({ value: value.market, label: marketLabel(value.market, markets) })
    return opts
  }, [markets, value.market])

  const running = active?.status === 'running'
  const set = (patch: Partial<ScanParams>) => onChange({ ...value, ...patch })

  return (
    <div>
      <div className="scn-controls">
        <Field label="Mercado">
          <Select value={value.market} onChange={(market) => set({ market })} options={marketOptions} title="Mercado de TradingView a escanear" />
        </Field>
        <Field label="Temporalidad">
          <Select value={value.tf} onChange={(tf) => set({ tf })} options={TF_OPTIONS} style={{ minWidth: 80 }} />
        </Field>
        <Field label="Límite">
          <NumberInput value={value.limit} onChange={(limit) => set({ limit })} min={5} max={1000} step={50} placeholder="300" />
        </Field>
        <Field label="Modo">
          <span className="scn-deep" title="Ejecuta NEXUS completo y el filtro (Trade Gate) sobre los mejores candidatos con historial completo">
            <Toggle checked={value.deep} onChange={(deep) => set({ deep })} label="Profundo (NEXUS + filtro)" />
          </span>
        </Field>
        <Field label="Top N">
          <div style={value.deep ? undefined : { opacity: 0.45, pointerEvents: 'none' }} title="Candidatos que pasan por el análisis profundo">
            <NumberInput value={value.deepTop} onChange={(deepTop) => set({ deepTop })} min={1} max={50} step={1} placeholder="12" />
          </div>
        </Field>
        <Button
          variant="primary"
          className="scn-run"
          icon={<Radar size={14} />}
          onClick={onRun}
          loading={starting}
          disabled={running || starting}
          title="Lanza un escaneo ahora (el escáner automático también corre en segundo plano)"
        >
          {running ? 'Escaneando…' : 'Escanear'}
        </Button>
      </div>
      {marketsLoading && (
        <div className="tiny muted row gap-1" style={{ marginTop: 6 }}>
          <Spinner /> Cargando mercados…
        </div>
      )}

      {active && (
        <div className="scn-progress" aria-live="polite">
          <div className="scn-progress-head">
            {active.status === 'running' ? <Spinner /> : active.status === 'done' ? <CircleCheck size={15} className="pos" /> : null}
            <span className="ellipsis">
              Escaneo #{active.id} · {marketLabel(active.market, markets)} {active.tf}
              {active.message ? ` — ${active.message}` : ''}
            </span>
            <span className="mono">{Math.round(clamp01(active.progress) * 100)} %</span>
          </div>
          <Meter
            value={active.status === 'done' ? 1 : clamp01(active.progress)}
            tone={active.status === 'error' ? 'neg' : active.status === 'done' ? 'pos' : 'info'}
            height={5}
          />
        </div>
      )}
      {error && (
        <div style={{ marginTop: 10 }}>
          <ErrorBox error={error} onRetry={running || starting ? undefined : onRun} />
        </div>
      )}
    </div>
  )
}

export default memo(ScanControls)
