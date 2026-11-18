// «Cartera»: NEXUS automática's buy-and-hold portfolio (backend autopilot.hold_*), like a broker app: a bar with how the
// money is split, then a tile per asset of its basket with its target share and what it holds — value, gain, quantity
// and purchase price — or why it is still waiting (a stock buys with its market open). It holds until the user sells
// («Vender», at market, simulated) or its emergency stop is hit. Pure view: AutopilotView passes the holdings and its
// handlers.
import { ChartCandlestick, Clock, PieChart } from 'lucide-react'
import type { AutopilotHoldItem, Position } from '../../api/types'
import { fmtPct, fmtPrice, signTone } from '../../lib/format'
import { Badge, Button, IconButton } from '../ui'
import { fmtMoney, fmtQty } from '../markets/opsUtils'
import { AssetAvatar, Block, assetColor, assetKind, assetName } from './DemoKit'

export { assetKind, assetName }

export interface PortfolioRow {
  symbol: string
  weight: number | null // target %, null for a holding no longer in the basket
  position: Position | null
  value: number | null
  cost: number | null
  gain: number | null
}

/** One row per asset of the basket (held or waiting) plus any holding outside it; and the totals of what is held. */
export function portfolioRows(basket: AutopilotHoldItem[], holdings: Position[]): { rows: PortfolioRow[]; cost: number; value: number; gain: number } {
  const bySymbol = new Map(holdings.map((p) => [p.symbol.toUpperCase(), p]))
  const rows: PortfolioRow[] = []
  const row = (symbol: string, weight: number | null): PortfolioRow => {
    const p = bySymbol.get(symbol.toUpperCase()) ?? null
    const units = p ? (p.qty_open > 0 ? p.qty_open : p.qty) : 0
    const price = p ? (p.price ?? p.entry_price) : null
    return {
      symbol,
      weight,
      position: p,
      value: p && price !== null ? units * price : null,
      cost: p ? units * p.entry_price : null,
      gain: p ? p.pnl : null,
    }
  }
  for (const b of basket) rows.push(row(b.symbol, b.weight))
  for (const p of holdings) if (!basket.some((b) => b.symbol.toUpperCase() === p.symbol.toUpperCase())) rows.push(row(p.symbol, null))
  const held = rows.filter((r) => r.position)
  return {
    rows,
    cost: held.reduce((s, r) => s + (r.cost ?? 0), 0),
    value: held.reduce((s, r) => s + (r.value ?? 0), 0),
    gain: held.reduce((s, r) => s + (r.gain ?? 0), 0),
  }
}

export interface PortfolioCardProps {
  basket: AutopilotHoldItem[]
  holdPct: number
  stopPct: number
  holdings: Position[]
  closingId: number | null
  onSell: (p: Position) => void
  onShow: (p: Position) => void
}

function waitingText(off: boolean, kind: string): string {
  if (off) return 'Cartera apagada'
  return kind === 'Cripto'
    ? 'Pendiente: la compra en el próximo minuto'
    : 'Pendiente: la compra cuando abra la bolsa de EE. UU. (lun. a vie., 9:30–16:00 de Nueva York)'
}

