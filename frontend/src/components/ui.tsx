// Shared UI primitives. Keep pages consistent by composing these (styles live in styles.css).
import { createContext, useContext, useEffect, useId, useRef, type CSSProperties, type ReactNode } from 'react'
import { CircleCheck, CircleX, Clock, TriangleAlert, X } from 'lucide-react'
import type { NexusAction, Verdict } from '../api/types'
import { ACTION_LABEL, VERDICT_LABEL, actionTone, verdictTone, type Tone } from '../lib/format'

type BtnVariant = 'default' | 'primary' | 'success' | 'danger' | 'ghost'

/** The control a <Field> labels. NumberInput, Select, TextInput and Toggle inside a Field take this id (unless they get
 *  their own `id`), so the label names the control for screen readers and clicking it focuses the control. */
const FieldContext = createContext<{ id: string; hintId?: string } | null>(null)

/** id / aria-describedby of a form control: its own id, else the id of the enclosing Field. */
function useControlIds(ownId: string | undefined): { id: string | undefined; describedBy: string | undefined } {
  const field = useContext(FieldContext)
  if (ownId !== undefined || !field) return { id: ownId, describedBy: undefined }
  return { id: field.id, describedBy: field.hintId }
}

/** For other controls placed in a <Field> (e.g. SymbolSearch): the id its label points at; undefined outside one. */
export function useFieldId(): string | undefined {
  return useContext(FieldContext)?.id
}

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
  id?: string
  'aria-label'?: string
  'aria-describedby'?: string
}) {
  const ids = useControlIds(props.id)
  const opts = (props.options as unknown[]).map((o) =>
    typeof o === 'string' ? { value: o as T, label: o } : (o as { value: T; label: string }),
  )
  return (
    <select
      className="select"
      id={ids.id}
      aria-label={props['aria-label']}
      aria-describedby={props['aria-describedby'] ?? ids.describedBy}
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
  id?: string
  'aria-label'?: string
  'aria-describedby'?: string
}) {
  const ids = useControlIds(props.id)
  return (
    <input
      className={`input ${props.mono ? 'mono' : ''}`}
      id={ids.id}
      aria-label={props['aria-label']}
      aria-describedby={props['aria-describedby'] ?? ids.describedBy}
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
  id?: string
  'aria-label'?: string
  'aria-describedby'?: string
}) {
  const ids = useControlIds(props.id)
  return (
    <input
      className="input mono"
      id={ids.id}
      aria-label={props['aria-label']}
      aria-describedby={props['aria-describedby'] ?? ids.describedBy}
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

/** Label + control. The label is associated with the NumberInput / Select / TextInput / Toggle / SymbolSearch inside
 *  (one control per Field); another control can take useFieldId(), or pass `htmlFor` and give it that id. */
export function Field(props: { label: ReactNode; children: ReactNode; hint?: ReactNode; style?: CSSProperties; htmlFor?: string }) {
  const autoId = useId()
  const id = props.htmlFor ?? autoId
  const hintId = props.hint ? `${autoId}-hint` : undefined
  return (
    <div className="field" style={props.style}>
      <label htmlFor={id}>{props.label}</label>
      <FieldContext.Provider value={{ id, hintId }}>{props.children}</FieldContext.Provider>
      {props.hint && (
        <span id={hintId} className="tiny muted">
          {props.hint}
        </span>
      )}
    </div>
  )
}

export function Toggle(props: {
  checked: boolean
  onChange: (v: boolean) => void
  label?: ReactNode
  disabled?: boolean
  id?: string
  'aria-label'?: string
  'aria-describedby'?: string
}) {
  const ids = useControlIds(props.id)
  return (
    <label className="toggle" style={props.disabled ? { opacity: 0.5, pointerEvents: 'none' } : undefined}>
      <input
        type="checkbox"
        id={ids.id}
        aria-label={props['aria-label']}
        aria-describedby={props['aria-describedby'] ?? ids.describedBy}
        checked={props.checked}
        disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.checked)}
      />
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

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), ' +
  'textarea:not([disabled]), iframe, [contenteditable]:not([contenteditable="false"]), [tabindex]:not([tabindex="-1"])'

/** Open dialogs, innermost last: only the top-most one reacts to Escape and keeps Tab inside. */
const openDialogs: HTMLElement[] = []

function focusableIn(root: HTMLElement | null): HTMLElement[] {
  if (!root) return []
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.tabIndex >= 0 && el.getClientRects().length > 0)
}

