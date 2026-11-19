// Plain-language helpers of the simple mode: why a trade opened in everyday words, how fast it is, where its price
// stands between the stop and the target, today's results and what NEXUS is doing now. Pure functions (tested in
// tests/simple-friendly.test.mjs); the technical texts stay one tap away in the trade's full detail.
import type { Position } from '../../api/types'
import type { AutopilotSnapshot } from '../../api/types-autopilot'

export type SourceKind = 'nexus' | 'strategy' | 'hold'

export interface TradeSource {
  kind: SourceKind
  /** Short chip: «NEXUS», «Supertrend», «Ruptura», «Cruce de medias», «Cartera»… */
  short: string
  /** The strategy's full name when it is one. */
  strategy: string | null
}

const PARAM_SOURCE = /^Fuente/
// the name after the word «estrategia» (the why text also quotes the entry's name, «Largo» / «Corto», before it)
const STRATEGY_NAME = /estrategia\s+«([^»]+)»/i

/** Where the trade's signal came from, from its plan (params «Fuente…» or the why text). */
export function sourceOf(p: Position): TradeSource {
  if (p.hold) return { kind: 'hold', short: 'Cartera', strategy: null }
  const param = p.plan?.params?.find((x) => PARAM_SOURCE.test(x.label))?.value ?? ''
  const why = p.plan?.why ?? ''
  const text = /Estrategia|estrategia/.test(param) ? param : /estrategia/.test(why) ? why : ''
  if (text) {
    const name = (text.match(STRATEGY_NAME)?.[1] ?? 'Estrategia').replace(/\s*\(estrategia\)\s*$/i, '')
    return { kind: 'strategy', short: strategyShort(name), strategy: name }
  }
  return { kind: 'nexus', short: 'NEXUS', strategy: null }
}

export function strategyShort(name: string): string {
  if (/supertrend/i.test(name)) return 'Supertrend'
  if (/donchian|breakout|ruptura/i.test(name)) return 'Ruptura'
  if (/\bema\b|cruce|media/i.test(name)) return 'Cruce de medias'
  if (/macd/i.test(name)) return 'MACD'
  if (/genxau/i.test(name)) return 'GenXAU'
  return name.length > 18 ? `${name.slice(0, 17)}…` : name
}

/** Why it opened, in everyday words (one sentence). */
export function friendlyReason(p: Position): string {
  const up = p.side === 'long'
  const src = sourceOf(p)
  if (src.kind === 'hold') return 'Es parte de la cartera de largo plazo: compra y espera, sin objetivo fijo.'
  const dir = up ? 'subir' : 'bajar'
  if (src.kind === 'strategy') {
    const n = src.strategy ?? ''
    if (/supertrend/i.test(n)) return `La tendencia giró: la estrategia Supertrend vio fuerza para ${dir}.`
    if (/donchian|breakout|ruptura/i.test(n))
      return `El precio rompió el ${up ? 'máximo' : 'mínimo'} de las últimas velas y puede seguir hacia ${up ? 'arriba' : 'abajo'}.`
    if (/\bema\b|cruce|media/i.test(n)) return `La media rápida cruzó ${up ? 'por encima' : 'por debajo'} de la lenta: señal de ${dir}.`
    if (/macd/i.test(n)) return `El MACD cruzó su señal hacia ${up ? 'arriba' : 'abajo'}.`
    return `La estrategia «${n}» dio una señal para ${dir}.`
  }
  const approved = /TOMAR/.test(p.plan?.why ?? '') && !/NO TOMAR/.test(p.plan?.why ?? '')
  return `El motor NEXUS vio señal para ${dir}${approved ? ' y su filtro la aprobó' : ''}.`
}

/** «Compra» / «Venta» and what it hopes for. */
export function sideWords(p: Position): { label: string; hope: string } {
  return p.side === 'long' ? { label: 'Compra', hope: 'gana si sube' } : { label: 'Venta', hope: 'gana si baja' }
}

const TF_WORDS: Record<string, string> = {
  '1m': 'velas de 1 min',
  '3m': 'velas de 3 min',
  '5m': 'velas de 5 min',
  '15m': 'velas de 15 min',
  '30m': 'velas de 30 min',
  '1h': 'velas de 1 hora',
  '2h': 'velas de 2 horas',
  '4h': 'velas de 4 horas',
  '1d': 'velas diarias',
  '1w': 'velas semanales',
}

/** How fast the trade is: «Rápida» (≤ 15 min), «Media» (30 min–1 h), «Lenta» (more). */
export function speedOf(tf: string): { label: string; detail: string } {
  const detail = TF_WORDS[tf] ?? tf
  if (['1m', '3m', '5m', '15m'].includes(tf)) return { label: 'Rápida', detail }
  if (['30m', '1h'].includes(tf)) return { label: 'Media', detail }
  return { label: 'Lenta', detail }
}

export interface PriceBar {
  /** 0 = at the stop, 1 = at the target (or, with no target, as far above the entry as the stop is below). */
  now: number
  entry: number
  stop: number
  end: number
  endLabel: string
  hasTarget: boolean
}

