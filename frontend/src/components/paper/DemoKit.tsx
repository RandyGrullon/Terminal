// Friendly building blocks of the demo page («Cuenta demo»), shared by «Mi cuenta» and «NEXUS automática»: the account
// switch, the hero with the value of the account and the result of its closed trades, the section tabs, an asset avatar,
// a tile per open trade (its path from the stop to the next target), an activity feed in plain words and a small
// labelled number. Pure views: the pages pass the data and the handlers. Everything they show is simulated (paper).
import { memo, type CSSProperties, type ReactNode } from 'react'
import { ArrowDownRight, ArrowUpRight, ChartCandlestick, CircleCheck, CircleX, OctagonAlert, TrendingDown, TrendingUp } from 'lucide-react'
import type { PaperAccount, Position, PositionPlan } from '../../api/types'
import { fmtAgo, fmtInt, fmtPct, fmtPrice, signTone, splitSymbol, type Tone } from '../../lib/format'
import { plural } from '../../lib/plural'
import { MIN_EVIDENCE_N } from '../brain/evidence'
import { Button } from '../ui'
import { fmtMoney, fmtQty, isNum } from '../markets/opsUtils'
import { adviceLevel, exitInfo, lotSizeText, lotsText, marketOf, tpHits } from './paperUtils'
import { detailProps } from './TradeDetailModal'

// ----------------------------------------------------------------------------- assets

const CRYPTO_EXCHANGES = new Set(['BINANCE', 'OKX', 'COINBASE', 'KRAKEN', 'KUCOIN', 'BYBIT', 'BITGET', 'GEMINI', 'BITSTAMP'])
const ETFS = new Set(['SPY', 'QQQ', 'IWM', 'GLD', 'DIA', 'VOO', 'VTI', 'SLV', 'TLT', 'XLK', 'XLF', 'ARKK', 'EEM', 'EFA', 'SMH', 'VT'])
const QUOTE_RE = /(USDT|USDC|FDUSD)(\.P)?$/

function split(symbol: string): { exchange: string; ticker: string } {
  const s = String(symbol || '').toUpperCase()
  const i = s.indexOf(':')
  return i >= 0 ? { exchange: s.slice(0, i), ticker: s.slice(i + 1) } : { exchange: '', ticker: s }
}

/** "Cripto", "ETF" or "Acción". */
export function assetKind(symbol: string): string {
  const { exchange, ticker } = split(symbol)
  if (CRYPTO_EXCHANGES.has(exchange) || QUOTE_RE.test(ticker)) return 'Cripto'
  return ETFS.has(ticker) ? 'ETF' : 'Acción'
}

/** "BTC" for BTCUSDT (and its perpetual BTCUSDT.P), the ticker otherwise. */
export function assetName(symbol: string): string {
  const { ticker } = split(symbol)
  return ticker.replace(QUOTE_RE, '') || ticker
}

/** Brand-like colours of the usual assets (a tint for the avatar); any other asset gets one from its name. */
const ASSET_COLORS: Record<string, string> = {
  BTC: '#f7931a', ETH: '#627eea', SOL: '#9945ff', BNB: '#f3ba2f', XRP: '#8aa4c8', DOGE: '#c2a633', ADA: '#3468d1',
  AVAX: '#e84142', LINK: '#2a5ada', AAPL: '#a2aaad', NVDA: '#76b900', MSFT: '#00a4ef', AMZN: '#ff9900', TSLA: '#e82127',
  GOOGL: '#4285f4', META: '#0866ff', SPY: '#5b8cff', QQQ: '#a97bff', MES: '#22d3ee', MNQ: '#a97bff', MYM: '#f5a524', M2K: '#16c784',
  PAXG: '#e6b422', XAUT: '#e6b422', XAUUSD: '#e6b422', GLD: '#e6b422',
}
/** Tokens backed by gold (1 token = 1 troy ounce): their price follows XAU/USD. */
const GOLD_TOKENS = new Set(['PAXG', 'XAUT'])
const PALETTE = ['#5b8cff', '#a97bff', '#22d3ee', '#f5a524', '#16c784', '#ff7ab6', '#3ea6ff', '#e8a87c']

