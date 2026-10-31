// One settings field: label + control (by kind) + help, with dirty marker, per-field revert and inline errors.
// The label is associated with its control (or names the group of controls) and the help / error describes it.
import { memo, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { ExternalLink, Eye, EyeOff, Undo2, X } from 'lucide-react'
import { TIMEFRAMES } from '../../api/types'
import { fmtNum } from '../../lib/format'
import { IconButton, Select, Toggle } from '../ui'
import { FEE_CLASS_LABEL, FEE_CLASSES, type FieldDef } from './schema'

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** 20 -> "20 s", 900 -> "15 min", 14400 -> "4 h", 90000 -> "1 d 1 h". */
function humanSeconds(total: number): string {
  const s = Math.max(0, Math.round(total))
  if (s < 60) return `${s} s`
  if (s < 3600) {
    const m = Math.floor(s / 60)
    const r = s % 60
    return r ? `${m} min ${r} s` : `${m} min`
  }
  if (s < 86400) {
    const h = Math.floor(s / 3600)
    const m = Math.round((s % 3600) / 60)
    return m ? `${h} h ${m} min` : `${h} h`
  }
  const d = Math.floor(s / 86400)
  const h = Math.round((s % 86400) / 3600)
  return h ? `${d} d ${h} h` : `${d} d`
}

/** id / description / validity of a control, so the label and the help or error text reach screen readers. */
interface A11y {
  id?: string
  describedBy?: string
  invalid?: boolean
}

/** Number input with a local text buffer so intermediate states ("0.", "-") can be typed. */
function NumField({
  value,
  onChange,
  def,
  small,
  a11y,
}: {
  value: unknown
  onChange: (v: number | null) => void
  def: FieldDef
  small?: boolean
  a11y?: A11y
}) {
  const num = finite(value) ? value : null
  const [text, setText] = useState(num === null ? '' : String(num))
  const focused = useRef(false)
  useEffect(() => {
    if (!focused.current) setText(num === null ? '' : String(num))
  }, [num])
  return (
    <input
      className={`input mono set-num ${small ? 'small' : ''}`}
      id={a11y?.id}
      aria-describedby={a11y?.describedBy}
      aria-invalid={a11y?.invalid || undefined}
      type="number"
      inputMode="decimal"
      value={text}
      min={def.min}
      max={def.max}
      step={def.step ?? 'any'}
      onFocus={() => {
        focused.current = true
      }}
      onBlur={() => {
        focused.current = false
        setText(num === null ? '' : String(num))
      }}
      onChange={(e) => {
        const raw = e.target.value
        setText(raw)
        if (raw.trim() === '') {
          onChange(null)
          return
        }
        const n = Number(raw)
        if (Number.isFinite(n)) onChange(n)
      }}
    />
  )
}

function FieldLink({ def }: { def: FieldDef }) {
  if (!def.link) return null
  return (
    <a className="tiny set-link" href={def.link.href} target="_blank" rel="noopener noreferrer">
      <ExternalLink size={11} /> {def.link.label}
    </a>
  )
}

/** Masked value with show / hide and clear: API keys and tokens ('secret', noun "clave") or passwords ('password'). */
function SecretField({
  value,
  onChange,
  def,
  a11y,
  noun = 'clave',
}: {
  value: unknown
  onChange: (v: string) => void
  def: FieldDef
  a11y: A11y
  noun?: 'clave' | 'contraseña'
}) {
  const [show, setShow] = useState(false)
  const v = typeof value === 'string' ? value : ''
  return (
    <div className="col gap-1">
      <div className="row gap-1">
        <input
          className="input mono grow"
          id={a11y.id}
          aria-describedby={a11y.describedBy}
          aria-invalid={a11y.invalid || undefined}
          type={show ? 'text' : 'password'}
          value={v}
          autoComplete="off"
          spellCheck={false}
          placeholder={def.placeholder ?? 'Sin configurar'}
          onChange={(e) => onChange(e.target.value)}
        />
        <IconButton
          size="sm"
          icon={show ? <EyeOff size={14} /> : <Eye size={14} />}
          title={show ? `Ocultar ${noun}` : `Mostrar ${noun}`}
          onClick={() => setShow((s) => !s)}
        />
        {v && <IconButton size="sm" icon={<X size={14} />} title={`Borrar ${noun}`} onClick={() => onChange('')} />}
      </div>
      <FieldLink def={def} />
    </div>
  )
}

/** Plain text: server names, addresses, chat ids… (nothing to hide). */
function TextField({ value, onChange, def, a11y }: { value: unknown; onChange: (v: string) => void; def: FieldDef; a11y: A11y }) {
  return (
    <div className="col gap-1">
      <input
        className="input mono"
        id={a11y.id}
        aria-describedby={a11y.describedBy}
        aria-invalid={a11y.invalid || undefined}
        type="text"
        inputMode={def.inputMode ?? 'text'}
        value={typeof value === 'string' ? value : ''}
        autoComplete="off"
        spellCheck={false}
        placeholder={def.placeholder ?? 'Sin configurar'}
        onChange={(e) => onChange(e.target.value)}
      />
      <FieldLink def={def} />
    </div>
  )
}

/** Several values of `def.options` as toggle chips. The list keeps the options' order (switching a chip off and on
 *  again gives back the saved value); a saved value that is not an option is still shown, so it can be removed. */
function MultiField({ value, onChange, def }: { value: unknown; onChange: (v: string[]) => void; def: FieldDef }) {
  const sel = Array.isArray(value) ? value.map(String) : []
  const opts = [...(def.options ?? [])]
  for (const v of sel) if (!opts.some((o) => o.value === v)) opts.push({ value: v, label: v })
  return (
    <div className="set-chips">
      {opts.map((o) => {
        const on = sel.includes(o.value)
        return (
          <button
            key={o.value}
            type="button"
            className={`set-pick ${on ? 'on' : ''}`}
            aria-pressed={on}
            onClick={() => onChange(opts.filter((x) => (x.value === o.value ? !on : sel.includes(x.value))).map((x) => x.value))}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

function SymbolsField({ value, onChange, a11y }: { value: unknown; onChange: (v: string[]) => void; a11y: A11y }) {
  const list = Array.isArray(value) ? value.map(String) : []
  const [text, setText] = useState('')
  const add = () => {
    const parts = text
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean)
    if (!parts.length) return
    const next = [...list]
    for (const p of parts) if (!next.includes(p)) next.push(p)
    onChange(next)
    setText('')
  }
  return (
    <div className="col gap-1">
      <div className="set-chips">
        {list.map((s) => (
          <span key={s} className="set-chip mono">
            {s}
            <button type="button" aria-label={`Quitar ${s}`} title="Quitar" onClick={() => onChange(list.filter((x) => x !== s))}>
              <X size={11} />
            </button>
          </span>
        ))}
        {!list.length && <span className="tiny muted">Ninguno</span>}
      </div>
      <div className="row gap-1">
        <input
          className="input mono grow"
          id={a11y.id}
          aria-describedby={a11y.describedBy}
          aria-invalid={a11y.invalid || undefined}
          value={text}
          placeholder="BINANCE:ETHUSDT y Enter"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            } else if (e.key === 'Backspace' && !text && list.length) onChange(list.slice(0, -1))
          }}
          onBlur={add}
        />
      </div>
    </div>
  )
}

function MarketsField({ value, onChange, markets }: { value: unknown; onChange: (v: string[]) => void; markets: { id: string; name: string }[] }) {
  const sel = Array.isArray(value) ? value.map(String) : []
  const all = [...markets]
  for (const id of sel) if (!all.some((m) => m.id === id)) all.push({ id, name: id })
  return (
    <div className="set-chips">
      {all.map((m) => {
        const on = sel.includes(m.id)
        return (
          <button
            key={m.id}
            type="button"
            className={`set-pick ${on ? 'on' : ''}`}
            aria-pressed={on}
            onClick={() => onChange(on ? sel.filter((x) => x !== m.id) : [...sel, m.id])}
            title={m.id}
          >
            {m.name}
          </button>
        )
      })}
    </div>
  )
}

function Tp3Field({ value, onChange, def, a11y }: { value: unknown; onChange: (v: (number | null)[]) => void; def: FieldDef; a11y: A11y }) {
  const arr: (number | null)[] = Array.isArray(value) ? [0, 1, 2].map((i) => (finite(value[i]) ? (value[i] as number) : null)) : [null, null, null]
  return (
    <div className="set-tp3">
      {arr.map((v, i) => (
        <label key={i} className="set-tp">
          <span className="tiny muted">TP{i + 1}</span>
          <NumField
            def={def}
            value={v}
            small
            a11y={{ describedBy: a11y.describedBy, invalid: a11y.invalid }}
            onChange={(n) => {
              const next = [...arr]
              next[i] = n
              onChange(next)
            }}
          />
          <span className="tiny muted">R</span>
        </label>
      ))}
    </div>
  )
}

/** Fee % per side by asset class (risk.fee_pct_by_class): one number per class the server knows. */
function FeeClassesField({ value, onChange, def, a11y }: { value: unknown; onChange: (v: Record<string, unknown>) => void; def: FieldDef; a11y: A11y }) {
  const map = value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
  const known = FEE_CLASSES.map((c) => c.id).filter((id) => id in map)
  const keys = [...known, ...Object.keys(map).filter((k) => !known.includes(k))]
  if (!keys.length) return <span className="tiny muted">Este servidor no admite comisiones por clase de activo.</span>
  return (
    <div className="set-tp3">
      {keys.map((k) => (
        <label key={k} className="set-tp">
          <span className="tiny muted">{FEE_CLASS_LABEL[k] ?? k}</span>
          <NumField
            def={def}
            value={map[k]}
            small
            a11y={{ describedBy: a11y.describedBy, invalid: a11y.invalid }}
            onChange={(n) => onChange({ ...map, [k]: n })}
          />
          <span className="tiny muted">{def.unit ?? ''}</span>
        </label>
      ))}
    </div>
  )
}

function valueHint(def: FieldDef, value: unknown): string | null {
  if (!finite(value)) return null
  if (def.kind === 'seconds') return `= ${humanSeconds(value)}`
  if (def.format === 'percent01') return `= ${fmtNum(value * 100, 0)} %`
  return null
}

interface Props {
  def: FieldDef
  value: unknown
  dirty: boolean
  error: string | null
  markets: { id: string; name: string }[]
  onChange: (key: string, value: unknown) => void
  onRevert: (key: string) => void
}

/** Kinds rendered as several controls: their label names the group instead of pointing at one control. */
const GROUP_KINDS = new Set<FieldDef['kind']>(['markets', 'multi', 'tp3', 'fee_classes'])

function SettingField({ def, value, dirty, error, markets, onChange, onRevert }: Props) {
  const set = (v: unknown) => onChange(def.key, v)
  const uid = useId()
  const labelId = `${uid}-label`
  const helpId = `${uid}-help`
  const group = GROUP_KINDS.has(def.kind)
  const a11y: A11y = { id: group ? undefined : `${uid}-control`, describedBy: helpId, invalid: !!error }
  let control: ReactNode
  switch (def.kind) {
    case 'number':
    case 'int':
    case 'seconds':
      control = (
        <div className="set-inline">
          <NumField def={def} value={value} onChange={set} a11y={a11y} />
          {def.unit && <span className="set-unit">{def.unit}</span>}
          {valueHint(def, value) && <span className="set-unit muted">{valueHint(def, value)}</span>}
        </div>
      )
      break
    case 'bool':
      control = (
        <Toggle id={a11y.id} aria-describedby={helpId} checked={value === true} onChange={set} label={value === true ? 'Activado' : 'Desactivado'} />
      )
      break
    case 'tf': {
      const cur = typeof value === 'string' ? value : '1h'
      control = (
        <Select
          id={a11y.id}
          aria-describedby={helpId}
          value={cur}
          onChange={set}
          options={(TIMEFRAMES as string[]).includes(cur) ? TIMEFRAMES : [cur, ...TIMEFRAMES]}
        />
      )
      break
    }
    case 'select': {
      const cur = typeof value === 'string' ? value : ''
      const opts = def.options ?? []
      const all = !cur || opts.some((o) => o.value === cur) ? opts : [{ value: cur, label: cur }, ...opts]
      control = <Select id={a11y.id} aria-describedby={helpId} value={cur} onChange={set} options={all} />
      break
    }
    case 'secret':
      control = <SecretField def={def} value={value} onChange={set} a11y={a11y} />
      break
    case 'password':
      control = <SecretField def={def} value={value} onChange={set} a11y={a11y} noun="contraseña" />
      break
    case 'text':
      control = <TextField def={def} value={value} onChange={set} a11y={a11y} />
      break
    case 'markets':
      control = <MarketsField value={value} onChange={set} markets={markets} />
      break
    case 'multi':
      control = <MultiField def={def} value={value} onChange={set} />
      break
    case 'tp3':
      control = <Tp3Field def={def} value={value} onChange={set} a11y={a11y} />
      break
    case 'fee_classes':
      control = <FeeClassesField def={def} value={value} onChange={set} a11y={a11y} />
      break
    case 'symbols':
      control = <SymbolsField value={value} onChange={set} a11y={a11y} />
      break
    default:
      control = <span className="muted">—</span>
  }

  return (
    <div className={`set-field ${def.wide ? 'wide' : ''} ${dirty ? 'dirty' : ''} ${error ? 'invalid' : ''}`} id={`set-field-${def.key}`}>
      <div className="set-field-head">
        {group ? (
          <span className="set-label" id={labelId} title={def.key}>
            {def.label}
          </span>
        ) : (
          <label className="set-label" id={labelId} htmlFor={a11y.id} title={def.key}>
            {def.label}
          </label>
        )}
        {dirty && <span className="set-dirty" title="Modificado sin guardar" />}
        <div className="grow" />
        {dirty && <IconButton size="sm" icon={<Undo2 size={13} />} title="Volver al valor guardado" onClick={() => onRevert(def.key)} />}
      </div>
      {group ? (
        <div role="group" aria-labelledby={labelId} aria-describedby={helpId} style={{ minWidth: 0 }}>
          {control}
        </div>
      ) : (
        control
      )}
      {error ? (
        <span className="set-error" id={helpId}>
          {error}
        </span>
      ) : (
        <span className="set-help" id={helpId}>
          {def.help}
        </span>
      )}
    </div>
  )
}

export default memo(SettingField)
