// Walk-forward validation of NEXUS + Trade Gate on the "Aprendizaje" page: the last report per asset / timeframe, the
// comparison with the baselines (NEXUS without the gate, random entries, buy and hold), the honest verdict and a
// button that runs a new validation in the background (with progress). Honesty rules (ARCHITECTURE §0.9): "x de n",
// 95 % intervals, green only when the verdict is "Indicios de ventaja", never "prueba" nor "garantía".
import { Fragment, useCallback, useEffect, useRef, useState, type MouseEvent } from 'react'
import { ChevronDown, ChevronRight, FlaskConical, Play, Settings2 } from 'lucide-react'
import { errorMessage, request } from '../../api/client'
import { socket } from '../../api/ws'
import type {
  ValidationGroup,
  ValidationJob,
  ValidationLatestResponse,
  ValidationReport,
  ValidationRequestInfo,
  ValidationRunRequest,
  ValidationSeries,
  ValidationStatus,
  ValidationVerdict,
  ValidationVerdictCode,
} from '../../api/types'
import { EXIT_REASON_LABEL, fmtDateTime, fmtInt, fmtNum, fmtPct, fmtR, type Tone } from '../../lib/format'
import { plural } from '../../lib/plural'
import { Badge, Button, Card, Empty, ErrorBox, Field, Loading, Meter, NumberInput, TextInput } from '../ui'
import { MIN_EVIDENCE_N, fmtOf } from './evidence'
import './validation.css'

const POLL_MS = 2000
const TF_CHOICES = ['15m', '1h', '4h', '1d']
const WON: [string, string] = ['ganada', 'ganadas']

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const arr = <T,>(v: T[] | null | undefined): T[] => (Array.isArray(v) ? v : [])

/** Badge / banner tone of a verdict: only "edge" is green, a loss after costs red, the rest amber or grey. */
export function verdictTone(code: ValidationVerdictCode | string | null | undefined): Tone {
  if (code === 'edge') return 'pos'
  if (code === 'negative') return 'neg'
  if (code === 'insufficient') return 'muted'
  return 'warn'
}

/** "−0,12R a +0,08R" (or "—"). */
export function fmtCi(ci: readonly number[] | null | undefined): string {
  return ci && isNum(ci[0]) && isNum(ci[1]) ? `${fmtR(ci[0])} a ${fmtR(ci[1])}` : '—'
}

/** Below this many trades no 95 % interval is shown (backend app.validation.stats.MIN_CI_N): a bootstrap of a
 *  handful of trades is far narrower than the real uncertainty. Older stored reports may still carry one. */
export const MIN_CI_N = 10
export const NO_CI_TITLE =
  `Con menos de ${MIN_CI_N} operaciones no se calcula intervalo: ` + 'sería mucho más estrecho que la incertidumbre real.'
/** The drawdown compared with the random sequences: closed trades in time order, the same measure for both. */
const CLOSED_DD_TITLE =
  'Caída máxima de la curva de operaciones cerradas en orden temporal, con el mismo riesgo por operación: ' +
  'la misma medida para NEXUS y para el azar'

/** The interval of a sample of `n` trades: none below MIN_CI_N, "· muy incierto" below MIN_EVIDENCE_N (30). */
export function ciText(ci: readonly number[] | null | undefined, n: number | null | undefined): string {
  if (!isNum(n) || n <= 0) return '—'
  if (n < MIN_CI_N) return `sin intervalo (menos de ${MIN_CI_N})`
  const s = fmtCi(ci)
  if (s === '—') return 'sin intervalo'
  return n < MIN_EVIDENCE_N ? `${s} · muy incierto` : s
}

/** A secondary line with the interval of `n` trades (explained on hover when there is none). The interval itself never
 *  breaks inside a narrow cell; "· muy incierto" may go to the next line. */
