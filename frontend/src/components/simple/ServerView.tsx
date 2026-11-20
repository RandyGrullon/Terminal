// Simple mode, «Servidor» tab: how the machine that runs NEXUS is doing — where it runs and since when, every start
// with how long it was down before (a crash or a power cut), the internet as the server sees it, the battery when it
// has one, memory and database — and the whole event log, live, with the problems first.
import { useCallback, useEffect, useMemo, useState } from 'react'
import { BatteryCharging, BatteryLow, BatteryMedium, Cpu, Power, RotateCcw, Wifi, WifiOff } from 'lucide-react'
import { api } from '../../api/client'
import type { NexusEvent, SystemStatus } from '../../api/types'
import type { WsStatus } from '../../api/ws'
import { socket } from '../../api/ws'
import { fmtAgo } from '../../lib/format'

const POLL_MS = 15_000
const SHOW = 60

const SOURCE_LABEL: Record<string, string> = {
  system: 'Servidor',
  hub: 'Datos',
  recorder: 'Grabador',
  paper: 'Cuentas',
  broker: 'Bróker',
  scanner: 'Escáner',
  macro: 'Semáforo',
  nexus: 'NEXUS',
  evaluator: 'Filtro',
  alerts: 'Alertas',
  scripts: 'PyChart',
  ai: 'Copiloto',
  validation: 'Validación',
  research: 'Investigación',
  maintenance: 'Mantenimiento',
}

export function fmtSpan(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s} s`
  if (s < 3600) return `${Math.floor(s / 60)} min`
  if (s < 86400) return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')} min`
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`
}

const fmtWhen = (ms: number) => {
  const d = new Date(ms)
  const today = new Date()
  const time = d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return d.toDateString() === today.toDateString() ? time : `${d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit' })} ${time}`
}

const fmtBytes = (n: number | null) =>
  n === null ? '—' : n > 1e9 ? `${(n / 1e9).toFixed(2).replace('.', ',')} GB` : `${Math.round(n / 1e6)} MB`

/** The machine's state in one word for the badge of the tab: 'bad' (no server / no internet / errors in the last 30
 *  min), 'warn' (slow internet, low battery) or 'ok'. */
export function serverHealth(st: SystemStatus | null, wsStatus: WsStatus, events: NexusEvent[], now = Date.now()): 'ok' | 'warn' | 'bad' {
  if (wsStatus !== 'open' || !st) return 'bad'
  if (st.net.state === 'down') return 'bad'
  if (events.some((e) => e.level === 'error' && now - e.ts < 30 * 60_000)) return 'bad'
  if (st.net.state === 'slow' || (st.battery && st.battery.percent < 20 && !st.battery.charging)) return 'warn'
  return 'ok'
}

function NetBars({ st }: { st: SystemStatus }) {
  const samples = st.net.samples
  if (!samples.length) return null
  const max = Math.max(300, ...samples.map((s) => s.ms ?? 0))
  return (
    <div className="srv-bars" aria-label="Últimas comprobaciones de internet">
      {samples.map((s) => (
        <span
          key={s.ts}
          className={s.ok ? ((s.ms ?? 0) > 1500 ? 'slow' : 'ok') : 'down'}
          style={{ height: `${s.ok ? Math.max(12, Math.round(((s.ms ?? 0) / max) * 100)) : 100}%` }}
          title={`${fmtWhen(s.ts)} · ${s.ok ? `${s.ms} ms` : (s.error ?? 'falló')}`}
        />
      ))}
    </div>
  )
}

