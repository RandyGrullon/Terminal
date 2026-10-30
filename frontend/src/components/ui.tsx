// Shared UI primitives. Keep pages consistent by composing these (styles live in styles.css).
import { useEffect, type CSSProperties, type ReactNode } from 'react'
import { CircleCheck, CircleX, Clock, TriangleAlert, X } from 'lucide-react'
import type { NexusAction, Verdict } from '../api/types'
import { ACTION_LABEL, VERDICT_LABEL, actionTone, verdictTone, type Tone } from '../lib/format'

type BtnVariant = 'default' | 'primary' | 'success' | 'danger' | 'ghost'

export function Button(props: {
  children?: ReactNode
  onClick?: () => void
  variant?: BtnVariant
  size?: 'sm' | 'md' | 'lg'
  disabled?: boolean
  loading?: boolean
  title?: string
  icon?: ReactNode
  active?: boolean
  type?: 'button' | 'submit'
  className?: string
  style?: CSSProperties
}) {
  const { variant = 'default', size = 'md' } = props
  const cls = [
    'btn',
    variant !== 'default' ? `btn-${variant}` : '',
    size === 'sm' ? 'btn-sm' : size === 'lg' ? 'btn-lg' : '',
    props.active ? 'active' : '',
    props.className ?? '',
  ]
    .filter(Boolean)
    .join(' ')
  return (
    <button
      type={props.type ?? 'button'}
      className={cls}
      onClick={props.onClick}
      disabled={props.disabled || props.loading}
      title={props.title}
      style={props.style}
    >
      {props.loading ? <Spinner /> : props.icon}
      {props.children}
    </button>
  )
}

export function IconButton(props: {
  icon: ReactNode
  title: string
  onClick?: () => void
  size?: 'sm' | 'md'
  active?: boolean
  disabled?: boolean
  variant?: BtnVariant
}) {
  const cls = ['btn', 'btn-icon', `btn-${props.variant ?? 'ghost'}`, props.size === 'sm' ? 'btn-sm' : '', props.active ? 'active' : '']
    .filter(Boolean)
    .join(' ')
  return (
    <button type="button" className={cls} title={props.title} aria-label={props.title} onClick={props.onClick} disabled={props.disabled}>
      {props.icon}
    </button>
  )
}

export function Card(props: {
  title?: ReactNode
  sub?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  className?: string
  bodyClassName?: string
  flush?: boolean
  style?: CSSProperties
}) {
  return (
    <section className={`card ${props.className ?? ''}`} style={props.style}>
      {(props.title || props.actions) && (
        <header className="card-header">
          <div className="col gap-0" style={{ minWidth: 0 }}>
            {props.title && <div className="card-title">{props.title}</div>}
            {props.sub && <div className="card-sub">{props.sub}</div>}
          </div>
          {props.actions && <div className="card-actions">{props.actions}</div>}
        </header>
      )}
      <div className={`card-body ${props.flush ? 'flush' : ''} ${props.bodyClassName ?? ''}`}>{props.children}</div>
    </section>
  )
}

export function Badge(props: { tone?: Tone; children: ReactNode; title?: string; outline?: boolean }) {
  return (
    <span className={`badge badge-${props.tone ?? 'muted'} ${props.outline ? 'badge-outline' : ''}`} title={props.title}>
      {props.children}
    </span>
  )
}