/** The element that has the keyboard focus (null when nothing has it, or outside a browser). */
function focusedElement(): HTMLElement | null {
  if (typeof document === 'undefined' || typeof HTMLElement === 'undefined') return null
  const el = document.activeElement
  return el instanceof HTMLElement && el !== document.body ? el : null
}

/** Keyboard focus of an open dialog: moves focus inside (first control of the content, else of the footer, else the
 *  dialog itself), keeps Tab / Shift+Tab inside, lets Escape close only the top-most dialog and gives focus back to
 *  the element that opened it (`openedFrom`: where the focus was when it opened; by default the current focus unless
 *  a control inside the dialog already took it, e.g. with autoFocus). Returns the cleanup. */
function trapDialog(dialog: HTMLElement, onEscape: () => void, openedFrom: HTMLElement | null = null): () => void {
  const current = focusedElement()
  const opener = openedFrom ?? (current && !dialog.contains(current) ? current : null)
  openDialogs.push(dialog)
  if (!dialog.contains(document.activeElement)) {
    const first = focusableIn(dialog.querySelector<HTMLElement>('.modal-body'))[0] ?? focusableIn(dialog.querySelector<HTMLElement>('.modal-footer'))[0]
    ;(first ?? dialog).focus()
  }
  const onKey = (e: KeyboardEvent) => {
    if (openDialogs[openDialogs.length - 1] !== dialog) return
    if (e.key === 'Escape') {
      if (e.defaultPrevented) return // already handled inside (e.g. a search box closing its list)
      e.preventDefault()
      onEscape()
    } else if (e.key === 'Tab') {
      const items = focusableIn(dialog)
      const active = document.activeElement
      if (!items.length) {
        e.preventDefault()
        dialog.focus()
      } else if (e.shiftKey && (active === items[0] || active === dialog || !dialog.contains(active))) {
        e.preventDefault()
        items[items.length - 1].focus()
      } else if (!e.shiftKey && (active === items[items.length - 1] || !dialog.contains(active))) {
        e.preventDefault()
        items[0].focus()
      }
    }
  }
  document.addEventListener('keydown', onKey)
  return () => {
    document.removeEventListener('keydown', onKey)
    const i = openDialogs.indexOf(dialog)
    if (i >= 0) openDialogs.splice(i, 1)
    // Back to the opener, unless the closing action already moved the focus somewhere else on the page.
    const active = document.activeElement
    if (opener?.isConnected && (!active || active === document.body || dialog.contains(active))) opener.focus()
  }
}

export function Modal(props: { open: boolean; onClose: () => void; title: ReactNode; children: ReactNode; footer?: ReactNode; width?: number }) {
  const titleId = useId()
  const dialogRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(props.onClose)
  onCloseRef.current = props.onClose
  // Where the focus was when the dialog opened, read while rendering: a control with autoFocus inside the dialog
  // takes the focus during the commit, before any effect runs, and must not be mistaken for the opener.
  const openerRef = useRef<HTMLElement | null>(null)
  const wasOpenRef = useRef(false)
  if (props.open && !wasOpenRef.current) openerRef.current = focusedElement()
  wasOpenRef.current = props.open
  useEffect(() => {
    if (!props.open || !dialogRef.current) return
    return trapDialog(dialogRef.current, () => onCloseRef.current(), openerRef.current)
  }, [props.open])
  if (!props.open) return null
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && props.onClose()}>
      <div
        ref={dialogRef}
        className="modal"
        style={{ outline: 'none', ...(props.width ? { width: `min(${props.width}px, 100%)` } : {}) }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="modal-header">
          <h3 id={titleId} className="grow">
            {props.title}
          </h3>
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