export function assetColor(name: string): string {
  const key = name.toUpperCase()
  if (ASSET_COLORS[key]) return ASSET_COLORS[key]
  let h = 0
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0
  return PALETTE[h % PALETTE.length]
}

export interface AssetInfo {
  /** Short name: "BTC", "AAPL", "MES", an option's contract. */
  name: string
  /** What it is, in plain words: "Cripto", "Acción", "ETF", "CFD", "Futuro", "Perpetuo", "Opción". */
  kind: string
  /** The letters in its avatar and the key of its colour. */
  avatar: string
}

/** How a position's asset is named on the tiles (an option by its contract, its avatar is its stock). */
export function assetOf(p: Position): AssetInfo {
  const inst = p.instrument
  if (inst?.kind === 'option') {
    const stock = splitSymbol(inst.underlying ?? '').ticker || 'OPC'
    return { name: marketOf(p).ticker, kind: 'Opción', avatar: stock }
  }
  if (inst?.kind === 'future' && inst.perpetual) {
    const coin = assetName(p.symbol)
    const lev = isNum(inst.leverage) ? ` ${fmtInt(inst.leverage)}×` : ''
    return { name: coin, kind: GOLD_TOKENS.has(coin) ? `Oro · perpetuo${lev}` : `Perpetuo${lev}`, avatar: coin }
  }
  if (inst?.kind === 'future') {
    const root = inst.root || splitSymbol(p.symbol).ticker.replace(/\d*!$/, '')
    return { name: root, kind: 'Futuro', avatar: root }
  }
  const name = assetName(p.symbol)
  const exchange = splitSymbol(p.symbol).exchange.toUpperCase()
  const kind = GOLD_TOKENS.has(name) || name === 'XAUUSD' ? 'Oro' : exchange === 'CAPITALCOM' ? 'CFD' : assetKind(p.symbol)
  return { name, kind, avatar: name }
}

export const AssetAvatar = memo(function AssetAvatar({ name, size = 36, dim = false }: { name: string; size?: number; dim?: boolean }) {
  const letters = name.length <= 4 ? name : name.slice(0, 3)
  const style = { '--dk-c': assetColor(name), width: size, height: size, fontSize: letters.length > 3 ? size * 0.27 : size * 0.32 } as CSSProperties
  return (
    <span className={`dk-avatar${dim ? ' dim' : ''}`} style={style} aria-hidden="true">
      {letters}
    </span>
  )
})

/** "Compra" (wins if it goes up) or "Venta" (wins if it goes down); an option: "Call" / "Put" on its stock. */
export function directionOf(p: Position): { label: string; hint: string; tone: Tone; up: boolean } {
  if (p.instrument?.kind === 'option') {
    const put = p.instrument.right === 'put'
    return { label: put ? 'Put' : 'Call', hint: put ? 'Gana si la acción baja' : 'Gana si la acción sube', tone: put ? 'neg' : 'pos', up: !put }
  }
  return p.side === 'short'
    ? { label: 'Venta', hint: 'Gana si el precio baja', tone: 'neg', up: false }
    : { label: 'Compra', hint: 'Gana si el precio sube', tone: 'pos', up: true }
}

/** Units still open (all of them once closed). */
export const unitsOf = (p: Position): number => (p.qty_open > 0 ? p.qty_open : p.qty)

/** A trade's size: in MT5 lots with what they hold ("0,05 lotes (5 oz)", "3,33 lotes (3,33 ETH)") when it has a lot,
 *  else its units. A portfolio holding keeps its units (bought by amount, not in lots). */