const clamp01 = (v: number) => Math.max(0, Math.min(1, v))

/** Where an open trade's price stands between its stop and its target, left to right for both sides. */
export function priceBarOf(p: Position): PriceBar | null {
  if (p.status !== 'open' || p.hold) return null
  const stop = p.sl
  const entry = p.entry_price
  const price = p.price ?? entry
  if (![stop, entry, price].every(Number.isFinite) || stop === entry) return null
  const target = p.plan?.target ?? p.tp1 ?? null
  const hasTarget = target !== null && Number.isFinite(target) && (target - entry) * (entry - stop) > 0
  const end = hasTarget ? (target as number) : entry + (entry - stop)
  const span = end - stop
  return {
    now: clamp01((price - stop) / span),
    entry: clamp01((entry - stop) / span),
    stop,
    end,
    endLabel: hasTarget ? 'Objetivo' : 'Sin objetivo fijo',
    hasTarget,
  }
}

export interface TodaySummary {
  closed: number
  wins: number
  pnl: number
}

/** Trades closed since local midnight (from the history the snapshot carries). */
export function todayOf(history: Position[], now = Date.now()): TodaySummary {
  const d = new Date(now)
  const start = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000
  const today = history.filter((p) => (p.exit_time ?? 0) >= start)
  return {
    closed: today.length,
    wins: today.filter((p) => p.pnl > 0).length,
    pnl: today.reduce((s, p) => s + (Number.isFinite(p.pnl) ? p.pnl : 0), 0),
  }
}

const shortSym = (s: string) => s.replace(/^[A-Z_]+:/, '').replace(/\.P$/, '')
const marketWord = (s: string) => (/XAU/.test(s) ? 'oro' : shortSym(s))

/** What NEXUS is doing now with this account, in one or two plain sentences. */
export function nexusNow(snap: AutopilotSnapshot, accountId: number, open: Position[]): { title: string; detail: string; tone: 'ok' | 'wait' | 'stop' } {
  const st = snap.autopilot
  if (!st?.enabled) return { title: 'En pausa', detail: 'NEXUS no abre operaciones nuevas hasta que lo actives en el modo Pro.', tone: 'stop' }
  const isMain = accountId === snap.account.id
  if (isMain && snap.risk?.blocked) {
    const until = snap.risk.blocked_until ? new Date(snap.risk.blocked_until * 1000).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' }) : null
    return {
      title: 'Frenada por hoy',
      detail: `Llegó a su límite de pérdida del día: no abre nada nuevo${until ? ` hasta las ${until}` : ''}. Las abiertas siguen con su stop.`,
      tone: 'stop',
    }
  }
  const session = st.session?.active ? ' Sesión de Nueva York abierta: modo agresivo.' : ''
  if (open.length) {
    const markets = [...new Set(open.map((p) => marketWord(p.symbol)))]
    return {
      title: `Vigilando ${open.length} ${open.length === 1 ? 'operación' : 'operaciones'}`,
      detail: `En ${markets.join(', ')}. Cada una se cierra sola con su stop o su objetivo, y busca nuevas entradas al cierre de cada vela.${session}`,
      tone: 'ok',
    }
  }
  return {
    title: 'Esperando una señal',
    detail: `Abre una operación cuando NEXUS o una de sus estrategias da entrada al cierre de una vela.${session}`,
    tone: 'wait',
  }
}

export const GLOSSARY: { term: string; text: string }[] = [
  { term: 'Valor de la cuenta', text: 'Lo que valdría la cuenta si cerrara todo ahora: el efectivo más lo que van ganando o perdiendo las abiertas.' },
  { term: 'Efectivo', text: 'El dinero ya realizado: el saldo inicial más lo ganado o perdido en operaciones cerradas.' },
  { term: 'En juego', text: 'Lo que van ganando o perdiendo las operaciones abiertas. Cambia con el precio y todavía no es definitivo.' },
  { term: 'Ya cerrado', text: 'Lo ganado o perdido en operaciones que ya terminaron.' },
  { term: 'Compra / Venta', text: 'Compra gana si el precio sube; Venta gana si el precio baja.' },
  { term: 'Stop', text: 'El precio donde la operación se cierra sola para limitar la pérdida.' },
  { term: 'Objetivo', text: 'El precio donde toma ganancia. Algunas estrategias no tienen objetivo fijo: se cierran con su propia regla.' },
  { term: 'R', text: 'El resultado medido en «riesgos»: +1R es ganar lo mismo que se arriesgaba hasta el stop; −1R es perder eso.' },
  { term: 'Rápida, media, lenta', text: 'El tamaño de las velas con que decide: 5 o 15 minutos (rápida), 1 hora (media), 4 horas o un día (lenta).' },
  { term: 'Cuentas de reto', text: 'Cuentas aparte con una meta (por ejemplo, llevar 500 $ a 1.000 $). Usan las mismas señales en modo agresivo.' },
  { term: 'x de n', text: 'Cuántas operaciones ganaron de cuántas cerró. Con menos de 30 todavía no dice nada fiable.' },
]
