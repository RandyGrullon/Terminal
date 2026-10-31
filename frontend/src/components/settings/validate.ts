// Client-side validation and change detection for the settings form (the backend validates again on save).
import type { Settings } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { fmtNum } from '../../lib/format'
import { FEE_CLASS_LABEL, type FieldDef } from './schema'

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const SYMBOL_RE = /^[A-Z0-9_.!&-]+(:[A-Z0-9_.!&/-]+)?$/i

function fmtLimit(v: number): string {
  return Number.isInteger(v) ? fmtNum(v, 0) : fmtNum(v, v < 1 ? 3 : 2)
}

function rangeError(def: FieldDef, v: number): string | null {
  const hasMin = finite(def.min)
  const hasMax = finite(def.max)
  if ((hasMin && v < (def.min as number)) || (hasMax && v > (def.max as number))) {
    if (hasMin && hasMax) return `Debe estar entre ${fmtLimit(def.min as number)} y ${fmtLimit(def.max as number)}`
    if (hasMin) return `Debe ser al menos ${fmtLimit(def.min as number)}`
    return `Debe ser como máximo ${fmtLimit(def.max as number)}`
  }
  return null
}

export function validateField(def: FieldDef, value: unknown): string | null {
  switch (def.kind) {
    case 'number':
    case 'int':
    case 'seconds': {
      if (value === null || value === undefined || value === '') return 'Obligatorio'
      if (!finite(value)) return 'Número no válido'
      if (def.kind !== 'number' && !Number.isInteger(value)) return 'Debe ser un número entero'
      return rangeError(def, value)
    }
    case 'bool':
      return typeof value === 'boolean' ? null : 'Valor no válido'
    case 'tf':
      return typeof value === 'string' && (TIMEFRAMES as string[]).includes(value) ? null : 'Temporalidad no válida'
    case 'select':
      return typeof value === 'string' && value ? null : 'Elige una opción'
    case 'secret':
      if (typeof value !== 'string') return 'Valor no válido'
      return /\s/.test(value.trim()) ? 'La clave no debe contener espacios' : null
    case 'markets':
      if (!Array.isArray(value)) return 'Valor no válido'
      return value.length ? null : 'Elige al menos un mercado'
    case 'symbols': {
      if (!Array.isArray(value)) return 'Valor no válido'
      const bad = value.find((s) => typeof s !== 'string' || !SYMBOL_RE.test(s))
      return bad === undefined ? null : `Símbolo no válido: ${String(bad)}`
    }
    case 'tp3': {
      if (!Array.isArray(value) || value.length !== 3) return 'Indica los tres objetivos'
      if (!value.every(finite)) return 'Los tres objetivos deben ser números'
      for (const v of value as number[]) {
        const e = rangeError(def, v)
        if (e) return e
      }
      const [a, b, c] = value as number[]
      return a < b && b < c ? null : 'Deben ir de menor a mayor: TP1 < TP2 < TP3'
    }
    case 'fee_classes': {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return 'Valor no válido'
      for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
        const name = FEE_CLASS_LABEL[k] ?? k
        if (v === null || v === undefined || v === '') return `${name}: obligatorio`
        if (!finite(v)) return `${name}: número no válido`
        const e = rangeError(def, v)
        if (e) return `${name}: ${e.charAt(0).toLowerCase()}${e.slice(1)}`
      }
      return null
    }
    default:
      return null
  }
}

/** Rules that involve more than one key. Returns key -> message. */
export function crossValidate(values: Settings): Record<string, string> {
  const out: Record<string, string> = {}
  const take = values['gate.take_score']
  const wait = values['gate.wait_score']
  if (finite(take) && finite(wait) && wait >= take) out['gate.wait_score'] = 'Debe ser menor que la puntuación para TOMAR'
  return out
}

function canonical(v: unknown): string {
  if (finite(v)) return String(Math.round(v * 1e9) / 1e9)
  if (Array.isArray(v)) return `[${v.map(canonical).join(',')}]`
  if (typeof v === 'string') return JSON.stringify(v)
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return `{${Object.keys(o)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(o[k])}`)
      .join(',')}}`
  }
  return JSON.stringify(v ?? null)
}

export function sameValue(a: unknown, b: unknown): boolean {
  return canonical(a) === canonical(b)
}

/** Finds which of the given keys a backend error message refers to ("Valor no válido para risk.fee_pct: …"). */
export function keyInMessage(message: string, keys: string[]): string | null {
  const sorted = [...keys].sort((x, y) => y.length - x.length)
  return sorted.find((k) => message.includes(k)) ?? null
}
