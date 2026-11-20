// Simple mode «Calendario»: a month of NEXUS automática's closed trades, one cell per day (its result), and the day's
// log (bitácora): every trade with why it opened, how it closed and why it won or lost (GET /api/paper/autopilot/calendar).
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { AutopilotCalendar, CalendarTrade } from '../../api/types-autopilot'
import { fmtInt, fmtPrice } from '../../lib/format'
import { TONE_LABEL, type AccountTone } from '../paper/NexusAccounts'
import { Loading } from '../ui'
import './calendar.css'

const MONTHS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']
const WEEK = ['L', 'M', 'X', 'J', 'V', 'S', 'D']
const DAY_NAMES = ['domingo', 'lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado']

const money = (v: number, sign = false) => {
  const [int, dec] = Math.abs(v).toFixed(2).split('.')
  const s = `${int.replace(/\B(?=(\d{3})+(?!\d))/g, '.')},${dec}`
  return `${sign ? (v > 0 ? '+' : v < 0 ? '−' : '') : v < 0 ? '−' : ''}${s} $`
}
const short$ = (v: number) => {
  const a = Math.abs(v)
  const s = a >= 1000 ? `${(a / 1000).toLocaleString('es-ES', { maximumFractionDigits: 1 })}k` : a.toLocaleString('es-ES', { maximumFractionDigits: a >= 100 ? 0 : 1 })
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${s}`
}
const tone = (v: number) => (Math.abs(v) < 0.005 ? '' : v > 0 ? 'pos' : 'neg')
const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
const hhmm = (ts: number) => new Date(ts * 1000).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
const shortSym = (s: string) => s.replace(/^[A-Z_]+:/, '').replace(/\.P$/, '')
const held = (s: number | null) => {
  if (s === null) return null
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`
  if (s < 86400) return `${(s / 3600).toLocaleString('es-ES', { maximumFractionDigits: 1 })} h`
  return `${(s / 86400).toLocaleString('es-ES', { maximumFractionDigits: 1 })} días`
}
const rTxt = (r: number | null) => (r === null ? '' : `${r > 0 ? '+' : r < 0 ? '−' : ''}${Math.abs(r).toLocaleString('es-ES', { maximumFractionDigits: 2 })}R`)

interface Day {
  key: string
  date: Date
  trades: CalendarTrade[]
  pnl: number
  wins: number
}