function CiLine({ ci, n }: { ci: readonly number[] | null | undefined; n: number | null | undefined }) {
  const [main, ...rest] = ciText(ci, n).split(' · ')
  const why = /^sin intervalo (\(.+\))$/.exec(main) // "sin intervalo" / "(menos de 10)" on two lines when narrow
  return (
    <div className="tiny muted" title={isNum(n) && n > 0 && n < MIN_CI_N ? NO_CI_TITLE : undefined}>
      {why ? (
        <>
          sin intervalo <span className="nowrap">{why[1]}</span>
        </>
      ) : (
        <span className={main.startsWith('sin intervalo') ? undefined : 'nowrap'}>{main}</span>
      )}
      {rest.length > 0 && ` · ${rest.join(' · ')}`}
    </div>
  )
}

/** "4 de 6 ganadas" with "muestra pequeña" on a line of its own (narrow table cells). */
function WinsCell({ wins, n }: { wins: number | null | undefined; n: number | null | undefined }) {
  const [main, note] = winsOf(wins, n).split(' · ')
  return (
    <>
      {main}
      {note && <div className="tiny muted">{note}</div>}
    </>
  )
}

/** An expectancy is coloured only with enough trades and a 95 % interval that excludes 0. */
export function expectancyTone(n: number | null | undefined, ci: readonly number[] | null | undefined): Tone {
  if (!ci || !isNum(n) || n < MIN_EVIDENCE_N || !isNum(ci[0]) || !isNum(ci[1])) return 'muted'
  if (ci[0] > 0) return 'pos'
  if (ci[1] < 0) return 'neg'
  return 'muted'
}

/** "12 de 40 ganadas", plus "· muestra pequeña" below 30 trades. */
export function winsOf(wins: number | null | undefined, n: number | null | undefined): string {
  if (!isNum(n) || n <= 0) return 'sin operaciones'
  const s = fmtOf({ n, wins: isNum(wins) ? wins : 0 }, false, WON) ?? '—'
  return n < MIN_EVIDENCE_N ? `${s} · muestra pequeña` : s
}

/** The verdict text without its leading label ("Sin evidencia de ventaja: el intervalo…" -> "El intervalo…"). */
export function verdictDetail(v: ValidationVerdict | null | undefined): string {
  if (!v?.text) return ''
  const rest = v.label && v.text.startsWith(v.label) ? v.text.slice(v.label.length).replace(/^[:\s]+/, '') : v.text
  return rest ? rest[0].toUpperCase() + rest.slice(1) : ''
}

/** "3 de 10 series · Validando BINANCE:ETHUSDT 4h (5000 velas)…" */
export function progressText(job: ValidationJob | null | undefined): string {
  if (!job) return ''
  const head = `${fmtInt(job.done)} de ${plural(job.total, 'serie', 'series')}`
  return job.message ? `${head} · ${job.message}` : head
}

const HISTORY_LABEL: Record<string, string> = {
  keyed: 'proveedor con clave',
  recorder: 'grabado localmente',
  synthetic: 'mercado sintético',
  none: 'sin historial',
}
const HIDDEN_PROVIDERS = new Set(['synthetic', 'recorder']) // said by the history label, in Spanish

/** Where the bars come from: the exchange / provider and, when it is not its full history, which kind of history
 *  ("binance", "mercado sintético", "grabado localmente"). Empty when unknown. */
export function dataSource(s: { provider?: string | null; history?: string | null }): string {
  const parts: string[] = []
  if (s.provider && !HIDDEN_PROVIDERS.has(s.provider)) parts.push(s.provider)
  if (s.history && s.history !== 'full') parts.push(HISTORY_LABEL[s.history] ?? s.history)
  return parts.join(' · ')
}

/** After a 'validation' event: true when a job this card never saw running has finished (a quick one started from
 *  the terminal can end inside the event debounce) and the report shown is older than that job. */
export function finishedUnseen(
  st: ValidationStatus | null | undefined,
  shown: ValidationReport | null | undefined,
  sawRunning: boolean,
): boolean {
  const job = st?.job
  if (!st || st.running || sawRunning || !job || job.state !== 'done') return false
  return !shown || !isNum(shown.started_at) || !isNum(job.started_at) || shown.started_at < job.started_at
}

