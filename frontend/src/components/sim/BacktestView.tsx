// "Backtest" tab: run NEXUS over history (causal signals, fees, slippage, partial exits) with and without the gate.
import { useEffect, useMemo, useState } from 'react'
import { ChartLine, TriangleAlert } from 'lucide-react'
import type { BacktestResult } from '../../api/types'
import { fmtBarTime, fmtInt } from '../../lib/format'
import { useApp } from '../../store'
import { Badge, Card, Disclaimer, Empty, ErrorBox } from '../ui'
import { BacktestForm, buildBacktestBody } from './BacktestForm'
import { BacktestMetrics } from './BacktestMetrics'
import { BacktestPriceChart } from './BacktestPriceChart'
import { EquityChart } from './EquityChart'
import { GateEffect } from './GateEffect'
import { JobProgress } from './JobProgress'
import { TradesTable } from './TradesTable'
import { useSimStore, type BacktestBody } from './simStore'

const PARAM_LABEL: Record<string, string> = {
  entry_threshold: 'Umbral de entrada',
  exit_threshold: 'Umbral de salida',
  sl_atr: 'SL (ATR)',
  horizon: 'Horizonte',
  trail_atr: 'Trailing (ATR)',
  time_stop_bars: 'Stop por tiempo',
  allow_short: 'Cortos',
}

const HISTORY_LABEL: Record<string, string> = {
  full: 'historial completo',
  keyed: 'proveedor con clave',
  recorder: 'grabado localmente',
  synthetic: 'sintético',
}

const asRecord = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.trim() !== '') : [])

/** NEXUS parameters actually used (the backend nests them under params.nexus; top-level keys as fallback). */
function paramChips(params: Record<string, unknown>): { key: string; text: string }[] {
  const source = asRecord(params.nexus) ?? params
  const out: { key: string; text: string }[] = []
  for (const k of Object.keys(PARAM_LABEL)) {
    const v = source[k]
    let text: string
    if (typeof v === 'boolean') text = v ? 'sí' : 'no'
    else if (typeof v === 'number' && Number.isFinite(v)) text = v.toLocaleString('es-ES', { maximumFractionDigits: 3 })
    else continue
    out.push({ key: k, text: `${PARAM_LABEL[k]}: ${text}` })
  }
  return out
}

/** Data source, ignored parameters and methodology notes sent with the result (disclaimers are shown once below). */
function resultInfo(params: Record<string, unknown>) {
  const provider = typeof params.provider === 'string' && params.provider ? params.provider : null
  const history = typeof params.history === 'string' ? HISTORY_LABEL[params.history] ?? params.history : null
  const ignored = strings(params.ignored_params)
  const notes = strings(params.notes).filter((n) => !/asesor/i.test(n))
  return { provider, history, ignored, notes }
}

function usedGateOf(result: BacktestResult, body: BacktestBody | null): boolean {
  if (body && typeof body.use_gate === 'boolean') return body.use_gate
  const p = result.params?.use_gate
  if (typeof p === 'boolean') return p
  return result.ungated !== null
}

function BacktestResults(props: { result: BacktestResult; body: BacktestBody | null; dim: boolean }) {
  const { result } = props
  const [selectedId, setSelectedId] = useState<number | null>(null)
  useEffect(() => setSelectedId(null), [result])
  const selected = useMemo(() => result.trades.find((t) => t.id === selectedId) ?? null, [result, selectedId])
  const usedGate = usedGateOf(result, props.body)
  const chips = paramChips(result.params ?? {})
  const info = resultInfo(result.params ?? {})

  return (
    <div className={`col gap-3 ${props.dim ? 'sim-dim' : ''}`}>
      <div className="sim-result-head">
        <span className="mono bold">{result.symbol}</span>
        <Badge tone="info">{result.tf}</Badge>
        <span className="small text-2">
          {fmtInt(result.bars)} velas · {fmtBarTime(result.start_time, result.tf)} → {fmtBarTime(result.end_time, result.tf)}
          {info.provider && ` · datos ${info.provider}${info.history ? ` (${info.history})` : ''}`}
        </span>
        <Badge tone={usedGate ? 'accent' : 'muted'}>{usedGate ? 'Con filtro (solo TOMAR)' : 'Sin filtro'}</Badge>
        {chips.map((c) => (
          <Badge key={c.key} outline>
            {c.text}
          </Badge>
        ))}
      </div>
      {info.ignored.length > 0 && (
        <div className="notice">
          <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
          <div>Parámetros no reconocidos por NEXUS (se ignoraron): {info.ignored.join(', ')}</div>
        </div>
      )}
      <BacktestMetrics metrics={result.metrics} />
      <div className="sim-bt-row">
        <GateEffect metrics={result.metrics} ungated={result.ungated} usedGate={usedGate} />
        <EquityChart result={result} />
      </div>
      <BacktestPriceChart result={result} selected={selected} onClearSelection={() => setSelectedId(null)} />
      <TradesTable trades={result.trades} tf={result.tf} selectedId={selectedId} onSelect={setSelectedId} />
      {info.notes.length > 0 && (
        <ul className="sim-notes tiny">
          {info.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function BacktestView() {
  const appSymbol = useApp((s) => s.symbol)
  const appTf = useApp((s) => s.tf)
  const form = useSimStore((s) => s.btForm)
  const patchBt = useSimStore((s) => s.patchBt)
  const job = useSimStore((s) => s.bt)
  const runBacktest = useSimStore((s) => s.runBacktest)
  const defaults = useSimStore((s) => s.defaults)
  const loadSettingsDefaults = useSimStore((s) => s.loadSettingsDefaults)
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    void loadSettingsDefaults()
  }, [loadSettingsDefaults])

  const submit = () => {
    const built = buildBacktestBody(form, appSymbol, appTf)
    if ('error' in built) {
      setFormError(built.error)
      return
    }
    setFormError(null)
    void runBacktest(built.body)
  }

  const retry = () => {
    if (job.attempt) void runBacktest(job.attempt)
    else submit()
  }

  return (
    <>
      <Card
        title={
          <>
            <ChartLine size={14} /> Backtest NEXUS
          </>
        }
        sub="Señales causales (cada vela solo usa el pasado), entrada en la apertura siguiente, comisiones, slippage y salidas parciales."
      >
        <BacktestForm
          form={form}
          appSymbol={appSymbol}
          appTf={appTf}
          defaults={defaults}
          loading={job.loading}
          error={formError}
          onChange={(p) => {
            setFormError(null)
            patchBt(p)
          }}
          onSubmit={submit}
        />
      </Card>

      {job.loading && (
        <JobProgress
          startedAt={job.startedAt}
          label={
            job.attempt?.use_gate
              ? `Ejecutando backtest de ${job.attempt.symbol} con y sin filtro…`
              : `Ejecutando backtest de ${job.attempt?.symbol ?? ''}…`
          }
        />
      )}
      {job.error && !job.loading && <ErrorBox error={`No se pudo ejecutar el backtest: ${job.error}`} onRetry={retry} />}
      {!job.result && !job.loading && !job.error && (
        <Card>
          <Empty icon={<ChartLine size={28} />} title="Todavía no hay resultados">
            Elige símbolo y temporalidad y pulsa «Ejecutar backtest». Con «Usar filtro (gate)» verás cuánto mejora el resultado al
            decir NO a las operaciones dudosas.
          </Empty>
        </Card>
      )}
      {job.result && <BacktestResults result={job.result} body={job.body} dim={job.loading} />}
      <Disclaimer />
    </>
  )
}