export function sizeOf(p: Position, units: number): string {
  const lots = p.hold ? null : lotsText(p, units)
  if (lots === null || !p.lot) return fmtQty(units)
  const unit = p.lot.unit === 'acción' && units !== 1 ? 'acciones' : p.lot.unit === 'unidad' && units !== 1 ? 'unidades' : p.lot.unit
  return `${lots} (${fmtQty(units)} ${unit})`
}

/** The money a trade puts at stake, as a broker app shows it: the amount bought, or the guarantee of a leveraged one. */
export function moneyOf(p: Position): { label: string; value: number | null; title: string } {
  const units = unitsOf(p)
  const notional = isNum(units) && isNum(p.entry_price) ? units * p.entry_price : null
  const inst = p.instrument
  if (inst?.kind === 'option') return { label: 'Prima pagada', value: notional, title: 'Lo que costaron las opciones (precio teórico, simulado)' }
  if (inst?.kind === 'future' && inst.perpetual && isNum(inst.leverage) && inst.leverage > 0 && notional !== null)
    return { label: 'Garantía', value: notional / inst.leverage, title: `Margen con apalancamiento ${fmtInt(inst.leverage)}×: mueve ${fmtMoney(notional)}` }
  if (inst?.kind === 'future' && isNum(inst.margin) && isNum(inst.multiplier) && inst.multiplier > 0)
    return { label: 'Garantía aprox.', value: inst.margin * (units / inst.multiplier), title: 'Margen inicial aproximado por contrato (no el de un bróker)' }
  return { label: 'Monto', value: notional, title: 'Cantidad por precio de entrada' }
}

// ----------------------------------------------------------------------------- a trade's path

export interface TradePath {
  stop: number
  stopLabel: string
  target: number | null
  price: number
  /** 0..1 along stop → target (null without a target). */
  at: number | null
  entryAt: number | null
  /** % the price still has to move in the trade's favour to reach the target. */
  toTarget: number | null
  /** % the price can still move against it before the stop (≤ 0: at or past it). */
  toStop: number
}

const clamp01 = (v: number) => Math.max(0, Math.min(1, v))

/** Where an open trade stands between its stop (or dynamic stop) and its next target, from its own levels (an
 *  option's are premiums, like its price). Null when it has no price yet. */
export function tradePath(p: Position): TradePath | null {
  const price = p.price
  if (!isNum(price) || price <= 0 || !isNum(p.entry_price) || !isNum(p.sl)) return null
  const s = p.side === 'short' ? -1 : 1
  const trailTighter = isNum(p.trail) && s * (p.trail - p.sl) > 0
  const stop = trailTighter ? (p.trail as number) : p.sl
  const hits = isNum(p.tp_hits) ? Math.max(0, Math.min(3, Math.round(p.tp_hits))) : tpHits(p).filter(Boolean).length
  const tps = [p.tp1, p.tp2, p.tp3]
  let target: number | null = null
  for (let i = hits; i < 3; i++) {
    const t = tps[i]
    if (isNum(t)) {
      target = t
      break
    }
  }
  const toStop = (s * (price - stop) / price) * 100
  const base = { stop, stopLabel: trailTighter ? 'Stop dinámico' : 'Stop', price, toStop }
  const span = target !== null ? s * (target - stop) : 0
  if (target === null || span <= 0) return { ...base, target: null, at: null, entryAt: null, toTarget: null }
  return {
    ...base,
    target,
    at: clamp01((s * (price - stop)) / span),
    entryAt: clamp01((s * (p.entry_price - stop)) / span),
    toTarget: (s * (target - price) / price) * 100,
  }
}