function fmtDay(sec: number | null | undefined): string {
  if (!isNum(sec)) return '—'
  return new Date(sec * 1000).toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit', year: '2-digit', timeZone: 'UTC' })
}

function bandText(band: readonly number[] | null | undefined): string {
  return band && band.length === 3 ? `${fmtR(band[0])} a ${fmtR(band[2])}` : '—'
}

function pctText(p: number | null | undefined): string {
  return isNum(p) ? fmtNum(p, 0) : '—'
}

export function VerdictBanner({ verdict }: { verdict: ValidationVerdict }) {
  const tone = verdictTone(verdict.code)
  return (
    <div className={`val-verdict val-${tone}`} role="status">
      <strong className={tone === 'muted' ? undefined : tone}>{verdict.label}</strong>
      <span>{verdictDetail(verdict)}</span>
    </div>
  )
}

export function ValidationProgress({ job }: { job: ValidationJob }) {
  const share = job.total > 0 ? Math.min(1, job.done / job.total) : 0
  return (
    <div className="val-progress" aria-live="polite">
      <div className="row between small wrap gap-2">
        <strong>Validando…</strong>
        <span className="tiny muted mono">
          {plural(arr(job.request?.symbols).length, 'símbolo', 'símbolos')} × {arr(job.request?.tfs).join(', ')} ·{' '}
          {plural(job.request?.bars, 'vela', 'velas')}
        </span>
      </div>
      <Meter value={share} />
      <span className="tiny muted">{progressText(job)}</span>
    </div>
  )
}

