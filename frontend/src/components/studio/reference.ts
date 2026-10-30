// PyChart API reference: loaded once from /api/scripts/reference and shared by the "Referencia" tab and the
// editor autocompletion. A static list of the documented API (ARCHITECTURE §12) keeps completion useful even
// when the backend is not reachable.
import type { Completion, CompletionContext, CompletionResult, CompletionSection } from '@codemirror/autocomplete'
import { api } from '../../api/client'
import type { ScriptReference } from '../../api/types'

export type RefItem = ScriptReference['sections'][number]['items'][number]

let cache: ScriptReference | null = null
let pending: Promise<ScriptReference> | null = null
let options: Completion[] | null = null

function sanitize(ref: ScriptReference | null | undefined): ScriptReference {
  const sections = Array.isArray(ref?.sections) ? ref.sections : []
  return {
    sections: sections
      .filter((s) => s && typeof s.title === 'string')
      .map((s) => ({
        title: s.title,
        items: (Array.isArray(s.items) ? s.items : []).filter((i) => i && typeof i.name === 'string' && i.name.trim() !== ''),
      })),
  }
}

/** Loads the reference (cached for the whole session). `force` re-fetches after an error. */
export function loadReference(force = false): Promise<ScriptReference> {
  if (cache && !force) return Promise.resolve(cache)
  if (pending && !force) return pending
  const p = api.scripts
    .reference()
    .then((ref) => {
      cache = sanitize(ref)
      options = null
      return cache
    })
    .finally(() => {
      if (pending === p) pending = null
    })
  pending = p
  return p
}

export function cachedReference(): ScriptReference | null {
  return cache
}

// ----------------------------------------------------------------------------- static fallback (contract)

const TA = [
  'sma', 'ema', 'wma', 'hma', 'rma', 'vwma', 'rsi', 'macd', 'stoch', 'atr', 'tr', 'bb', 'kc', 'donchian',
  'supertrend', 'sar', 'adx', 'dmi', 'cci', 'mfi', 'obv', 'roc', 'mom', 'stdev', 'variance', 'highest', 'lowest',
  'highestbars', 'lowestbars', 'change', 'crossover', 'crossunder', 'cross', 'rising', 'falling', 'pivothigh',
  'pivotlow', 'barssince', 'valuewhen', 'cum', 'linreg', 'correlation', 'percentrank', 'median', 'vwap', 'wpr',
  'cmo', 'tsi',
]

const STATIC: { section: string; items: [string, string][] }[] = [
  {
    section: 'Declaración',
    items: [
      ['indicator', '(title, overlay=True)'],
      ['strategy', '(title, initial_capital=10000, risk_pct=1, fee_pct=0.1)'],
    ],
  },
  {
    section: 'Series',
    items: ['open', 'high', 'low', 'close', 'volume', 'time', 'hl2', 'hlc3', 'ohlc4', 'bar_index'].map((n): [string, string] => [n, 'serie']),
  },
  {
    section: 'Entradas',
    items: ['int', 'float', 'bool', 'string', 'select', 'source'].map((n): [string, string] => [`input.${n}`, '(defval, title)']),
  },
  { section: 'Análisis técnico (ta)', items: TA.map((n): [string, string] => [`ta.${n}`, '(...)']) },
  {
    section: 'Dibujo',
    items: [
      ['plot', '(series, title, color, linewidth, style, pane)'],
      ['plotshape', '(cond, title, location, color, shape, text)'],
      ['hline', '(price, title, color, style)'],
      ['bgcolor', '(cond, color)'],
      ['alertcondition', '(cond, title, message)'],
    ],
  },
  {
    section: 'Estrategia',
    items: [
      ['strategy.entry', '(id, direction, when, qty_pct)'],
      ['strategy.exit', '(id, from_entry, stop, limit, trail_atr)'],
      ['strategy.close', '(id, when)'],
    ],
  },
  {
    section: 'Utilidades',
    items: [
      ['nz', '(x, replacement=0)'],
      ['na', '(x)'],
      ['fixnan', '(x)'],
      ['print', '(*valores)'],
      ...['red', 'green', 'blue', 'orange', 'yellow', 'purple', 'aqua', 'teal', 'lime', 'gray', 'white', 'black'].map(
        (c): [string, string] => [`color.${c}`, 'color'],
      ),
    ],
  },
]

function completionType(name: string, signature: string): string {
  if (name.startsWith('color.')) return 'constant'
  if (signature.includes('(')) return 'function'
  return 'variable'
}

function detailOf(name: string, signature: string): string {
  const sig = signature.trim()
  if (!sig) return ''
  return sig.startsWith(name) ? sig.slice(name.length).trim() : sig
}

function buildOptions(): Completion[] {
  const seen = new Set<string>()
  const out: Completion[] = []
  const sectionCache = new Map<string, CompletionSection>()
  const sectionOf = (name: string, rank: number) => {
    let s = sectionCache.get(name)
    if (!s) {
      s = { name, rank }
      sectionCache.set(name, s)
    }
    return s
  }
  const push = (name: string, signature: string, doc: string, section: string, rank: number) => {
    if (seen.has(name)) return
    seen.add(name)
    out.push({
      label: name,
      detail: detailOf(name, signature),
      info: doc ? `${signature || name}\n\n${doc}` : signature || undefined,
      type: completionType(name, signature),
      section: sectionOf(section, rank),
      boost: name.includes('.') ? 1 : 2,
    })
  }
  if (cache) {
    cache.sections.forEach((s, i) => s.items.forEach((it) => push(it.name.trim(), it.signature ?? '', it.doc ?? '', s.title, i)))
  }
  const base = cache ? cache.sections.length : 0
  STATIC.forEach((s, i) => s.items.forEach(([n, sig]) => push(n, sig.startsWith('(') ? `${n}${sig}` : sig, '', s.section, base + i)))
  return out
}

function completionOptions(): Completion[] {
  if (!options) options = buildOptions()
  return options
}

/** CodeMirror completion source for the PyChart API (dotted names such as `ta.ema` included). */
export function pychartCompletionSource(context: CompletionContext): CompletionResult | null {
  const word = context.matchBefore(/[A-Za-z_][\w.]*/)
  if (!word) {
    return context.explicit ? { from: context.pos, options: completionOptions(), validFor: /^[\w.]*$/ } : null
  }
  if (word.from === word.to && !context.explicit) return null
  return { from: word.from, options: completionOptions(), validFor: /^[\w.]*$/ }
}

// Start loading the reference early so autocompletion has the documentation as soon as possible.
export function warmReference(): void {
  loadReference().catch(() => {
    /* the Reference tab shows the error and offers a retry */
  })
}