export default function CalendarView({ toneOfAccount }: { toneOfAccount: (accountId: number) => AccountTone }) {
  const now = new Date()
  const [month, setMonth] = useState(() => new Date(now.getFullYear(), now.getMonth(), 1))
  const [data, setData] = useState<AutopilotCalendar | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [account, setAccount] = useState<'all' | AccountTone>('all')

  const load = useCallback(async (m: Date) => {
    setLoading(true)
    setError(null)
    try {
      const start = Math.floor(m.getTime() / 1000)
      const end = Math.floor(new Date(m.getFullYear(), m.getMonth() + 1, 1).getTime() / 1000)
      setData(await api.paper.autopilot.calendar(start, end))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load(month)
  }, [month, load])

  const trades = useMemo(
    () => (data?.trades ?? []).filter((t) => account === 'all' || toneOfAccount(t.account_id) === account),
    [data, account, toneOfAccount],
  )
  const accountsHere = useMemo(() => {
    const seen = new Set<AccountTone>()
    for (const t of data?.trades ?? []) seen.add(toneOfAccount(t.account_id))
    return (['10k', '1k', '500', '100', 'other'] as AccountTone[]).filter((x) => seen.has(x))
  }, [data, toneOfAccount])

  const days = useMemo(() => {
    const map = new Map<string, Day>()
    for (const t of trades) {
      const d = new Date(t.exit_time * 1000)
      const k = dayKey(d)
      const day = map.get(k) ?? { key: k, date: new Date(d.getFullYear(), d.getMonth(), d.getDate()), trades: [], pnl: 0, wins: 0 }
      day.trades.push(t)
      day.pnl += t.pnl
      if (t.pnl > 0) day.wins += 1
      map.set(k, day)
    }
    for (const d of map.values()) d.trades.sort((a, b) => a.exit_time - b.exit_time)
    return map
  }, [trades])

  // the selected day: today when it has trades, else the latest day with trades
  useEffect(() => {
    if (selected && days.has(selected)) return
    const today = dayKey(new Date())
    if (days.has(today)) setSelected(today)
    else {
      const last = [...days.values()].sort((a, b) => b.date.getTime() - a.date.getTime())[0]
      setSelected(last ? last.key : null)
    }
  }, [days, selected])

  const maxAbs = useMemo(() => Math.max(1, ...[...days.values()].map((d) => Math.abs(d.pnl))), [days])
  const monthPnl = trades.reduce((s, t) => s + t.pnl, 0)
  const monthWins = trades.filter((t) => t.pnl > 0).length
  const greenDays = [...days.values()].filter((d) => d.pnl > 0).length

  // the month grid (weeks start on Monday)
  const cells: (Date | null)[] = []
  const lead = (month.getDay() + 6) % 7
  for (let i = 0; i < lead; i++) cells.push(null)
  const nDays = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate()
  for (let d = 1; d <= nDays; d++) cells.push(new Date(month.getFullYear(), month.getMonth(), d))
  while (cells.length % 7) cells.push(null)

  const day = selected ? days.get(selected) ?? null : null
  const isCurrent = month.getFullYear() === now.getFullYear() && month.getMonth() === now.getMonth()

  return (
    <>
      <div className="cal-head">
        <button type="button" className="cal-nav" aria-label="Mes anterior" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}>
          <ChevronLeft size={18} />
        </button>
        <h2 className="cal-title">
          {MONTHS[month.getMonth()]} {month.getFullYear()}
        </h2>
        <button type="button" className="cal-nav" aria-label="Mes siguiente" disabled={isCurrent} onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}>
          <ChevronRight size={18} />
        </button>
      </div>

      {accountsHere.length > 1 && (
        <div className="sm-filter" role="group" aria-label="Filtrar por cuenta">
          {(['all', ...accountsHere] as Array<'all' | AccountTone>).map((f) => (
            <button key={f} type="button" className={`sm-filter-btn ${f !== 'all' ? `t-${f}` : ''} ${account === f ? 'active' : ''}`} onClick={() => setAccount(f)}>
              {f === 'all' ? 'Todas' : TONE_LABEL[f].replace('Cuenta ', '')}
            </button>
          ))}
        </div>
      )}

      <div className="cal-summary">
        <div>
          <span>Resultado del mes</span>
          <b className={`mono ${tone(monthPnl)}`}>{money(monthPnl, true)}</b>
        </div>
        <div>
          <span>Ganadoras</span>
          <b className="mono">
            {fmtInt(monthWins)} de {fmtInt(trades.length)}
          </b>
        </div>
        <div>
          <span>Días en verde</span>
          <b className="mono">
            {fmtInt(greenDays)} de {fmtInt(days.size)}
          </b>
        </div>
      </div>

      {loading && !data && <Loading label="Cargando el calendario…" />}
      {error && <div className="errorbox">No se pudo cargar el calendario: {error}</div>}

      <div className="cal-grid" role="grid" aria-label={`Resultado por día de ${MONTHS[month.getMonth()]}`}>
        {WEEK.map((w) => (
          <span key={w} className="cal-wd" aria-hidden>
            {w}
          </span>
        ))}
        {cells.map((d, i) => {
          if (!d) return <span key={`x${i}`} className="cal-cell cal-empty" />
          const info = days.get(dayKey(d))
          const k = dayKey(d)
          const strength = info ? 0.18 + 0.62 * Math.min(1, Math.abs(info.pnl) / maxAbs) : 0
          const isToday = k === dayKey(new Date())
          return (
            <button
              key={k}
              type="button"
              className={`cal-cell ${info ? (info.pnl > 0 ? 'win' : info.pnl < 0 ? 'loss' : 'flat') : 'none'} ${selected === k ? 'sel' : ''} ${isToday ? 'today' : ''}`}
              style={info ? ({ '--s': strength } as React.CSSProperties) : undefined}
              disabled={!info}
              onClick={() => setSelected(k)}
              aria-label={info ? `${d.getDate()}: ${money(info.pnl, true)}, ${info.trades.length} operaciones` : `${d.getDate()}: sin operaciones`}
            >
              <span className="cal-dn">{d.getDate()}</span>
              {info && (
                <>
                  <span className="cal-pnl mono">{short$(info.pnl)}</span>
                  <span className="cal-n">{info.trades.length}</span>
                </>
              )}
            </button>
          )
        })}
      </div>

      {day ? <DayLog day={day} toneOfAccount={toneOfAccount} /> : !loading && <div className="sm-empty">Sin operaciones cerradas este mes.</div>}
      {data && <p className="sm-hint">{data.note}</p>}
    </>
  )
}