export function PathBar({ path }: { path: TradePath }) {
  if (path.at === null || path.entryAt === null || path.target === null) {
    return (
      <div className="dk-path-note">
        Sin objetivo fijo · {path.stopLabel.toLowerCase()} en {fmtPrice(path.stop)}
        {path.toStop > 0 ? ` (a ${fmtPct(path.toStop, 1, false)})` : ''}
      </div>
    )
  }
  const from = Math.min(path.at, path.entryAt) * 100
  const width = Math.abs(path.at - path.entryAt) * 100
  const good = path.at >= path.entryAt
  return (
    <div className="dk-path">
      <div className="dk-path-track" role="img" aria-label={`Precio entre el stop y el objetivo: ${Math.round(path.at * 100)} % del camino`}>
        <span className={`dk-path-fill ${good ? 'pos' : 'neg'}`} style={{ left: `${from}%`, width: `${width}%` }} />
        <span className="dk-path-entry" style={{ left: `${path.entryAt * 100}%` }} title="Entrada" />
        <span className={`dk-path-dot ${good ? 'pos' : 'neg'}`} style={{ left: `${path.at * 100}%` }} title={`Ahora ${fmtPrice(path.price)}`} />
      </div>
      <div className="dk-path-labels">
        <span className="neg">
          {path.stopLabel} {fmtPrice(path.stop)}
        </span>
        <span className="pos">Objetivo {fmtPrice(path.target)}</span>
      </div>
      <div className="dk-path-note">
        {path.toTarget !== null && path.toTarget > 0 ? `Le falta ${fmtPct(path.toTarget, 1, false)} para el objetivo` : 'En el objetivo'}
        {' · '}
        {path.toStop > 0 ? `el stop está a ${fmtPct(path.toStop, 1, false)}` : 'en el stop'}
      </div>
    </div>
  )
}

// ----------------------------------------------------------------------------- open trade tile

/** «Objetivo 3.683,37 (1,6R)» or «Sin objetivo: …», and why it opened (NEXUS automática's trades). */
export function PlanLine({ plan }: { plan: PositionPlan }) {
  return (
    <div className="dk-tile-plan tiny">
      {plan.target !== null ? (
        <span>
          <b>Objetivo</b> <span className="mono">{fmtPrice(plan.target)}</span>
          {isNum(plan.target_r) ? ` (${plan.target_r.toLocaleString('es-ES', { maximumFractionDigits: 1 })}R)` : ''}
        </span>
      ) : (
        <span>
          <b>Sin objetivo:</b> {plan.no_target ?? 'sale con su stop y sus reglas de salida'}
        </span>
      )}
      <span className="muted">
        <b>Por qué:</b> {plan.why}
      </span>
    </div>
  )
}

export interface TradeTileProps {
  p: Position
  closingId: number | null
  /** Shown as a chip ("Medida · 1h"). */
  tag?: string | null
  onClose: (p: Position) => void
  onShow: (p: Position) => void
  /** A click on the tile (not on its buttons) opens the trade's detail: why it opened and what it took. */
  onDetail?: (p: Position) => void
}