export default function PortfolioCard({ basket, holdPct, stopPct, holdings, closingId, onSell, onShow }: PortfolioCardProps) {
  const { rows, cost, value, gain } = portfolioRows(basket, holdings)
  const off = holdPct <= 0
  const held = rows.filter((r) => r.position && (r.value ?? 0) > 0)
  return (
    <Block
      className="ap-portfolio"
      title={
        <span className="row gap-2">
          <PieChart size={16} /> Cartera · comprar y mantener <Badge tone={holdings.length ? 'accent' : 'muted'}>{holdings.length}</Badge>
        </span>
      }
      sub={
        off
          ? 'Apagada: NEXUS automática solo hace operaciones de corto plazo.'
          : `El ${fmtPct(holdPct, 0, false)} de la cuenta repartido en estos activos. Compra a precio de mercado (las acciones y ETF, con su bolsa abierta) y los mantiene, como en un bróker: solo vende si tú la vendes o si un activo cae un ${fmtPct(stopPct, 0, false)} desde su compra. Simulado: nunca dinero real.`
      }
    >
      {holdings.length > 0 && (
        <div className="dk-port-sum">
          <div className="dk-fig">
            <span className="dk-fig-label">Invertido</span>
            <b className="dk-fig-value">{fmtMoney(cost)}</b>
          </div>
          <div className="dk-fig">
            <span className="dk-fig-label">Vale ahora</span>
            <b className="dk-fig-value">{fmtMoney(value)}</b>
          </div>
          <div className="dk-fig">
            <span className="dk-fig-label">Ganancia</span>
            <b className={`dk-fig-value ${signTone(gain)}`}>
              {fmtMoney(gain, true)} <span className="dk-fig-hint">({fmtPct(cost > 0 ? (gain / cost) * 100 : null)})</span>
            </b>
          </div>
          {value > 0 && (
            <div className="dk-alloc" role="img" aria-label="Reparto de la cartera por valor">
              <div className="dk-alloc-bar">
                {held.map((r) => (
                  <span
                    key={r.symbol}
                    style={{ width: `${((r.value ?? 0) / value) * 100}%`, background: assetColor(assetName(r.symbol)) }}
                    title={`${assetName(r.symbol)}: ${fmtPct(((r.value ?? 0) / value) * 100, 1, false)}`}
                  />
                ))}
              </div>
              <div className="dk-alloc-legend">
                {held.map((r) => (
                  <span key={r.symbol}>
                    <i style={{ background: assetColor(assetName(r.symbol)) }} /> {assetName(r.symbol)} {fmtPct(((r.value ?? 0) / value) * 100, 0, false)}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
      {rows.length > 0 && (
        <div className="dk-port-grid">
          {rows.map((r) => {
            const p = r.position
            const kind = assetKind(r.symbol)
            const name = assetName(r.symbol)
            const pct = p && r.cost ? ((r.gain ?? 0) / r.cost) * 100 : null
            return (
              <article key={r.symbol} className={`dk-hold${p ? '' : ' waiting'}`}>
                <header className="dk-tile-head">
                  <AssetAvatar name={name} size={34} dim={!p} />
                  <div className="dk-tile-name">
                    <b title={r.symbol}>{name}</b>
                    <span>
                      {kind}
                      {r.weight !== null ? ` · objetivo ${fmtPct(r.weight, 0, false)}` : ''}
                    </span>
                  </div>
                  {p ? (
                    <div className="dk-hold-gain">
                      <b className={signTone(r.gain)}>{fmtMoney(r.gain, true)}</b>
                      <span className={signTone(r.gain)}>{fmtPct(pct)}</span>
                    </div>
                  ) : null}
                </header>
                {p ? (
                  <>
                    <div className="dk-hold-value">
                      <span className="dk-fig-label">Valor</span>
                      <b className="mono">{fmtMoney(r.value)}</b>
                    </div>
                    <div className="dk-hold-line">
                      {fmtQty(p.qty_open > 0 ? p.qty_open : p.qty)} · compra {fmtPrice(p.entry_price)} · ahora {fmtPrice(p.price)}
                    </div>
                    <footer className="dk-tile-foot">
                      <span />
                      <span className="row gap-1">
                        <IconButton icon={<ChartCandlestick size={14} />} title="Ver la compra en el gráfico" size="sm" onClick={() => onShow(p)} />
                        <Button size="sm" loading={closingId === p.id} disabled={closingId !== null} onClick={() => onSell(p)} title="Vender a precio de mercado (simulado)">
                          Vender
                        </Button>
                      </span>
                    </footer>
                  </>
                ) : (
                  <div className="dk-hold-wait">
                    <Clock size={13} /> {waitingText(off, kind)}
                  </div>
                )}
              </article>
            )
          })}
        </div>
      )}
      <div className="tiny muted">
        Comprar y mantener no garantiza ganancias: el valor sube y baja con el mercado. Datos simulados; no es asesoría financiera.
      </div>
    </Block>
  )
}