export default function ServerView({ wsStatus, onHealth }: { wsStatus: WsStatus; onHealth?: (h: 'ok' | 'warn' | 'bad') => void }) {
  const [st, setSt] = useState<SystemStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [events, setEvents] = useState<NexusEvent[]>([])
  const [filter, setFilter] = useState<'problems' | 'all'>('problems')
  const [show, setShow] = useState(SHOW)
  const [checkedAt, setCheckedAt] = useState<number | null>(null)

  const load = useCallback(async () => {
    try {
      const [s, ev] = await Promise.all([api.systemStatus(), api.events({ limit: 400 })])
      setSt(s)
      setEvents(ev.events.slice().sort((a, b) => b.ts - a.ts))
      setErr(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setCheckedAt(Date.now())
    }
  }, [])

  useEffect(() => {
    void load()
    const id = window.setInterval(() => document.visibilityState === 'visible' && void load(), POLL_MS)
    const off = socket.on('event', ({ event }) => setEvents((prev) => [event as NexusEvent, ...prev].slice(0, 600)))
    return () => {
      window.clearInterval(id)
      off()
    }
  }, [load])

  const health = serverHealth(err ? null : st, wsStatus, events)
  useEffect(() => onHealth?.(health), [health, onHealth])

  const shown = useMemo(
    () => (filter === 'problems' ? events.filter((e) => e.level === 'warn' || e.level === 'error') : events),
    [events, filter],
  )
  const online = wsStatus === 'open' && !err && st !== null
  const b = st?.battery ?? null
  const BatteryIcon = !b ? BatteryMedium : b.charging ? BatteryCharging : b.percent < 20 ? BatteryLow : BatteryMedium

  return (
    <>
      <section className={`srv-hero ${online ? 'ok' : 'bad'}`}>
        <div className="srv-hero-head">
          <span className="srv-dot" aria-hidden />
          <b>{online ? 'Funcionando' : 'Sin conexión con el servidor'}</b>
        </div>
        {st && (
          <>
            <div className="srv-where">{st.host.label}</div>
            <div className="srv-uptime">
              Encendido hace <b>{fmtSpan(st.uptime_s)}</b> · desde {fmtWhen(st.started_at)}
            </div>
          </>
        )}
        {!online && (
          <p className="sm-hint">
            {err
              ? `No responde: ${err}. Si NEXUS corre en el teléfono, revisa que tenga batería, Wi-Fi y que Termux siga abierto.`
              : 'Reconectando… Si no vuelve, el servidor puede estar apagado o sin internet.'}
          </p>
        )}
        {checkedAt && <span className="tiny muted">Comprobado {fmtAgo(checkedAt)}</span>}
      </section>

      {st && (
        <div className="srv-grid">
          <section className={`srv-card net-${st.net.state}`}>
            <span className="srv-card-head">
              {st.net.state === 'down' ? <WifiOff size={16} /> : <Wifi size={16} />} Internet
            </span>
            <b className="srv-big">{st.net.state === 'ok' ? 'Bien' : st.net.state === 'slow' ? 'Lento' : st.net.state === 'down' ? 'Sin conexión' : st.net.state === 'off' ? 'Apagado' : '…'}</b>
            <span className="srv-sub">{st.net.text}</span>
            <NetBars st={st} />
            <span className="srv-sub">
              Última hora: {st.net.checks_1h - st.net.fails_1h} de {st.net.checks_1h} bien
              {st.net.avg_ms !== null ? ` · media ${st.net.avg_ms} ms` : ''}
            </span>
          </section>

          <section className={`srv-card ${b && b.percent < 20 && !b.charging ? 'net-down' : ''}`}>
            <span className="srv-card-head">
              <BatteryIcon size={16} /> Batería
            </span>
            {b ? (
              <>
                <b className="srv-big">{Math.round(b.percent)} %</b>
                <span className="srv-sub">
                  {b.charging ? 'Cargando' : b.plugged ? 'Enchufado' : 'Sin cargador'}
                  {b.temperature_c !== null ? ` · ${b.temperature_c.toFixed(1).replace('.', ',')} °C` : ''}
                </span>
                <div className="srv-battery">
                  <span style={{ width: `${Math.max(2, Math.min(100, b.percent))}%` }} />
                </div>
                {b.temperature_c !== null && b.temperature_c >= 45 && <span className="srv-sub neg">Está caliente: ponlo en un lugar ventilado.</span>}
              </>
            ) : (
              <span className="srv-sub">{st.battery_note ?? 'No disponible'}</span>
            )}
          </section>

          <section className="srv-card">
            <span className="srv-card-head">
              <Cpu size={16} /> Recursos
            </span>
            <span className="srv-sub">
              Memoria de NEXUS: <b>{st.memory_mb !== null ? `${Math.round(st.memory_mb)} MB` : '—'}</b>
            </span>
            <span className="srv-sub">
              Base de datos: <b>{fmtBytes(st.db_size_bytes)}</b>
            </span>
            <span className="srv-sub">Python {st.host.python}</span>
          </section>
        </div>
      )}

      {st && st.starts.length > 0 && (
        <>
          <h2 className="sm-h">Caídas y arranques</h2>
          <div className="srv-starts">
            {st.starts.map((s) => {
              const crash = s.clean === false
              return (
                <div key={s.at} className={`srv-start ${crash ? 'crash' : ''}`}>
                  <span className="srv-start-icon">{crash ? <Power size={15} /> : <RotateCcw size={15} />}</span>
                  <span className="srv-start-main">
                    <b>Arrancó {fmtWhen(s.at)}</b>
                    <span>
                      {s.down_ms === null
                        ? 'Primer arranque registrado'
                        : crash
                          ? `Estuvo caído ${fmtSpan(s.down_ms / 1000)} sin apagarse bien (apagón, cierre forzado o el sistema lo detuvo)`
                          : `Se había apagado bien; estuvo parado ${fmtSpan(s.down_ms / 1000)}`}
                    </span>
                    <span className="tiny muted">{s.host}</span>
                  </span>
                </div>
              )
            })}
          </div>
        </>
      )}

      <h2 className="sm-h">
        Registro <span className="sm-count">{shown.length}</span>
      </h2>
      <div className="sm-filter" role="group" aria-label="Filtrar el registro">
        <button type="button" className={`sm-filter-btn ${filter === 'problems' ? 'active' : ''}`} onClick={() => setFilter('problems')}>
          Problemas
        </button>
        <button type="button" className={`sm-filter-btn ${filter === 'all' ? 'active' : ''}`} onClick={() => setFilter('all')}>
          Todo
        </button>
      </div>
      <div className="srv-log">
        {shown.length === 0 && <div className="sm-empty">{filter === 'problems' ? 'Sin avisos ni errores recientes.' : 'Sin eventos todavía.'}</div>}
        {shown.slice(0, show).map((e) => (
          <div key={e.id} className={`srv-ev lv-${e.level}`}>
            <span className="srv-ev-time mono">{fmtWhen(e.ts)}</span>
            <span className="srv-ev-src">{SOURCE_LABEL[e.source] ?? e.source}</span>
            <span className="srv-ev-msg">{e.message}</span>
          </div>
        ))}
        {shown.length > show && (
          <button type="button" className="sm-cta" onClick={() => setShow((n) => n + SHOW)}>
            Ver más
          </button>
        )}
      </div>
    </>
  )
}