export const TradeTile = memo(function TradeTile({ p, closingId, tag, onClose, onShow, onDetail }: TradeTileProps) {
  const a = assetOf(p)
  const dir = directionOf(p)
  const money = moneyOf(p)
  const path = tradePath(p)
  const level = adviceLevel(p)
  const pnlTone = signTone(p.pnl)
  return (
    <article className={`dk-tile${level === 'close' ? ' alert' : ''}${onDetail ? ' dk-clickable' : ''}`} {...detailProps(p, onDetail)}>
      <header className="dk-tile-head">
        <AssetAvatar name={a.avatar} />
        <div className="dk-tile-name">
          <b title={p.symbol}>{a.name}</b>
          <span>
            {a.kind} · abierta {fmtAgo(p.entry_time * 1000)}
          </span>
        </div>
        <span className={`dk-dir ${dir.tone}`} title={dir.hint}>
          {dir.up ? <TrendingUp size={13} /> : <TrendingDown size={13} />} {dir.label}
        </span>
      </header>
      <div className="dk-tile-pnl">
        <b className={pnlTone}>{fmtMoney(p.pnl, true)}</b>
        <span className={`dk-pct ${pnlTone}`}>{fmtPct(p.pnl_pct)}</span>
      </div>
      <div className="dk-tile-money" title={money.title}>
        {money.label} <b className="mono">{fmtMoney(money.value)}</b>
        <span className="muted" title={lotSizeText(p) ?? undefined}>
          {' '}
          · {sizeOf(p, unitsOf(p))} a {fmtPrice(p.entry_price)}
        </span>
      </div>
      {path && <PathBar path={path} />}
      {p.plan && <PlanLine plan={p.plan} />}
      {level === 'close' && p.advice && (
        <div className="dk-tile-advice" role="note">
          <OctagonAlert size={13} /> {p.advice}
        </div>
      )}
      <footer className="dk-tile-foot">
        {tag ? <span className="dk-chip">{tag}</span> : <span />}
        <span className="row gap-1">
          <Button size="sm" variant="ghost" icon={<ChartCandlestick size={13} />} onClick={() => onShow(p)} title="Ver la operación en el gráfico: entrada, stop y objetivos">
            Ver
          </Button>
          <Button size="sm" loading={closingId === p.id} disabled={closingId !== null} onClick={() => onClose(p)} title="Cerrar a precio de mercado (simulado)">
            Cerrar
          </Button>
        </span>
      </footer>
    </article>
  )
})

// ----------------------------------------------------------------------------- hero

/** Cumulative result of the closed trades, oldest first, starting at 0 (the curve of the hero). */
export function pnlCurve(history: readonly Position[]): number[] {
  const closed = history
    .filter((p) => isNum(p.pnl))
    .slice()
    .sort((a, b) => (isNum(a.exit_time) ? a.exit_time : 0) - (isNum(b.exit_time) ? b.exit_time : 0) || a.id - b.id)
  const out = [0]
  for (const p of closed) out.push(out[out.length - 1] + p.pnl)
  return out
}

export function Sparkline({ values, height = 70 }: { values: readonly number[]; height?: number }) {
  const w = 300
  const h = height
  const lo = Math.min(0, ...values)
  const hi = Math.max(0, ...values)
  const span = hi - lo || 1
  const x = (i: number) => (values.length > 1 ? (i / (values.length - 1)) * w : 0)
  const y = (v: number) => h - 4 - ((v - lo) / span) * (h - 8)
  const line = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const last = values[values.length - 1] ?? 0
  const tone = last > 0 ? 'pos' : last < 0 ? 'neg' : 'muted'
  return (
    <svg className={`dk-spark ${tone}`} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label="Resultado acumulado de las operaciones cerradas">
      <line x1="0" x2={w} y1={y(0)} y2={y(0)} className="dk-spark-zero" />
      <polygon points={`0,${y(0)} ${line} ${w},${y(0)}`} className="dk-spark-area" />
      <polyline points={line} className="dk-spark-line" />
    </svg>
  )
}

/** A labelled number: "Efectivo 89,97 $". */
export function Figure({ label, value, tone, hint, title }: { label: ReactNode; value: ReactNode; tone?: Tone; hint?: ReactNode; title?: string }) {
  return (
    <div className="dk-fig" title={title}>
      <span className="dk-fig-label">{label}</span>
      <b className={`dk-fig-value ${tone ?? ''}`}>{value}</b>
      {hint ? <span className="dk-fig-hint">{hint}</span> : null}
    </div>
  )
}

export interface AccountHeroProps {
  account: PaperAccount
  history: readonly Position[]
  /** Above the big number: "Valor de tu cuenta". */
  label: string
  /** Extra figures after the usual ones (the portfolio of NEXUS…). */
  extra?: ReactNode
  /** Below everything (an honest line, a button…). */
  footer?: ReactNode
}

/** The value of the account in big, what it changed since it started, its cash, the open and closed result, the winners
 *  as «x de n» (no conclusion below 30) and the curve of its closed trades. */
