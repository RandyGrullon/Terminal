// Helpers for script inputs (input.int/float/bool/string/select/source): option lists, validation and pruning of
// the user's overrides after a compile.
import type { ScriptInput } from '../../api/types'
import type { InputOverrides } from './studioStore'

export const SOURCE_OPTIONS = ['close', 'open', 'high', 'low', 'hl2', 'hlc3', 'ohlc4', 'volume']

export function inputOptions(def: ScriptInput): string[] {
  const opts = Array.isArray(def.options) ? def.options.map(String) : []
  if (opts.length) return opts
  return def.type === 'source' ? SOURCE_OPTIONS : []
}

/** True when `value` is acceptable for the input definition (used to drop stale overrides after a compile). */
export function isValidInputValue(def: ScriptInput, value: unknown): boolean {
  switch (def.type) {
    case 'int':
      return typeof value === 'number' && Number.isInteger(value)
    case 'float':
      return typeof value === 'number' && Number.isFinite(value)
    case 'bool':
      return typeof value === 'boolean'
    case 'string':
      return typeof value === 'string'
    case 'select':
    case 'source': {
      if (typeof value !== 'string') return false
      const opts = inputOptions(def)
      return !opts.length || opts.includes(value)
    }
    default:
      return false
  }
}

/** Keeps only overrides that still match an input with a compatible type and differ from its default. */
export function pruneOverrides(defs: ScriptInput[], overrides: InputOverrides): InputOverrides {
  const out: InputOverrides = {}
  for (const d of defs) {
    if (!(d.name in overrides)) continue
    const v = overrides[d.name]
    if (isValidInputValue(d, v) && v !== d.default) out[d.name] = v
  }
  return out
}