export function Tabs<T extends string>(props: {
  tabs: { id: T; label: ReactNode }[]
  value: T
  onChange: (id: T) => void
  pills?: boolean
}) {
  return (
    <div className={`tabs ${props.pills ? 'pills' : ''}`} role="tablist">
      {props.tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={t.id === props.value}
          className={`tab ${t.id === props.value ? 'active' : ''}`}
          onClick={() => props.onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  )
}

export function Select<T extends string>(props: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: string }[] | readonly T[]
  title?: string
  style?: CSSProperties
  disabled?: boolean
}) {
  const opts = (props.options as unknown[]).map((o) =>
    typeof o === 'string' ? { value: o as T, label: o } : (o as { value: T; label: string }),
  )
  return (
    <select
      className="select"
      value={props.value}
      title={props.title}
      style={props.style}
      disabled={props.disabled}
      onChange={(e) => props.onChange(e.target.value as T)}
    >
      {opts.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

export function TextInput(props: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  onEnter?: () => void
  mono?: boolean
  style?: CSSProperties
  autoFocus?: boolean
  type?: string
}) {
  return (
    <input
      className={`input ${props.mono ? 'mono' : ''}`}
      type={props.type ?? 'text'}
      value={props.value}
      placeholder={props.placeholder}
      style={props.style}
      autoFocus={props.autoFocus}
      onChange={(e) => props.onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') props.onEnter?.()
      }}
    />
  )
}

export function NumberInput(props: {
  value: number | null
  onChange: (v: number | null) => void
  min?: number
  max?: number
  step?: number
  placeholder?: string
  style?: CSSProperties
}) {
  return (
    <input
      className="input mono"
      type="number"
      value={props.value ?? ''}
      min={props.min}
      max={props.max}
      step={props.step ?? 'any'}
      placeholder={props.placeholder}
      style={props.style}
      onChange={(e) => {
        const v = e.target.value
        props.onChange(v === '' ? null : Number(v))
      }}
    />
  )
}

export function Field(props: { label: ReactNode; children: ReactNode; hint?: ReactNode; style?: CSSProperties }) {
  return (
    <div className="field" style={props.style}>
      <label>{props.label}</label>
      {props.children}
      {props.hint && <span className="tiny muted">{props.hint}</span>}
    </div>
  )
}

export function Toggle(props: { checked: boolean; onChange: (v: boolean) => void; label?: ReactNode; disabled?: boolean }) {
  return (
    <label className="toggle" style={props.disabled ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
      <input type="checkbox" checked={props.checked} onChange={(e) => props.onChange(e.target.checked)} />
      <span className="track" />
      {props.label}
    </label>
  )
}

export function Stat(props: { label: ReactNode; value: ReactNode; tone?: Tone; hint?: ReactNode; title?: string }) {
  return (
    <div className="stat" title={props.title}>
      <span className="stat-label">{props.label}</span>
      <span className={`stat-value ${props.tone && props.tone !== 'muted' ? props.tone : ''}`}>{props.value}</span>
      {props.hint && <span className="stat-hint">{props.hint}</span>}
    </div>
  )
}

/** Horizontal bar. value in [0,1] (or [-1,1] with `centered`). */
export function Meter(props: { value: number; tone?: Tone; centered?: boolean; height?: number; title?: string }) {
  const v = Number.isFinite(props.value) ? props.value : 0
  const color = props.tone ? (props.tone === 'muted' ? 'var(--muted)' : `var(--${props.tone})`) : 'var(--accent)'
  const style: CSSProperties = props.centered
    ? v >= 0
      ? { left: '50%', width: `${Math.min(1, v) * 50}%`, background: color }
      : { left: `${50 + Math.max(-1, v) * 50}%`, width: `${Math.min(1, -v) * 50}%`, background: color }
    : { width: `${Math.max(0, Math.min(1, v)) * 100}%`, background: color }
  return (
    <div className="meter" style={{ height: props.height ?? 6 }} title={props.title}>
      <span style={style} />
    </div>
  )
}

export function Spinner(props: { large?: boolean }) {
  return <span className={`spinner ${props.large ? 'lg' : ''}`} />
}

export function Loading(props: { label?: string }) {
  return (
    <div className="empty">
      <Spinner large />
      <span>{props.label ?? 'Cargando…'}</span>
    </div>
  )
}

export function Empty(props: { icon?: ReactNode; title: ReactNode; children?: ReactNode }) {
  return (
    <div className="empty">
      {props.icon}
      <strong style={{ color: 'var(--text-2)' }}>{props.title}</strong>
      {props.children && <div className="small">{props.children}</div>}
    </div>
  )
}

export function ErrorBox(props: { error: unknown; onRetry?: () => void }) {
  if (!props.error) return null
  const msg = props.error instanceof Error ? props.error.message : String(props.error)
  return (
    <div className="errorbox">
      <TriangleAlert size={16} style={{ flex: 'none', marginTop: 1 }} />
      <div className="grow">{msg}</div>
      {props.onRetry && (
        <Button size="sm" onClick={props.onRetry}>
          Reintentar
        </Button>
      )}
    </div>
  )
}

export function Modal(props: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; width?: number }) {
  useEffect(() => {
    if (!props.open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') props.onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [props.open, props.onClose])
  if (!props.open) return null
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && props.onClose()}>
      <div className="modal" style={props.width ? { width: `min(${props.width}px, 100%)` } : undefined} role="dialog" aria-modal="true">
        <div className="modal-header">
          <h3 className="grow">{props.title}</h3>
          <IconButton icon={<X size={16} />} title="Cerrar" onClick={props.onClose} />
        </div>
        <div className="modal-body">{props.children}</div>
        {props.footer && <div className="modal-footer">{props.footer}</div>}
      </div>
    </div>
  )
}

/** TOMAR / ESPERAR / NO TOMAR chip with optional score (0-100). */
export function VerdictBadge(props: { verdict: Verdict | null | undefined; score?: number | null; large?: boolean; label?: string }) {
  if (!props.verdict) return <span className="verdict muted">—</span>
  const tone = verdictTone(props.verdict)
  const Icon = props.verdict === 'TAKE' ? CircleCheck : props.verdict === 'AVOID' ? CircleX : Clock
  return (
    <span className={`verdict ${tone} ${props.large ? 'lg' : ''}`} style={{ color: `var(--${tone})` }}>
      <Icon size={props.large ? 20 : 15} />
      {props.label ?? VERDICT_LABEL[props.verdict]}
      {props.score !== undefined && props.score !== null && <span className="mono" style={{ opacity: 0.85 }}>{Math.round(props.score)}/100</span>}
    </span>
  )
}

/** COMPRAR / VENDER / MANTENER … chip. */
export function ActionBadge(props: { action: NexusAction | string | null | undefined; label?: string; large?: boolean }) {
  if (!props.action) return <span className="verdict muted">—</span>
  const tone = actionTone(props.action)
  const label = props.label ?? ACTION_LABEL[props.action as NexusAction] ?? props.action
  return (
    <span className={`verdict ${tone} ${props.large ? 'lg' : ''}`} style={{ color: tone === 'muted' ? 'var(--text-2)' : `var(--${tone})` }}>
      {label}
    </span>
  )
}

export function Disclaimer() {
  return (
    <p className="disclaimer">
      Herramienta educativa y de investigación. Las señales y probabilidades se calculan con datos históricos y no garantizan
      resultados futuros. No es asesoría financiera. Toda operación en NEXUS es simulada (papel).
    </p>
  )
}