export const AccountHero = memo(function AccountHero({ account: a, history, label, extra, footer }: AccountHeroProps) {
  const change = a.equity - a.initial_balance
  const pct = a.initial_balance > 0 ? (change / a.initial_balance) * 100 : null
  const tone = signTone(Math.abs(change) < 0.005 ? 0 : change)
  const curve = pnlCurve(history)
  const n = a.trades
  return (
    <section className={`dk-hero ${tone}`}>
      <div className="dk-hero-main">
        <span className="dk-hero-label">{label}</span>
        <div className="dk-hero-value mono">{fmtMoney(a.equity)}</div>
        <div className={`dk-hero-change ${tone}`}>
          {change >= 0 ? <ArrowUpRight size={16} /> : <ArrowDownRight size={16} />}
          <b>
            {fmtMoney(change, true)} ({fmtPct(pct)})
          </b>
          <span className="muted">desde que empezó con {fmtMoney(a.initial_balance)}</span>
        </div>
        <div className="dk-figs">
          <Figure label="Efectivo" value={fmtMoney(a.balance)} title="Dinero de la cuenta sin contar lo que ganan o pierden las posiciones abiertas" />
          <Figure
            label="En juego ahora"
            value={fmtMoney(a.unrealized_pnl, true)}
            tone={signTone(a.unrealized_pnl)}
            hint={plural(a.open_positions, 'abierta', 'abiertas')}
            title="Lo que ganan o pierden ahora las posiciones abiertas (cambia con el precio)"
          />
          <Figure label="Ya ganado o perdido" value={fmtMoney(a.realized_pnl, true)} tone={signTone(a.realized_pnl)} hint="en operaciones cerradas" />
          <Figure
            label="Ganadoras"
            value={`${fmtInt(a.wins)} de ${fmtInt(n)}`}
            hint={n <= 0 ? 'sin cerradas todavía' : n < MIN_EVIDENCE_N ? 'muy pocas para concluir' : undefined}
            title="Operaciones cerradas con beneficio, de todas las cerradas. No mide una ventaja: un porcentaje alto puede perder dinero y uno bajo ganarlo."
          />
          {extra}
        </div>
      </div>
      <div className="dk-hero-chart">
        <span className="dk-hero-label">Resultado de las operaciones cerradas</span>
        {curve.length >= 3 ? (
          <Sparkline values={curve} />
        ) : (
          <div className="dk-spark-empty">La curva aparece cuando cierre al menos dos operaciones.</div>
        )}
        <span className="tiny muted">
          {curve.length > 1 ? `Acumulado de ${plural(curve.length - 1, 'operación cerrada', 'operaciones cerradas')}: ` : ''}
          {curve.length > 1 && <b className={signTone(curve[curve.length - 1])}>{fmtMoney(curve[curve.length - 1], true)}</b>}
        </span>
      </div>
      {footer ? <div className="dk-hero-foot">{footer}</div> : null}
    </section>
  )
})

// ----------------------------------------------------------------------------- section tabs

export interface SectionTab<T extends string> {
  id: T
  label: string
  icon: ReactNode
  count?: number | null
}

export function SectionTabs<T extends string>({ tabs, value, onChange, label }: { tabs: SectionTab<T>[]; value: T; onChange: (id: T) => void; label: string }) {
  return (
    <nav className="dk-sections" role="tablist" aria-label={label}>
      {tabs.map((t) => (
        <button key={t.id} type="button" role="tab" aria-selected={t.id === value} className={`dk-section${t.id === value ? ' active' : ''}`} onClick={() => onChange(t.id)}>
          {t.icon}
          <span>{t.label}</span>
          {isNum(t.count) && t.count > 0 ? <span className="dk-count">{fmtInt(t.count)}</span> : null}
        </button>
      ))}
    </nav>
  )
}

// ----------------------------------------------------------------------------- activity feed

