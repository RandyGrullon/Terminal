// "Monte Carlo" tab: distribution of outcomes (bootstrap of real trades or synthetic markets), fan chart,
// histograms of final returns and max drawdowns, and risk figures (P(profit), P(ruin), VaR / CVaR).
import { useEffect, useMemo, useState } from 'react'
import { Dices, Info, Play } from 'lucide-react'
import type { MonteCarloResult, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { fmtInt, fmtNum, fmtPct, signTone, type Tone } from '../../lib/format'
import { plural } from '../../lib/plural'
import { useApp } from '../../store'
import { Badge, Button, Card, Disclaimer, Empty, ErrorBox, Field, NumberInput, Select, Stat, TextInput } from '../ui'
import { FanChart } from './FanChart'
import { Histogram, type RefLine } from './Histogram'
import { JobProgress } from './JobProgress'
import { MC_MODE_LABEL, asFraction, fmtRate } from './labels'
import { finiteValues, mean, quantileSorted } from './stats'
import { useSimStore, type MonteCarloBody, type MonteCarloForm } from './simStore'

const TF_OPTIONS = TIMEFRAMES.map((t) => ({ value: t, label: t }))
const MODE_OPTIONS = (Object.keys(MC_MODE_LABEL) as ('bootstrap' | 'synthetic')[]).map((m) => ({ value: m, label: MC_MODE_LABEL[m] }))

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const inRange = (v: number | null, lo: number, hi: number): v is number => v !== null && Number.isFinite(v) && v >= lo && v <= hi
const pctAxis = (v: number) => `${v.toLocaleString('es-ES', { maximumFractionDigits: 1 })} %`

function buildBody(form: MonteCarloForm, appSymbol: string, appTf: Timeframe): { body: MonteCarloBody } | { error: string } {
  if (!inRange(form.paths, 20, 5000)) return { error: 'Trayectorias: entre 20 y 5.000.' }
  if (!inRange(form.bars, 20, 10000)) return { error: 'Barras: entre 20 y 10.000.' }
  if (!inRange(form.initialCapital, 1, 1e12)) return { error: 'El capital debe ser mayor que 0.' }
  if (form.seed !== null && !Number.isFinite(form.seed)) return { error: 'La semilla debe ser un número entero.' }
  const body: MonteCarloBody = {
    mode: form.mode,
    // bootstrap: timeframe of the history to backtest · synthetic: timeframe of the generated markets
    tf: form.tf ?? appTf,
    paths: Math.round(form.paths),
    bars: Math.round(form.bars),
    initial_capital: form.initialCapital,
  }
  if (form.seed !== null) body.seed = Math.round(form.seed)
  if (form.mode === 'bootstrap') {
    const symbol = (form.symbol ?? appSymbol).trim()
    if (!symbol) return { error: 'Indica el símbolo cuyas operaciones se remuestrean.' }
    body.symbol = symbol
  }
  return { body }
}

/** Limits applied by the server per mode (shown as hints so the user knows what will actually run). */
const MODE_HINTS: Record<'bootstrap' | 'synthetic', { paths: string; bars: string }> = {
  bootstrap: { paths: 'Remuestreos (50 – 5.000)', bars: 'Velas de historial del backtest (1.000 – 5.000)' },
  synthetic: { paths: 'Mercados sintéticos (5 – 60)', bars: 'Velas por mercado (300 – 800)' },
}

/** Puts a risk figure in "return" convention (negative = loss) using the empirical tail as reference. */
function orientRisk(v: number | null | undefined, ref: number | null): number | null {
  if (!isNum(v)) return null
  if (ref === null) return v
  return Math.abs(v - ref) <= Math.abs(v + ref) ? v : -v
}

function riskView(r: MonteCarloResult) {
  const finals = finiteValues(r.final_returns_pct).sort((a, b) => a - b)
  const dds = finiteValues(r.max_drawdowns_pct)
    .map((v) => Math.abs(v))
    .sort((a, b) => a - b)
  const p5 = quantileSorted(finals, 0.05)
  const tail = finals.slice(0, Math.max(1, Math.floor(finals.length * 0.05)))
  const tailMean = finals.length ? mean(tail) : null
  return {
    finals,
    dds,
    var95: orientRisk(r.var95_pct, p5),
    cvar95: orientRisk(r.cvar95_pct, tailMean),
    worst: finals.length ? finals[0] : null,
    best: finals.length ? finals[finals.length - 1] : null,
    medianFinal: isNum(r.median_return_pct) ? r.median_return_pct : quantileSorted(finals, 0.5),
    medianDd: quantileSorted(dds, 0.5),
  }
}

function MonteCarloResults(props: { result: MonteCarloResult; body: MonteCarloBody | null; capital: number; dim: boolean }) {
  const { result: r, body } = props
  const risk = useMemo(() => riskView(r), [r])
  const pProfit = asFraction(r.prob_profit)
  const pRuin = asFraction(r.prob_ruin)
  const ruinTone: Tone = pRuin === null ? 'muted' : pRuin > 0.05 ? 'neg' : pRuin > 0.01 ? 'warn' : 'pos'

  const returnRefs: RefLine[] = [{ value: 0, label: '0', color: 'var(--muted)', dashed: false }]
  if (isNum(risk.medianFinal)) returnRefs.push({ value: risk.medianFinal, label: 'Mediana', color: 'var(--accent-2)' })
  if (isNum(risk.var95)) returnRefs.push({ value: risk.var95, label: 'VaR 95 %', color: 'var(--neg)' })
  const ddRefs: RefLine[] = []
  if (isNum(risk.medianDd)) ddRefs.push({ value: risk.medianDd, label: 'Mediana', color: 'var(--accent-2)' })
  ddRefs.push({ value: 50, label: 'Ruina (50 %)', color: 'var(--neg)' })

  const summary =
    pProfit !== null && isNum(risk.var95)
      ? `En el ${fmtRate(pProfit, 0)} de las trayectorias el capital termina por encima del inicial; en 1 de cada 20 casos el resultado es ${fmtPct(risk.var95, 1)} o peor.`
      : null

  return (
    <div className={`col gap-3 ${props.dim ? 'sim-dim' : ''}`}>
      <div className="sim-result-head">
        <Badge tone="accent">{r.mode === 'synthetic' ? 'Sintético' : 'Bootstrap'}</Badge>
        {r.mode === 'bootstrap' && body?.symbol && (
          <span className="mono bold">
            {body.symbol} <span className="muted">{body.tf}</span>
          </span>
        )}
        <span className="small text-2">
          {plural(r.paths, 'trayectoria', 'trayectorias')} × {plural(r.steps, 'paso', 'pasos')} · capital inicial {fmtNum(props.capital, 2)}
        </span>
      </div>

      <div className="sim-risk">
        <Stat
          label="P(beneficio)"
          value={fmtRate(r.prob_profit)}
          tone={pProfit === null ? 'muted' : pProfit >= 0.5 ? 'pos' : 'neg'}
          hint="Trayectorias que terminan en positivo"
        />
        <Stat label="P(ruina, DD ≥ 50 %)" value={fmtRate(r.prob_ruin)} tone={ruinTone} hint="Caída de al menos la mitad del capital" />
        <Stat label="Mediana" value={fmtPct(risk.medianFinal)} tone={signTone(risk.medianFinal)} hint="Resultado típico" />
        <Stat label="Esperado" value={fmtPct(r.expected_return_pct)} tone={signTone(r.expected_return_pct)} hint="Media de todas las trayectorias" />
        <Stat label="VaR 95 %" value={fmtPct(risk.var95)} tone={signTone(risk.var95)} hint="1 de cada 20 trayectorias termina peor" />
        <Stat label="CVaR 95 %" value={fmtPct(risk.cvar95)} tone={signTone(risk.cvar95)} hint="Media del 5 % de peores casos" />
      </div>
      <div className="sim-metrics-extra small">
        <span>
          <span className="muted">Mejor trayectoria</span> <b className={signTone(risk.best)}>{fmtPct(risk.best)}</b>
        </span>
        <span>
          <span className="muted">Peor trayectoria</span> <b className={signTone(risk.worst)}>{fmtPct(risk.worst)}</b>
        </span>
        <span>
          <span className="muted">Drawdown máximo mediano</span> <b className="neg">{fmtPct(isNum(risk.medianDd) ? -risk.medianDd || 0 : null)}</b>
        </span>
      </div>
      {summary && <div className="sim-summary">{summary}</div>}

      <FanChart result={r} capital={props.capital} />

      <div className="grid-2">
        <Card title="Rendimiento final" sub="Distribución del resultado de cada trayectoria" flush>
          <div className="sim-hist-wrap">
            <Histogram
              values={risk.finals}
              formatX={pctAxis}
              colorFor={(lo, hi) => (hi <= 0 ? 'var(--neg)' : lo >= 0 ? 'var(--pos)' : 'var(--warn)')}
              refLines={returnRefs}
              unit="trayectorias"
              ariaLabel="Histograma del rendimiento final por trayectoria"
            />
          </div>
        </Card>
        <Card title="Drawdown máximo" sub="Peor caída desde un máximo en cada trayectoria" flush>
          <div className="sim-hist-wrap">
            <Histogram
              values={risk.dds}
              formatX={pctAxis}
              colorFor={(lo) => (lo >= 50 ? 'var(--neg)' : lo >= 20 ? 'var(--warn)' : 'var(--info)')}
              refLines={ddRefs}
              unit="trayectorias"
              ariaLabel="Histograma del drawdown máximo por trayectoria"
            />
          </div>
        </Card>
      </div>

      {r.notes.length > 0 && (
        <Card
          title={
            <>
              <Info size={14} /> Notas
            </>
          }
        >
          <ul className="sim-notes">
            {r.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}

export function MonteCarloView() {
  const appSymbol = useApp((s) => s.symbol)
  const appTf = useApp((s) => s.tf)
  const form = useSimStore((s) => s.mcForm)
  const patchMc = useSimStore((s) => s.patchMc)
  const job = useSimStore((s) => s.mc)
  const runMonteCarlo = useSimStore((s) => s.runMonteCarlo)
  const loadSettingsDefaults = useSimStore((s) => s.loadSettingsDefaults)
  const [formError, setFormError] = useState<string | null>(null)

  useEffect(() => {
    void loadSettingsDefaults()
  }, [loadSettingsDefaults])

  const bootstrap = form.mode === 'bootstrap'
  const patch = (p: Partial<MonteCarloForm>) => {
    setFormError(null)
    patchMc(p)
  }

  const submit = () => {
    const built = buildBody(form, appSymbol, appTf)
    if ('error' in built) {
      setFormError(built.error)
      return
    }
    setFormError(null)
    void runMonteCarlo(built.body)
  }

  const retry = () => {
    if (job.attempt) void runMonteCarlo(job.attempt)
    else submit()
  }

  const capital = job.body?.initial_capital ?? form.initialCapital ?? 10000

  return (
    <>
      <Card
        title={
          <>
            <Dices size={14} /> Monte Carlo
          </>
        }
        sub="Miles de futuros posibles: ¿cuánto se puede ganar, cuánto se puede perder y con qué probabilidad?"
      >
        <form
          className="col gap-3"
          // Validation is done by buildBody (Spanish messages); native step/min checks would block valid values.
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            if (!job.loading) submit()
          }}
        >
          <div className="sim-form">
            <Field label="Modo" style={{ gridColumn: 'span 2' }}>
              <Select<'bootstrap' | 'synthetic'> value={form.mode} onChange={(mode) => patch({ mode })} options={MODE_OPTIONS} />
            </Field>
            {bootstrap ? (
              <Field label="Símbolo" style={{ gridColumn: 'span 2' }}>
                <TextInput
                  value={form.symbol ?? appSymbol}
                  onChange={(v) => patch({ symbol: v })}
                  placeholder="btc, aapl, BINANCE:ETHUSDT…"
                  mono
                />
              </Field>
            ) : (
              // Synthetic mode has no symbol to type: a caption (no <label> pointing at nothing) and static text.
              <div className="field" style={{ gridColumn: 'span 2' }}>
                <span className="label">Símbolo</span>
                <div className="input mono sim-static" title="El modo sintético genera sus propios mercados">
                  SIM:NEXUS (mercados generados)
                </div>
              </div>
            )}
            <Field label="Temporalidad">
              <Select<Timeframe> value={form.tf ?? appTf} onChange={(tf) => patch({ tf })} options={TF_OPTIONS} />
            </Field>
            <Field label="Trayectorias" hint={MODE_HINTS[form.mode].paths}>
              <NumberInput value={form.paths} onChange={(v) => patch({ paths: v })} min={20} max={5000} step={50} />
            </Field>
            <Field label="Barras" hint={MODE_HINTS[form.mode].bars}>
              <NumberInput value={form.bars} onChange={(v) => patch({ bars: v })} min={20} max={10000} step={50} />
            </Field>
            <Field label="Semilla">
              <NumberInput value={form.seed} onChange={(v) => patch({ seed: v })} step={1} placeholder="por defecto" />
            </Field>
            <Field label="Capital">
              <NumberInput value={form.initialCapital} onChange={(v) => patch({ initialCapital: v })} min={1} step={500} />
            </Field>
          </div>
          <div className="row wrap gap-3">
            <span className="tiny muted grow">
              {bootstrap
                ? 'Bootstrap: hace un backtest del símbolo y remuestrea sus operaciones reales en otro orden para ver la dispersión de resultados.'
                : 'Sintético: ejecuta NEXUS sobre muchos mercados simulados (semillas y regímenes distintos).'}
            </span>
            <Button type="submit" variant="primary" icon={<Play size={14} />} loading={job.loading}>
              Simular
            </Button>
          </div>
          {formError && <div className="errorbox">{formError}</div>}
        </form>
      </Card>

      {job.loading && (
        <JobProgress
          startedAt={job.startedAt}
          label={
            job.attempt?.mode === 'synthetic'
              ? `Ejecutando NEXUS en ${fmtInt(Math.min(60, Math.max(5, job.attempt.paths ?? 30)))} mercados sintéticos…`
              : `Backtest de ${job.attempt?.symbol ?? ''} y ${plural(job.attempt?.paths ?? null, 'remuestreo', 'remuestreos')}…`
          }
          hint={job.attempt?.mode === 'synthetic' ? 'Cada mercado ejecuta NEXUS completo: puede tardar hasta un minuto' : undefined}
        />
      )}
      {job.error && !job.loading && <ErrorBox error={`No se pudo ejecutar el Monte Carlo: ${job.error}`} onRetry={retry} />}
      {!job.result && !job.loading && !job.error && (
        <Card>
          <Empty icon={<Dices size={28} />} title="Todavía no hay simulaciones">
            Pulsa «Simular» para ver el abanico de resultados posibles, la probabilidad de terminar en beneficio y el riesgo de ruina.
          </Empty>
        </Card>
      )}
      {job.result && <MonteCarloResults result={job.result} body={job.body} capital={capital} dim={job.loading} />}
      <Disclaimer />
    </>
  )
}