function DayLog({ day, toneOfAccount }: { day: Day; toneOfAccount: (id: number) => AccountTone }) {
  const losses = day.trades.filter((t) => t.pnl < 0)
  const causes = new Map<string, { label: string; n: number; pnl: number }>()
  for (const t of losses) {
    const c = causes.get(t.cause) ?? { label: t.cause_label, n: 0, pnl: 0 }
    c.n += 1
    c.pnl += t.pnl
    causes.set(t.cause, c)
  }
  const byAcc = new Map<number, { name: string; pnl: number; n: number }>()
  for (const t of day.trades) {
    const a = byAcc.get(t.account_id) ?? { name: t.account, pnl: 0, n: 0 }
    a.pnl += t.pnl
    a.n += 1
    byAcc.set(t.account_id, a)
  }
  const d = day.date
  return (
    <section className="cal-day">
      <h2 className="sm-h">
        {DAY_NAMES[d.getDay()]} {d.getDate()} de {MONTHS[d.getMonth()]}
        <b className={`mono cal-day-pnl ${tone(day.pnl)}`}>{money(day.pnl, true)}</b>
      </h2>
      <p className="cal-day-sum">
        {fmtInt(day.trades.length)} cerradas · {fmtInt(day.wins)} ganaron · {fmtInt(losses.length)} perdieron
      </p>
      <div className="cal-accs">
        {[...byAcc.entries()].map(([id, a]) => (
          <span key={id} className={`cal-acc t-${toneOfAccount(id)}`}>
            <i aria-hidden /> {TONE_LABEL[toneOfAccount(id)] === 'Reto' ? a.name : TONE_LABEL[toneOfAccount(id)].replace('Cuenta ', '')}{' '}
            <b className={`mono ${tone(a.pnl)}`}>{money(a.pnl, true)}</b>
          </span>
        ))}
      </div>
      {causes.size > 0 && (
        <div className="cal-why">
          <span className="cal-why-t">¿Por qué perdió?</span>
          {[...causes.values()]
            .sort((a, b) => a.pnl - b.pnl)
            .map((c) => (
              <span key={c.label} className="cal-why-row">
                <span>
                  {c.label} <span className="muted">× {c.n}</span>
                </span>
                <b className="mono neg">{money(c.pnl, true)}</b>
              </span>
            ))}
        </div>
      )}
      <ol className="cal-log">
        {day.trades.map((t) => {
          const tn = toneOfAccount(t.account_id)
          return (
            <li key={t.id} className={`cal-entry t-${tn} ${t.result}`}>
              <div className="cal-entry-top">
                <span className="cal-time mono">{hhmm(t.exit_time)}</span>
                <span className={`sm-acct-chip t-${tn}`}>{tn === 'other' ? t.account : TONE_LABEL[tn].replace('Cuenta ', '')}</span>
                <b>{shortSym(t.symbol)}</b>
                <span className={`sm-side ${t.side === 'long' ? 'pos' : 'neg'}`}>{t.side === 'long' ? 'Compra' : 'Venta'}</span>
                <span className="cal-entry-pnl">
                  <b className={`mono ${tone(t.pnl)}`}>{money(t.pnl, true)}</b>
                  {t.r !== null && <span className={`mono cal-r ${tone(t.r)}`}>{rTxt(t.r)}</span>}
                </span>
              </div>
              <span className={`cal-cause ${t.result}`}>{t.cause_label}</span>
              <p className="cal-lesson">{t.lesson}</p>
              <dl className="cal-facts">
                <div>
                  <dt>Abrió</dt>
                  <dd>
                    {hhmm(t.entry_time)}
                    {t.entry_price !== null ? ` a ${fmtPrice(t.entry_price)}` : ''} · vela {t.tf}
                  </dd>
                </div>
                <div>
                  <dt>Cerró</dt>
                  <dd>
                    {hhmm(t.exit_time)}
                    {t.exit_price !== null ? ` a ${fmtPrice(t.exit_price)}` : ''}
                    {t.exit_label ? ` · ${t.exit_label}` : ''}
                    {held(t.held_s) ? ` · duró ${held(t.held_s)}` : ''}
                  </dd>
                </div>
                {t.why && (
                  <div className="wide">
                    <dt>Por qué entró</dt>
                    <dd>{t.why}</dd>
                  </div>
                )}
              </dl>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