export interface ActivityItem {
  key: string
  /** unix ms */
  time: number
  kind: 'open' | 'close'
  title: string
  detail: string
  amount: number | null
  tone: Tone
  position: Position
}

function openText(p: Position): { title: string; detail: string } {
  const a = assetOf(p)
  const money = moneyOf(p)
  const amount = `${money.label.toLowerCase()} ${fmtMoney(money.value)}`
  if (p.hold) return { title: `Compró ${a.name} para la cartera`, detail: `${fmtQty(unitsOf(p))} a ${fmtPrice(p.entry_price)} · ${amount}` }
  if (p.instrument?.kind === 'option') return { title: `Compró un ${directionOf(p).label.toLowerCase()} ${a.name}`, detail: amount }
  const dir = p.side === 'short' ? 'Vendió en corto' : 'Compró'
  return { title: `${dir} ${a.name}`, detail: `${sizeOf(p, p.qty)} a ${fmtPrice(p.entry_price)} · ${amount}` }
}

/** What happened, newest first, in plain words: each opening (of the open and the closed trades) and each close. */
export function activityOf(open: readonly Position[], closed: readonly Position[], limit = 8): ActivityItem[] {
  const items: ActivityItem[] = []
  for (const p of [...open, ...closed]) {
    if (isNum(p.entry_time)) {
      const t = openText(p)
      items.push({ key: `o${p.id}`, time: p.entry_time * 1000, kind: 'open', ...t, amount: null, tone: directionOf(p).tone, position: p })
    }
  }
  for (const p of closed) {
    if (!isNum(p.exit_time)) continue
    const a = assetOf(p)
    const why = exitInfo(p).label
    items.push({
      key: `c${p.id}`,
      time: p.exit_time * 1000,
      kind: 'close',
      title: p.hold ? `Vendió ${a.name} de la cartera` : `Cerró ${a.name}`,
      detail: why,
      amount: isNum(p.pnl) ? p.pnl : null,
      tone: signTone(p.pnl),
      position: p,
    })
  }
  return items.sort((x, y) => y.time - x.time || (x.key < y.key ? 1 : -1)).slice(0, limit)
}

export const ActivityFeed = memo(function ActivityFeed({ items, onShow, empty }: { items: ActivityItem[]; onShow: (p: Position) => void; empty: ReactNode }) {
  if (items.length === 0) return <div className="dk-empty">{empty}</div>
  return (
    <ul className="dk-feed">
      {items.map((it) => {
        const icon =
          it.kind === 'open' ? (
            it.tone === 'neg' ? <ArrowDownRight size={14} /> : <ArrowUpRight size={14} />
          ) : it.tone === 'neg' ? (
            <CircleX size={14} />
          ) : (
            <CircleCheck size={14} />
          )
        return (
          <li key={it.key}>
            <button type="button" className="dk-feed-row" onClick={() => onShow(it.position)} title="Ver en el gráfico">
              <span className={`dk-feed-icon ${it.tone}`}>{icon}</span>
              <span className="dk-feed-text">
                <b>{it.title}</b>
                <span>{it.detail}</span>
              </span>
              <span className="dk-feed-side">
                {it.amount !== null ? <b className={signTone(it.amount)}>{fmtMoney(it.amount, true)}</b> : null}
                <span>{fmtAgo(it.time)}</span>
              </span>
            </button>
          </li>
        )
      })}
    </ul>
  )
})

// ----------------------------------------------------------------------------- a titled block (lighter than a Card)

export function Block({ title, sub, actions, children, className }: { title: ReactNode; sub?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`dk-block ${className ?? ''}`}>
      <header className="dk-block-head">
        <div className="col gap-0" style={{ minWidth: 0 }}>
          <h2>{title}</h2>
          {sub ? <span className="dk-block-sub">{sub}</span> : null}
        </div>
        {actions ? <div className="row gap-2">{actions}</div> : null}
      </header>
      {children}
    </section>
  )
}
