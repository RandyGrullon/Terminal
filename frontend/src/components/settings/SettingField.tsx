// One settings field: label + control (by kind) + help, with dirty marker, per-field revert and inline errors.
import { memo, useEffect, useRef, useState, type ReactNode } from 'react'
import { ExternalLink, Eye, EyeOff, Undo2, X } from 'lucide-react'
import { TIMEFRAMES } from '../../api/types'
import { fmtNum } from '../../lib/format'
import { IconButton, Select, Toggle } from '../ui'
import type { FieldDef } from './schema'

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

/** Number input with a local text buffer so intermediate states ("0.", "-") can be typed. */
function NumField({ value, onChange, def, small }: { value: unknown; onChange: (v: number | null) => void; def: FieldDef; small?: boolean }) {
  const num = finite(value) ? value : null
  const [text, setText] = useState(num === null ? '' : String(num))
  const focused = useRef(false)
  useEffect(() => {
    if (!focused.current) setText(num === null ? '' : String(num))
  }, [num])
  return (
    <input
      className={`input mono set-num ${small ? 'small' : ''}`}
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

function SecretField({ value, onChange, def }: { value: unknown; onChange: (v: string) => void; def: FieldDef }) {
  const [show, setShow] = useState(false)
  const v = typeof value === 'string' ? value : ''
  return (
    <div className="col gap-1">
      <div className="row gap-1">
        <input
          className="input mono grow"
          type={show ? 'text' : 'password'}
          value={v}
          autoComplete="off"
          spellCheck={false}
          placeholder="Sin configurar"
          onChange={(e) => onChange(e.target.value)}
        />
        <IconButton
          size="sm"
          icon={show ? <EyeOff size={14} /> : <Eye size={14} />}
          title={show ? 'Ocultar clave' : 'Mostrar clave'}
          onClick={() => setShow((s) => !s)}
        />
        {v && <IconButton size="sm" icon={<X size={14} />} title="Borrar clave" onClick={() => onChange('')} />}
      </div>
      {def.link && (
        <a className="tiny set-link" href={def.link.href} target="_blank" rel="noopener noreferrer">
          <ExternalLink size={11} /> {def.link.label}
        </a>
      )}
    </div>
  )
}

function SymbolsField({ value, onChange }: { value: unknown; onChange: (v: string[]) => void }) {
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

function Tp3Field({ value, onChange, def }: { value: unknown; onChange: (v: (number | null)[]) => void; def: FieldDef }) {
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

function SettingField({ def, value, dirty, error, markets, onChange, onRevert }: Props) {
  const set = (v: unknown) => onChange(def.key, v)
  let control: ReactNode
  switch (def.kind) {
    case 'number':
    case 'int':
    case 'seconds':
      control = (
        <div className="set-inline">
          <NumField def={def} value={value} onChange={set} />
          {def.unit && <span className="set-unit">{def.unit}</span>}
          {valueHint(def, value) && <span className="set-unit muted">{valueHint(def, value)}</span>}
        </div>
      )
      break
    case 'bool':
      control = <Toggle checked={value === true} onChange={set} label={value === true ? 'Activado' : 'Desactivado'} />
      break
    case 'tf': {
      const cur = typeof value === 'string' ? value : '1h'
      control = <Select value={cur} onChange={set} options={(TIMEFRAMES as string[]).includes(cur) ? TIMEFRAMES : [cur, ...TIMEFRAMES]} />
      break
    }
    case 'select': {
      const cur = typeof value === 'string' ? value : ''
      const opts = def.options ?? []
      const all = !cur || opts.some((o) => o.value === cur) ? opts : [{ value: cur, label: cur }, ...opts]
      control = <Select value={cur} onChange={set} options={all} />
      break
    }
    case 'secret':
      control = <SecretField def={def} value={value} onChange={set} />
      break
    case 'markets':
      control = <MarketsField value={value} onChange={set} markets={markets} />
      break
    case 'tp3':
      control = <Tp3Field def={def} value={value} onChange={set} />
      break
    case 'symbols':
      control = <SymbolsField value={value} onChange={set} />
      break
    default:
      control = <span className="muted">—</span>
  }

  return (
    <div className={`set-field ${def.wide ? 'wide' : ''} ${dirty ? 'dirty' : ''} ${error ? 'invalid' : ''}`} id={`set-field-${def.key}`}>
      <div className="set-field-head">
        <label className="set-label" title={def.key}>
          {def.label}
        </label>
        {dirty && <span className="set-dirty" title="Modificado sin guardar" />}
        <div className="grow" />
        {dirty && <IconButton size="sm" icon={<Undo2 size={13} />} title="Volver al valor guardado" onClick={() => onRevert(def.key)} />}
      </div>
      {control}
      {error ? <span className="set-error">{error}</span> : <span className="set-help">{def.help}</span>}
    </div>
  )
}

export default memo(SettingField)