function GroupsTable({ groups }: { groups: ValidationGroup[] }) {
  return (
    <div className="table-wrap">
      <table className="table val-table">
        <thead>
          <tr>
            <th>Conjunto</th>
            <th className="num">Operaciones</th>
            <th className="num" title="Intervalo del 95 % remuestreando bloques de tiempo de todos los activos a la vez">
              Expectativa · IC 95 % por bloques
            </th>
            <th className="num" title="R medio por encima de una entrada al azar del mismo periodo y lado">Sobre el azar</th>
            <th className="num" title="NEXUS tomando todas las señales: expectativa e intervalo del 95 % por bloques">Sin filtro</th>
            <th className="num">Series positivas</th>
            <th className="num" title="Activos equivalentes independientes según la correlación diaria media">Activos</th>
            <th>Veredicto</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <tr key={g.key}>
              <td>
                {g.label}
                <div className="tiny muted">{arr(g.tfs).join(', ')}</div>
              </td>
              <td className="num">
                <WinsCell wins={g.wins} n={g.trades} />
              </td>
              <td className={`num ${expectancyTone(g.trades, g.ci95_blocks)}`}>
                {fmtR(g.expectancy_r)}
                <CiLine ci={g.ci95_blocks} n={g.trades} />
              </td>
              <td className={`num ${expectancyTone(g.trades, g.excess_ci95)}`}>
                {fmtR(g.excess_r)}
                <CiLine ci={g.excess_ci95} n={g.trades} />
              </td>
              <td className={`num ${g.ungated ? expectancyTone(g.ungated.trades, g.ungated.ci95_blocks) : 'muted'}`}>
                {fmtR(g.ungated?.expectancy_r ?? g.ungated_expectancy_r)}
                {g.ungated && (
                  <>
                    <CiLine ci={g.ungated.ci95_blocks} n={g.ungated.trades} />
                    <div className="tiny muted" title={g.ungated.verdict?.text}>
                      {plural(g.ungated.trades, 'operación', 'operaciones')}
                    </div>
                  </>
                )}
              </td>
              <td className="num">
                {fmtInt(g.positive_series)} de {fmtInt(g.series)}
              </td>
              <td className="num">
                {fmtInt(g.assets)}
                {g.assets > 1 && isNum(g.effective_assets) && <div className="tiny muted">≈ {fmtNum(g.effective_assets, 1)} indep.</div>}
              </td>
              <td>
                <Badge tone={verdictTone(g.verdict?.code)} title={g.verdict?.text}>
                  {g.verdict?.label ?? '—'}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Where the result comes from for one group: the entries alone (same mechanical exits as the random entries) and
 *  what NEXUS' own exits add on the same entries. Shown only when the server sends the split. */
export function ExitSplit({ group }: { group: ValidationGroup }) {
  const rows = [
    { name: 'Con filtro', v: group as ValidationGroup | ValidationGroup['ungated'] },
    { name: 'Sin filtro', v: group.ungated },
  ].filter((r) => r.v && isNum(r.v.mechanical_excess_r))
  if (!rows.length) return null
  return (
    <div className="val-box">
      <h4>{group.label}: entradas frente a salidas</h4>
      {rows.map(({ name, v }) => (
        <span key={name}>
          <strong>{name}</strong> · entradas sobre el azar con las mismas salidas{' '}
          <span className={expectancyTone(v?.trades, v?.mechanical_excess_ci95)}>{fmtR(v?.mechanical_excess_r)}</span> (
          {ciText(v?.mechanical_excess_ci95, v?.trades)}) · efecto de las salidas del modelo{' '}
          <span className={expectancyTone(v?.trades, v?.exit_effect_ci95)}>{fmtR(v?.exit_effect_r)}</span> (
          {ciText(v?.exit_effect_ci95, v?.trades)})
        </span>
      ))}
      <span className="tiny muted">
        R por operación, intervalo del 95 % por bloques de tiempo. «Mismas salidas»: stop, objetivos y cierre por tiempo de NEXUS,
        sin sus salidas anticipadas.
      </span>
    </div>
  )
}

function exitText(reasons: Record<string, number> | null | undefined): string {
  const parts = Object.entries(reasons ?? {})
    .filter(([, n]) => isNum(n) && n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${fmtInt(n)} ${(EXIT_REASON_LABEL[k] ?? k).toLowerCase()}`)
  return parts.length ? parts.join(' · ') : '—'
}

function SeriesDetail({ s }: { s: ValidationSeries }) {
  const o = s.overall.nexus
  const m = s.overall.mechanical
  const bh = s.overall.buy_hold
  const rnd = s.overall.random
  const source = dataSource(s)
  return (
    <div className="val-detail">
      <span className="small">{s.verdict.text}</span>
      <div className="table-wrap">
        <table className="table val-table">
          <thead>
            <tr>
              <th>Periodo</th>
              <th className="num">Operaciones</th>
              <th className="num">Expectativa · IC 95 %</th>
              <th className="num">Sin filtro</th>
              <th className="num">Azar p5 a p95</th>
              <th className="num">Percentil</th>
              <th className="num">Comprar y mantener</th>
            </tr>
          </thead>
          <tbody>
            {arr(s.folds).map((f) => (
              <tr key={f.index}>
                <td>
                  P{f.index} <span className="tiny muted">{fmtDay(f.start_time)} a {fmtDay(f.end_time)}</span>
                </td>
                <td className="num">
                  <WinsCell wins={f.nexus.wins} n={f.nexus.trades} />
                </td>
                <td className="num">
                  {fmtR(f.nexus.expectancy_r)}
                  <CiLine ci={f.nexus.ci95} n={f.nexus.trades} />
                </td>
                <td className="num">{fmtR(f.ungated.expectancy_r)}</td>
                <td className="num">{bandText(f.random.expectancy_band)}</td>
                <td className="num">{pctText(f.random.percentile)}</td>
                <td className="num">{fmtPct(f.buy_hold.return_pct, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="val-detail-grid">
        <div className="val-box">
          <h4>Cuenta simulada ({fmtNum(s.risk_pct, Number.isInteger(s.risk_pct) ? 0 : 2)} % de riesgo por operación)</h4>
          <span>
            NEXUS + filtro: {fmtPct(o.return_pct, 1)} · DD máx {fmtPct(o.max_dd_pct, 1, false)} · Sharpe {fmtNum(o.sharpe, 2)}
          </span>
          <span>
            Comprar y mantener: {fmtPct(bh.return_pct, 1)} · DD máx {fmtPct(bh.max_dd_pct, 1, false)} · Sharpe {fmtNum(bh.sharpe, 2)}
          </span>
          <span className="tiny muted">
            Las dos se valoran vela a vela. Comprar y mantener arriesga todo el capital; NEXUS, un % por operación.
          </span>
        </div>
        <div className="val-box">
          <h4>Frente al azar ({fmtInt(rnd.runs)} secuencias)</h4>
          <span>
            Azar: {fmtR(rnd.expectancy_r)} de media ({bandText(rnd.expectancy_band)}) · percentil de NEXUS {pctText(rnd.percentile)}
          </span>
          <span>
            Mismas salidas: entradas de NEXUS con las salidas mecánicas {fmtR(m.expectancy_r)} ({ciText(m.ci95, m.trades)}) · percentil{' '}
            {pctText(rnd.percentile_mechanical)}
          </span>
          {o.trades > 0 && isNum(rnd.trade_max_dd_p50) && (
            <span title={CLOSED_DD_TITLE}>
              Caída máx. por operaciones cerradas: NEXUS {fmtPct(o.trade_max_dd_pct, 1, false)} · azar (mediana){' '}
              {fmtPct(rnd.trade_max_dd_p50, 1, false)}
            </span>
          )}
          <span className="tiny muted">
            Hace falta un percentil de 95 o más para hablar de ventaja; 50 es lo esperable sin ninguna habilidad.
          </span>
        </div>
        {s.verdict_ungated && (
          <div className="val-box">
            <h4>NEXUS sin filtro frente a su propio azar</h4>
            <span>
              <Badge tone={verdictTone(s.verdict_ungated.code)}>{s.verdict_ungated.label}</Badge>
            </span>
            <span>{verdictDetail(s.verdict_ungated)}</span>
            <span className="tiny muted">
              {winsOf(s.overall.ungated.wins, s.overall.ungated.trades)} · azar {bandText(s.overall.random_ungated?.expectancy_band)}
            </span>
          </div>
        )}
        <div className="val-box">
          <h4>Filtro «¿Tomo esta operación?»</h4>
          <span>{s.gate_effect.text}</span>
          <span className="tiny muted">
            Señales rechazadas en todo el historial (calentamiento incluido): {fmtInt(s.rejected_signals)} · salidas:{' '}
            {exitText(s.exit_reasons)}
          </span>
        </div>
        <div className="val-box">
          <h4>Datos</h4>
          <span>
            {plural(s.bars, 'vela', 'velas')} de {fmtDay(s.start_time)} a {fmtDay(s.end_time)} · se mide desde {fmtDay(s.test_start_time)}
          </span>
          <span className="tiny muted">
            {source ? `${source} · ` : ''}comisión {fmtNum(s.fee_pct, 3)} % por lado · deslizamiento {fmtNum(s.slippage_pct, 2)} % ·{' '}
            {isNum(s.gaps) ? `${plural(s.gaps, 'hueco', 'huecos')} · ` : ''}
            {plural(s.open_trades, 'operación abierta', 'operaciones abiertas')} al final (excluidas)
          </span>
          {s.note && <span className="tiny warn">{s.note}</span>}
        </div>
      </div>
    </div>
  )
}

function SeriesTable(props: { series: ValidationSeries[]; open: string | null; onToggle: (key: string) => void }) {
  return (
    <div className="table-wrap">
      <table className="table val-table">
        <thead>
          <tr>
            <th>Serie</th>
            <th className="num">Operaciones</th>
            <th className="num">Expectativa · IC 95 %</th>
            <th className="num">Sin filtro</th>
            <th className="num" title="Expectativa de las secuencias de entradas al azar (percentiles 5 y 95)">Azar p5 a p95</th>
            <th className="num" title="Posición de NEXUS + filtro dentro de las secuencias al azar (50 = como el azar)">Percentil</th>
            <th className="num">Comprar y mantener</th>
            <th>Veredicto</th>
          </tr>
        </thead>
        <tbody>
          {props.series.map((s) => {
            const key = `${s.symbol}|${s.tf}`
            const isOpen = props.open === key
            const o = s.overall.nexus
            const toggle = (e?: MouseEvent) => {
              e?.stopPropagation()
              props.onToggle(key)
            }
            return (
              <Fragment key={key}>
                <tr className="clickable" onClick={() => props.onToggle(key)}>
                  <td className="mono">
                    <button type="button" className="val-row-btn" aria-expanded={isOpen} onClick={toggle}>
                      {isOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                      {s.symbol} <span className="muted">{s.tf}</span>
                    </button>
                    {s.note && (
                      <div className="tiny warn" title={s.note}>
                        {s.history === 'synthetic' ? 'datos simulados' : 'aviso sobre los datos'}
                      </div>
                    )}
                  </td>
                  <td className="num">
                    <WinsCell wins={o.wins} n={o.trades} />
                  </td>
                  <td className={`num ${expectancyTone(o.trades, o.ci95)}`}>
                    {fmtR(o.expectancy_r)}
                    <CiLine ci={o.ci95} n={o.trades} />
                  </td>
                  <td className={`num ${expectancyTone(s.overall.ungated.trades, s.overall.ungated.ci95)}`}>
                    {fmtR(s.overall.ungated.expectancy_r)}
                    <div className="tiny muted">
                      {plural(s.overall.ungated.trades, 'operación', 'operaciones')}
                      {isNum(s.overall.random_ungated?.percentile) ? ` · percentil ${pctText(s.overall.random_ungated?.percentile)}` : ''}
                    </div>
                  </td>
                  <td className="num">{bandText(s.overall.random.expectancy_band)}</td>
                  <td className="num">{pctText(s.overall.random.percentile)}</td>
                  <td className="num">
                    {fmtPct(s.overall.buy_hold.return_pct, 1)}
                    <div className="tiny muted">DD {fmtPct(s.overall.buy_hold.max_dd_pct, 1, false)}</div>
                  </td>
                  <td>
                    <Badge tone={verdictTone(s.verdict.code)} title={s.verdict.text}>
                      {s.verdict.label}
                    </Badge>
                  </td>
                </tr>
                {isOpen && (
                  <tr className="val-detail-row">
                    <td colSpan={8}>
                      <SeriesDetail s={s} />
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** "5 periodos", or "hasta 5 periodos" when a short series was split into fewer (each needs ≥ 100 decision bars). */
export function foldsText(configured: number | null | undefined, series: readonly ValidationSeries[]): string {
  const fewer = series.some((s) => isNum(configured) && arr(s.folds).length < configured)
  return `${fewer ? 'hasta ' : ''}${fmtInt(configured)} periodos`
}

/** The last report: headline verdict, groups, one row per series (expandable), method and disclaimer. */
export function ValidationReportView(props: { report: ValidationReport; open?: string | null; onToggle?: (key: string) => void }) {
  const r = props.report
  const series = arr(r.series).filter((s) => s && s.overall && s.verdict)
  const groups = arr(r.groups).filter((g) => g && g.verdict)
  const errors = arr(r.errors)
  const meta = [
    fmtDateTime(r.generated_at),
    plural(series.length, 'serie', 'series'),
    `${fmtInt(r.request?.bars)} velas por serie`,
    foldsText(r.config?.folds, series),
    `${fmtInt(r.config?.random_runs)} secuencias al azar`,
    r.source === 'cli' ? 'ejecutada desde la línea de comandos' : null,
  ].filter(Boolean)
  return (
    <div className="col gap-3">
      {r.summary && <VerdictBanner verdict={r.summary} />}
      <span className="tiny muted">{meta.join(' · ')}</span>
      {groups.length > 0 && <GroupsTable groups={groups} />}
      {groups.length > 0 && <ExitSplit group={groups[groups.length - 1]} />}
      {series.length > 0 && <SeriesTable series={series} open={props.open ?? null} onToggle={props.onToggle ?? (() => undefined)} />}
      {errors.length > 0 && (
        <div className="notice">
          <div className="col gap-1">
            <strong>{plural(errors.length, 'serie sin validar', 'series sin validar')}</strong>
            {errors.map((e, i) => (
              <span key={i} className="tiny">
                {e.symbol} {e.tf}: {e.message}
              </span>
            ))}
          </div>
        </div>
      )}
      <details className="val-method">
        <summary>Cómo se mide (método)</summary>
        <ul>
          {arr(r.method).map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      </details>
      <p className="disclaimer">{r.disclaimer}</p>
    </div>
  )
}

const splitSymbols = (text: string): string[] =>
  text
    .split(/[,;\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)

function RunForm(props: { defaults: ValidationRequestInfo; disabled: boolean; onRun: (body: ValidationRunRequest) => void }) {
  const [symbols, setSymbols] = useState(arr(props.defaults.symbols).join(', '))
  const [tfs, setTfs] = useState<string[]>(arr(props.defaults.tfs))
  const [bars, setBars] = useState<number | null>(props.defaults.bars ?? 3000)
  const toggle = (tf: string) => setTfs((cur) => (cur.includes(tf) ? cur.filter((x) => x !== tf) : [...cur, tf]))
  const valid = tfs.length > 0 && isNum(bars) && bars >= 1000 && bars <= 5000
  return (
    <div className="val-form">
      <Field label="Símbolos" hint="Separados por comas: BINANCE:BTCUSDT, eth, sol…">
        <TextInput value={symbols} onChange={setSymbols} mono />
      </Field>
      <div className="col gap-1" role="group" aria-label="Temporalidades">
        <span className="tiny muted">Temporalidades</span>
        <div className="row gap-1">
          {TF_CHOICES.map((tf) => (
            <Button key={tf} size="sm" active={tfs.includes(tf)} onClick={() => toggle(tf)}>
              {tf}
            </Button>
          ))}
        </div>
      </div>
      <Field label="Velas por serie" hint="1000 a 5000 velas cerradas">
        <NumberInput value={bars} onChange={setBars} min={1000} max={5000} step={500} />
      </Field>
      <Button
        variant="primary"
        icon={<Play size={14} />}
        disabled={props.disabled || !valid}
        onClick={() => props.onRun({ symbols: splitSymbols(symbols), tfs, bars: bars ?? undefined })}
      >
        Validar
      </Button>
    </div>
  )
}

export default function ValidationCard() {
  const [status, setStatus] = useState<ValidationStatus | null>(null)
  const [report, setReport] = useState<ValidationReport | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [open, setOpen] = useState<string | null>(null)
  const wasRunning = useRef(false)
  const shownReport = useRef<ValidationReport | null>(null)

  const loadStatus = useCallback(async () => {
    const st = await request<ValidationStatus>('GET', '/api/validation/status')
    setStatus(st)
    return st
  }, [])
  const loadReport = useCallback(async () => {
    const res = await request<ValidationLatestResponse>('GET', '/api/validation/latest')
    shownReport.current = res?.report ?? null
    setReport(shownReport.current)
  }, [])
  const loadAll = useCallback(async () => {
    try {
      await Promise.all([loadStatus(), loadReport()])
      setError(null)
    } catch (e) {
      setError(e)
    } finally {
      setLoaded(true)
    }
  }, [loadStatus, loadReport])

  useEffect(() => {
    void loadAll()
  }, [loadAll])

  // A job started elsewhere (terminal, another tab) or one that just ended: its events (source 'validation') refresh
  // the status, which starts the polling below or reads the new report. A job that ended before this card ever saw it
  // running (a quick one from the terminal) reads its report here.
  useEffect(() => {
    let timer: number | null = null
    const off = socket.on('event', ({ event }) => {
      if (event?.source !== 'validation') return
      if (timer !== null) window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        timer = null
        loadStatus()
          .then((st) => (finishedUnseen(st, shownReport.current, wasRunning.current) ? loadReport() : undefined))
          .catch(() => undefined)
      }, 500)
    })
    return () => {
      off()
      if (timer !== null) window.clearTimeout(timer)
    }
  }, [loadStatus, loadReport])

  const running = !!status?.running
  // Poll the job while it runs; when it ends, read the new report once.
  useEffect(() => {
    if (!running) {
      if (wasRunning.current) {
        wasRunning.current = false
        loadReport().catch((e) => setError(e))
      }
      return
    }
    wasRunning.current = true
    const id = window.setInterval(() => {
      loadStatus().catch(() => undefined)
    }, POLL_MS)
    return () => window.clearInterval(id)
  }, [running, loadStatus, loadReport])

  const run = async (body: ValidationRunRequest) => {
    setStarting(true)
    setRunError(null)
    try {
      setStatus(await request<ValidationStatus>('POST', '/api/validation/run', { body }))
      setShowForm(false)
    } catch (e) {
      setRunError(`No se pudo iniciar la validación: ${errorMessage(e)}`)
      loadStatus().catch(() => undefined)
    } finally {
      setStarting(false)
    }
  }

  const job = status?.job ?? null
  const onToggle = (key: string) => setOpen((cur) => (cur === key ? null : key))
  return (
    <Card
      className="val-card"
      title={
        <>
          <FlaskConical size={15} className="info" /> Validación walk-forward: ¿hay ventaja real?
        </>
      }
      sub="NEXUS + filtro frente a NEXUS sin filtro, entradas al azar y comprar y mantener, con las mismas reglas de riesgo y costes"
      actions={
        <>
          <Button size="sm" icon={<Settings2 size={14} />} active={showForm} disabled={running} onClick={() => setShowForm((v) => !v)}>
            Opciones
          </Button>
          <Button size="sm" variant="primary" icon={<Play size={14} />} loading={starting} disabled={running} onClick={() => void run({})}>
            {running ? 'Validando…' : 'Validar ahora'}
          </Button>
        </>
      }
    >
      <div className="col gap-3">
        {showForm && status && !running && <RunForm defaults={status.defaults} disabled={starting} onRun={(b) => void run(b)} />}
        {runError && <ErrorBox error={runError} />}
        {running && job && <ValidationProgress job={job} />}
        {!running && job?.state === 'error' && <ErrorBox error={`La última validación falló: ${job.error ?? job.message}`} />}
        {!loaded ? (
          <Loading label="Cargando la última validación…" />
        ) : report ? (
          <>
            {error !== null && <ErrorBox error={`No se pudo actualizar: ${errorMessage(error)}`} onRetry={() => void loadAll()} />}
            <ValidationReportView report={report} open={open} onToggle={onToggle} />
          </>
        ) : error !== null ? (
          <ErrorBox error={error} onRetry={() => void loadAll()} />
        ) : (
          <Empty icon={<FlaskConical size={22} />} title="Aún no hay ninguna validación">
            Ejecuta una para medir, con datos pasados y sin mirar el futuro, si NEXUS + filtro gana más que entradas al azar con las
            mismas reglas de riesgo.
            {status?.defaults
              ? ` Con los valores por defecto (${plural(arr(status.defaults.symbols).length * arr(status.defaults.tfs).length, 'serie', 'series')} de ${fmtInt(status.defaults.bars)} velas) puede tardar uno o dos minutos.`
              : ''}
          </Empty>
        )}
      </div>
    </Card>
  )
}
