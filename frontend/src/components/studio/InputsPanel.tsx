// "Entradas" panel: one control per script input (input.int/float/bool/string/select/source). Only the values the
// user changes are stored (overrides); everything else follows the defaults written in the code.
import { memo, useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, RotateCcw } from 'lucide-react'
import type { ScriptInput } from '../../api/types'
import { Button, Select, Toggle } from '../ui'
import { defaultStep } from './chartUtils'
import { inputOptions } from './inputs'
import type { InputOverrides } from './studioStore'

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function inBounds(def: ScriptInput, v: number): boolean {
  return (!finite(def.min) || v >= def.min) && (!finite(def.max) || v <= def.max)
}

function clampNum(def: ScriptInput, v: number): number {
  let x = v
  if (finite(def.min)) x = Math.max(def.min, x)
  if (finite(def.max)) x = Math.min(def.max, x)
  return def.type === 'int' ? Math.round(x) : x
}

/** Number field with a local text buffer: commits valid values while typing, clamps and normalizes on blur. */
function InputNumber({ def, value, onCommit }: { def: ScriptInput; value: number | null; onCommit: (v: number) => void }) {
  const [text, setText] = useState(value === null ? '' : String(value))
  const focused = useRef(false)
  useEffect(() => {
    if (!focused.current) setText(value === null ? '' : String(value))
  }, [value])
  return (
    <input
      className="input mono"
      type="number"
      value={text}
      min={def.min}
      max={def.max}
      step={def.step ?? defaultStep(def.type === 'int' ? 'int' : 'float', def.default)}
      onFocus={() => {
        focused.current = true
      }}
      onChange={(e) => {
        const raw = e.target.value
        setText(raw)
        const n = Number(raw)
        if (raw.trim() === '' || !Number.isFinite(n)) return
        const v = def.type === 'int' ? Math.round(n) : n
        if (inBounds(def, v)) onCommit(v)
      }}
      onBlur={() => {
        focused.current = false
        const n = Number(text)
        if (text.trim() === '' || !Number.isFinite(n)) {
          setText(value === null ? '' : String(value))
          return
        }
        const v = clampNum(def, n)
        setText(String(v))
        if (v !== value) onCommit(v)
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') e.currentTarget.blur()
      }}
    />
  )
}

interface Props {
  defs: ScriptInput[]
  overrides: InputOverrides
  onChange: (name: string, value: unknown) => void
  onReset: () => void
}

function InputsPanel({ defs, overrides, onChange, onReset }: Props) {
  const [open, setOpen] = useState(true)
  if (!defs.length) return null
  const changed = Object.keys(overrides).length

  const control = (def: ScriptInput) => {
    const value = def.name in overrides ? overrides[def.name] : def.default
    switch (def.type) {
      case 'int':
      case 'float':
        return <InputNumber def={def} value={finite(value) ? value : null} onCommit={(v) => onChange(def.name, v)} />
      case 'bool':
        return <Toggle checked={value === true} onChange={(v) => onChange(def.name, v)} label={value === true ? 'Sí' : 'No'} />
      case 'select':
      case 'source': {
        const opts = inputOptions(def)
        const cur = typeof value === 'string' ? value : String(value ?? '')
        const all = !cur || opts.includes(cur) ? opts : [cur, ...opts]
        if (!all.length) {
          return <input className="input" value={cur} onChange={(e) => onChange(def.name, e.target.value)} />
        }
        return <Select value={cur} options={all.map((o) => ({ value: o, label: o }))} onChange={(v) => onChange(def.name, v)} />
      }
      default:
        return (
          <input
            className="input"
            value={typeof value === 'string' ? value : String(value ?? '')}
            onChange={(e) => onChange(def.name, e.target.value)}
          />
        )
    }
  }

  return (
    <div className="studio-inputs">
      <div className="studio-inputs-head">
        <button type="button" className="studio-link" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          Entradas <span className="muted">({defs.length})</span>
        </button>
        {changed > 0 && <span className="tiny accent">{changed} modificada(s)</span>}
        <div className="grow" />
        {changed > 0 && (
          <Button size="sm" variant="ghost" icon={<RotateCcw size={13} />} onClick={onReset} title="Volver a los valores del código">
            Restablecer
          </Button>
        )}
      </div>
      {open && (
        <div className="studio-inputs-grid">
          {defs.map((def) => (
            <div key={def.name} className={`studio-input ${def.name in overrides ? 'changed' : ''}`}>
              <label title={`${def.name} · ${def.type}`}>{def.title || def.name}</label>
              {control(def)}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export default memo(InputsPanel)
