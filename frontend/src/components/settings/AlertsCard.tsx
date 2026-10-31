// Alerts outside the app (settings page, below the "Alertas" group): state of each channel, "Enviar prueba" with the
// per-channel result or the Spanish error, the latest alerts and what happened to them, and this browser's desktop
// notifications (Notification API, opt-in; the permission is requested only from the toggle's click).
import { memo, useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import {
  BellRing,
  CircleCheck,
  CircleX,
  CirclePause,
  Mail,
  MonitorSmartphone,
  RefreshCw,
  Send,
  TriangleAlert,
  Undo2,
} from 'lucide-react'
import { errorMessage, request } from '../../api/client'
import type { AlertChannelResult, AlertChannelStatus, AlertRecord, AlertsStatus, AlertsTestResponse } from '../../api/types'
import { fmtAgo, fmtDateTime, fmtInt, type Tone } from '../../lib/format'
import { useApp } from '../../store'
import { notifyDesktop, useDesktopPref, type DesktopPermission } from '../macro/sound'
import { Badge, Button, Card, ErrorBox, IconButton, Loading, Toggle } from '../ui'
import type { FieldDef } from './schema'

const POLL_MS = 30_000

const STATUS_META: Record<string, { label: string; tone: Tone }> = {
  sent: { label: 'Enviado', tone: 'pos' },
  partial: { label: 'Enviado en parte', tone: 'warn' },
  failed: { label: 'Falló', tone: 'neg' },
  quiet: { label: 'Silenciado', tone: 'muted' },
  rate_limited: { label: 'Por el límite', tone: 'warn' },
  no_channel: { label: 'Sin canal activo', tone: 'muted' },
}
const PRIORITY_META: Record<string, { label: string; tone: Tone }> = {
  critical: { label: 'Crítico', tone: 'neg' },
  warn: { label: 'Importante', tone: 'warn' },
  info: { label: 'Info', tone: 'info' },
}
const CATEGORY_LABEL: Record<string, string> = { macro: 'Semáforo', signals: 'Señal NEXUS', paper: 'Cuenta demo' }
const CHANNEL_LABEL: Record<string, string> = { telegram: 'Telegram', email: 'Correo' }

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const plural = (n: number, one: string, many: string) => `${fmtInt(n)} ${n === 1 ? one : many}`
/** "a", "a y b", "a, b y c". */
const joinEs = (items: string[]) => (items.length <= 1 ? (items[0] ?? '') : `${items.slice(0, -1).join(', ')} y ${items[items.length - 1]}`)

/** One line describing a channel: what it is waiting for, or where it sends. */
export function channelState(ch: AlertChannelStatus): { tone: Tone; text: string } {
  if (ch.problem) return { tone: 'neg', text: ch.problem }
  if (!ch.configured) {
    const missing = Array.isArray(ch.missing) && ch.missing.length ? `falta ${joinEs(ch.missing)}` : 'sin configurar'
    return ch.enabled
      ? { tone: 'warn', text: `Activado, pero incompleto: ${missing}` }
      : { tone: 'muted', text: `Desactivado · ${missing}` }
  }
  if (!ch.enabled) return { tone: 'muted', text: `Configurado pero desactivado${ch.target ? ` · ${ch.target}` : ''}` }
  return { tone: 'pos', text: `Activo${ch.target ? ` · ${ch.target}` : ''}` }
}

function ChannelIcon({ id }: { id: string }) {
  return id === 'email' ? <Mail size={14} /> : <Send size={14} />
}

function StateIcon({ tone }: { tone: Tone }) {
  if (tone === 'pos') return <CircleCheck size={14} className="pos" />
  if (tone === 'neg') return <CircleX size={14} className="neg" />
  return <CirclePause size={14} className={tone === 'warn' ? 'warn' : 'muted'} />
}

function ResultLine({ r }: { r: AlertChannelResult }) {
  let icon: ReactNode
  let text: string
  if (r.skipped) {
    icon = <CirclePause size={14} className="muted" />
    text = `no se probó · ${r.error ?? 'sin configurar'}`
  } else if (r.ok) {
    icon = <CircleCheck size={14} className="pos" />
    const secs = finite(r.duration_ms) ? ` en ${(r.duration_ms / 1000).toLocaleString('es-ES', { maximumFractionDigits: 1 })} s` : ''
    text = `mensaje de prueba enviado${secs}${r.enabled === false ? ' (canal desactivado: actívalo para recibir los avisos)' : ''}`
  } else {
    icon = <CircleX size={14} className="neg" />
    text = r.error ?? 'error desconocido'
  }
  return (
    <div className="set-service">
      {icon}
      <span className="small set-wrap">
        <strong>{r.label}</strong>: {text}
      </span>
    </div>
  )
}

function RecentRow({ a }: { a: AlertRecord }) {
  const st = STATUS_META[a.status] ?? { label: a.status, tone: 'muted' as Tone }
  const pr = PRIORITY_META[a.priority] ?? { label: a.priority, tone: 'muted' as Tone }
  const errors = (a.results ?? []).filter((r) => !r.ok && r.error).map((r) => `${CHANNEL_LABEL[r.channel] ?? r.channel}: ${r.error}`)
  return (
    <div className="col gap-0" style={{ minWidth: 0 }} title={[a.text, ...errors].join('\n')}>
      <div className="row gap-1" style={{ flexWrap: 'wrap' }}>
        <span className="tiny muted">{fmtAgo(a.at)}</span>
        <Badge tone={st.tone}>{st.label}</Badge>
        <Badge tone={pr.tone} outline>
          {pr.label}
        </Badge>
        <span className="tiny muted">{CATEGORY_LABEL[a.category] ?? a.category}</span>
      </div>
      <span className="small ellipsis">{a.text}</span>
      {errors.length > 0 && <span className="tiny neg set-wrap">{errors[0]}</span>}
    </div>
  )
}

const PERMISSION_TEXT: Record<DesktopPermission, string> = {
  unsupported: 'Este navegador no admite notificaciones de escritorio.',
  denied: 'El navegador las tiene bloqueadas para este sitio: permítelas en la configuración del sitio y vuelve a activarlas.',
  default: 'Al activarlas, el navegador te pedirá permiso.',
  granted: '',
}

function DesktopNotifications() {
  const notify = useApp((s) => s.notify)
  const { wanted, permission, enable, disable } = useDesktopPref()
  const [asking, setAsking] = useState(false)
  const on = wanted && permission === 'granted'
  const toggle = async (next: boolean) => {
    if (!next) {
      disable()
      return
    }
    setAsking(true)
    try {
      const p = await enable() // the permission prompt comes from this click
      if (p === 'granted') notify('Notificaciones de escritorio activadas en este navegador', 'success')
      else if (p === 'denied') notify('El navegador bloqueó las notificaciones de este sitio', 'warn')
    } finally {
      setAsking(false)
    }
  }
  const test = () => {
    const shown = notifyDesktop(
      { title: 'NEXUS · Prueba', body: 'Así verás el semáforo en rojo y las señales NEXUS con la pestaña en segundo plano.', tag: `nexus-test-${Date.now()}` },
      { force: true },
    )
    if (!shown) notify('El navegador no mostró la notificación (revisa el permiso y el modo «No molestar»)', 'warn')
  }
  return (
    <div className="col gap-1">
      <div className="row gap-2" style={{ flexWrap: 'wrap' }}>
        <MonitorSmartphone size={14} className="accent" />
        <span className="small grow">
          <strong>Notificaciones de este navegador</strong>
        </span>
        <Toggle
          checked={on}
          disabled={permission === 'unsupported' || asking}
          onChange={(v) => void toggle(v)}
          label={on ? 'Activadas' : 'Desactivadas'}
          aria-label="Notificaciones de escritorio en este navegador"
        />
      </div>
      <span className="tiny muted set-wrap">
        Semáforo en rojo y señales NEXUS mientras la pestaña está en segundo plano. Solo funcionan con NEXUS abierto en
        alguna pestaña; con la pestaña cerrada usa Telegram o el correo (necesitan el servidor de NEXUS en marcha).{' '}
        {PERMISSION_TEXT[permission]}
      </span>
      {on && (
        <Button size="sm" variant="ghost" onClick={test} title="Muestra ahora una notificación de prueba">
          Probar notificación
        </Button>
      )}
    </div>
  )
}

interface Props {
  /** Unsaved changes in the "Alertas" group: the test uses the saved settings. */
  dirtyCount: number
  /** Bumped by the page after every successful save (re-read the channels). */
  savedTick: number
}

function AlertsCard({ dirtyCount, savedTick }: Props) {
  const notify = useApp((s) => s.notify)
  const [status, setStatus] = useState<AlertsStatus | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<AlertsTestResponse | null>(null)
  const alive = useRef(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const st = await request<AlertsStatus>('GET', '/api/alerts/status')
      if (!alive.current) return
      setStatus(st && typeof st === 'object' ? st : null)
      setError(null)
    } catch (e) {
      if (alive.current) setError(e)
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    alive.current = true
    void load()
    const timer = window.setInterval(() => void load(), POLL_MS)
    return () => {
      alive.current = false
      window.clearInterval(timer)
    }
  }, [load])

  useEffect(() => {
    if (savedTick > 0) void load()
  }, [savedTick, load])

  const channels = Array.isArray(status?.channels) ? status.channels : []
  const configured = channels.some((c) => c.configured)

  const sendTest = async () => {
    setTesting(true)
    try {
      const r = await request<AlertsTestResponse>('POST', '/api/alerts/test', { body: {} })
      setResult(r)
      notify(r.ok ? `Prueba enviada: ${r.message}` : `Prueba no completada: ${r.message}`, r.ok ? 'success' : 'warn')
    } catch (e) {
      notify(`No se pudo enviar la prueba: ${errorMessage(e)}`, 'error')
    } finally {
      setTesting(false)
      void load()
    }
  }

  const shownTest = result ?? status?.last_test ?? null
  const shownAt = result ? null : status?.last_test?.at
  const c = status?.counters
  const recent = Array.isArray(status?.recent) ? status.recent.slice(0, 6) : []
  const warnings = Array.isArray(status?.warnings) ? status.warnings : []

  return (
    <Card
      title={
        <>
          <BellRing size={14} /> Prueba y estado de las alertas
        </>
      }
      sub="Telegram y correo reciben avisos aunque la pestaña esté cerrada, mientras el servidor de NEXUS siga en marcha; nada se envía a un canal desactivado."
      actions={<IconButton size="sm" icon={<RefreshCw size={13} />} title="Actualizar" onClick={() => void load()} disabled={loading} />}
    >
      {error !== null && !status ? (
        <ErrorBox error={error} onRetry={() => void load()} />
      ) : !status ? (
        <Loading label="Consultando las alertas…" />
      ) : (
        <div className="col gap-2">
          {warnings.map((w, i) => (
            <div key={i} className="notice">
              <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
              <span className="set-wrap">{w}</span>
            </div>
          ))}
          <div className="set-services">
            {channels.map((ch) => {
              const st = channelState(ch)
              const last = ch.last
              return (
                <div key={ch.id} className="set-service">
                  <StateIcon tone={st.tone} />
                  <div className="col gap-0 grow" style={{ minWidth: 0 }}>
                    <span className="small row gap-1">
                      <ChannelIcon id={String(ch.id)} /> {ch.label}
                    </span>
                    <span className={`tiny set-wrap ${st.tone === 'neg' ? 'neg' : st.tone === 'warn' ? 'warn' : 'muted'}`}>{st.text}</span>
                    {last && (
                      <span className={`tiny set-wrap ${last.ok ? 'muted' : 'neg'}`}>
                        {last.ok ? `Último aviso entregado ${fmtAgo(last.at)}` : `Último aviso falló ${fmtAgo(last.at)}: ${last.error}`}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
          {dirtyCount > 0 && (
            <span className="tiny warn set-wrap">
              Tienes {dirtyCount === 1 ? '1 cambio' : `${dirtyCount} cambios`} sin guardar en Alertas: la prueba usa los ajustes guardados.
            </span>
          )}
          <Button
            variant="primary"
            icon={<Send size={14} />}
            loading={testing}
            disabled={!configured || dirtyCount > 0}
            onClick={() => void sendTest()}
            title={
              dirtyCount > 0
                ? 'Guarda los cambios antes de probar'
                : configured
                  ? 'Envía un mensaje de prueba a cada canal configurado'
                  : 'Completa los datos de Telegram o del correo y guarda'
            }
          >
            Enviar prueba
          </Button>
          {shownTest && (
            <div className="col gap-1">
              <span className="tiny muted">{shownAt ? `Última prueba: ${fmtDateTime(shownAt)}` : 'Resultado de la prueba'}</span>
              <div className="set-services">
                {(shownTest.results ?? []).map((r) => (
                  <ResultLine key={String(r.channel)} r={r} />
                ))}
              </div>
            </div>
          )}
          {c && (
            <span className="tiny muted set-wrap" title={`Desde ${fmtDateTime(status.since)}`}>
              Desde {fmtDateTime(status.since)}: {plural(c.sent, 'aviso enviado', 'avisos enviados')} ·{' '}
              {plural(c.failed, 'fallido', 'fallidos')} · {plural(c.quiet, 'silenciado', 'silenciados')} ·{' '}
              {fmtInt(c.rate_limited)} por el límite · {plural(c.filtered, 'filtrado', 'filtrados')} por tus ajustes. Mensajes en
              la última hora: {fmtInt(status.sent_last_hour)} (límite {fmtInt(status.max_per_hour)}; los críticos pueden
              superarlo).
            </span>
          )}
          {status.quiet?.enabled && (
            <span className="tiny muted">
              Horas de silencio de {status.quiet.start} a {status.quiet.end} ({status.quiet.timezone})
              {status.quiet.active ? ' · ahora en silencio' : ''}
            </span>
          )}
          {recent.length > 0 && (
            <>
              <div className="divider" />
              <span className="tiny muted">Últimos avisos</span>
              <div className="col gap-2">
                {recent.map((a, i) => (
                  <RecentRow key={`${a.at}-${i}`} a={a} />
                ))}
              </div>
            </>
          )}
        </div>
      )}
      <div className="divider" style={{ margin: '10px 0' }} />
      <DesktopNotifications />
    </Card>
  )
}

export default memo(AlertsCard)
