// Backtest form (symbol, timeframe, history, capital, risk, costs, gate toggle and advanced NEXUS params).
import { useState } from 'react'
import { ChevronDown, ChevronRight, Play } from 'lucide-react'
import type { Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { Button, Field, NumberInput, Select, TextInput, Toggle } from '../ui'
import type { BacktestBody, BacktestForm as Form, SettingsDefaults } from './simStore'

const TF_OPTIONS = TIMEFRAMES.map((t) => ({ value: t, label: t }))

const inRange = (v: number | null, lo: number, hi: number): v is number => v !== null && Number.isFinite(v) && v >= lo && v <= hi

/** Validates the form and builds the POST /api/backtest body (error message in Spanish when invalid). */
export function buildBacktestBody(form: Form, appSymbol: string, appTf: Timeframe): { body: BacktestBody } | { error: string } {
  const symbol = (form.symbol ?? appSymbol).trim()
  if (!symbol) return { error: 'Indica el símbolo a probar (por ejemplo: btc, aapl, BINANCE:ETHUSDT).' }
  if (!inRange(form.limit, 100, 20000)) return { error: 'Velas: indica un número entre 100 y 20.000.' }
  if (!inRange(form.initialCapital, 1, 1e12)) return { error: 'El capital inicial debe ser mayor que 0.' }
  if (!inRange(form.riskPct, 0.01, 100)) return { error: 'Riesgo por operación: entre 0,01 % y 100 %.' }
  if (form.feePct !== null && !inRange(form.feePct, 0, 10)) return { error: 'Comisión: entre 0 % y 10 % (vacía = la de su clase de activo).' }
  if (!inRange(form.slippagePct, 0, 10)) return { error: 'Slippage: entre 0 % y 10 %.' }
  const params: Record<string, unknown> = {}
  if (form.entryThreshold !== null) {
    if (!inRange(form.entryThreshold, 0.01, 1)) return { error: 'Umbral de entrada: entre 0,01 y 1.' }
    params.entry_threshold = form.entryThreshold
  }
  if (form.slAtr !== null) {
    if (!inRange(form.slAtr, 0.1, 20)) return { error: 'SL en ATR: entre 0,1 y 20.' }
    params.sl_atr = form.slAtr
  }
  if (form.horizon !== null) {
    if (!inRange(form.horizon, 1, 500)) return { error: 'Horizonte: entre 1 y 500 velas.' }
    params.horizon = Math.round(form.horizon)
  }
  const body: BacktestBody = {
    symbol,
    tf: form.tf ?? appTf,
    limit: Math.round(form.limit),
    initial_capital: form.initialCapital,
    risk_pct: form.riskPct,
    slippage_pct: form.slippagePct,
    use_gate: form.useGate,
    params,
  }
  // Commission only when the user typed one: otherwise the server uses the asset class's fee (Ajustes › Riesgo,
  // risk.fee_pct_by_class) and reports the one used in result.params.fee_pct / params.notes.
  if (form.feePct !== null) body.fee_pct = form.feePct
  return { body }
}

const hint = (v: number | null | undefined, fallback: string) =>
  v === null || v === undefined ? fallback : v.toLocaleString('es-ES', { maximumFractionDigits: 4 })

export function BacktestForm(props: {
  form: Form
  appSymbol: string
  appTf: Timeframe
  defaults: SettingsDefaults | null
  loading: boolean
  error: string | null
  onChange: (patch: Partial<Form>) => void
  onSubmit: () => void
}) {
  const { form, onChange } = props
  const [advanced, setAdvanced] = useState(form.entryThreshold !== null || form.slAtr !== null || form.horizon !== null)
  const d = props.defaults

  return (
    <form
      className="col gap-3"
      // Validation is done by buildBacktestBody (Spanish messages); native step/min checks would block valid values.
      noValidate
      onSubmit={(e) => {
        e.preventDefault()
        if (!props.loading) props.onSubmit()
      }}
    >
      <div className="sim-form">
        <Field label="Símbolo" style={{ gridColumn: 'span 2' }}>
          <TextInput value={form.symbol ?? props.appSymbol} onChange={(v) => onChange({ symbol: v })} placeholder="btc, aapl, BINANCE:ETHUSDT…" mono />
        </Field>
        <Field label="Temporalidad">
          <Select<Timeframe> value={form.tf ?? props.appTf} onChange={(tf) => onChange({ tf })} options={TF_OPTIONS} />
        </Field>
        <Field label="Velas (historial)">
          <NumberInput value={form.limit} onChange={(v) => onChange({ limit: v })} min={100} max={20000} step={100} />
        </Field>
        <Field label="Capital inicial">
          <NumberInput value={form.initialCapital} onChange={(v) => onChange({ initialCapital: v })} min={1} step={500} />
        </Field>
        <Field label="Riesgo / operación %">
          <NumberInput value={form.riskPct} onChange={(v) => onChange({ riskPct: v })} min={0.01} max={100} step={0.25} />
        </Field>
        <Field label="Comisión %" hint="Vacío = la de su clase de activo">
          <NumberInput
            value={form.feePct}
            onChange={(v) => onChange({ feePct: v })}
            min={0}
            max={10}
            step={0.01}
            placeholder="Según la clase de activo (Ajustes)"
          />
        </Field>
        <Field label="Slippage %">
          <NumberInput value={form.slippagePct} onChange={(v) => onChange({ slippagePct: v })} min={0} max={10} step={0.01} />
        </Field>
      </div>

      <div className="row wrap gap-3">
        <Toggle checked={form.useGate} onChange={(v) => onChange({ useGate: v })} label="Usar filtro (gate)" />
        <span className="tiny muted">
          {form.useGate
            ? 'Solo se toman las señales con veredicto TOMAR; también se ejecuta la versión sin filtro para comparar.'
            : 'Se toman todas las señales de NEXUS, sin filtro (no habrá comparación).'}
        </span>
        <span className="grow" />
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setAdvanced((v) => !v)} aria-expanded={advanced}>
          {advanced ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Parámetros avanzados
        </button>
        <Button type="submit" variant="primary" icon={<Play size={14} />} loading={props.loading}>
          Ejecutar backtest
        </Button>
      </div>

      {advanced && (
        <div className="sim-form sim-form-advanced">
          <Field label="Umbral de entrada" hint={`Vacío = ajustes (${hint(d?.entryThreshold, '0,35')})`}>
            <NumberInput
              value={form.entryThreshold}
              onChange={(v) => onChange({ entryThreshold: v })}
              min={0.01}
              max={1}
              step={0.05}
              placeholder={hint(d?.entryThreshold, '0,35')}
            />
          </Field>
          <Field label="SL (múltiplo de ATR)" hint={`Vacío = ajustes (${hint(d?.slAtr, '2')})`}>
            <NumberInput value={form.slAtr} onChange={(v) => onChange({ slAtr: v })} min={0.1} max={20} step={0.25} placeholder={hint(d?.slAtr, '2')} />
          </Field>
          <Field label="Horizonte (velas)" hint={`Vacío = ajustes (${hint(d?.horizon, '5')})`}>
            <NumberInput value={form.horizon} onChange={(v) => onChange({ horizon: v })} min={1} max={500} step={1} placeholder={hint(d?.horizon, '5')} />
          </Field>
        </div>
      )}

      {props.error && <div className="errorbox">{props.error}</div>}
    </form>
  )
}
