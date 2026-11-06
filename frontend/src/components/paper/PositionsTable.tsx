// Open demo positions with their "hasta aquí" stop, TP progress, the gate verdict they were opened with and the live
// NEXUS advice (red when NEXUS recommends closing). Actions: close at market, open the chart.
import { memo } from 'react'
import { ChartCandlestick, Check, Info, OctagonAlert, ShieldCheck, TriangleAlert } from 'lucide-react'
import type { NexusWarning, Position } from '../../api/types'
import { fmtBarTime, fmtDuration, fmtPct, fmtPrice, fmtR, signTone, toneColor } from '../../lib/format'
import { Badge, Button, IconButton, VerdictBadge } from '../ui'
import { ORIGIN_LABEL, fmtMoney, fmtQty, isNum } from '../markets/opsUtils'
import { adviceLevel, contractsOf, contractsText, distPct, marketOf, stopProtects, tpHits } from './paperUtils'
import { useMediaQuery } from './useMediaQuery'

interface Props {
  positions: Position[]
  closingId: number | null
  onClose: (p: Position) => void
  onOpenChart: (p: Position) => void
  /** Extra origin labels (e.g. NEXUS's own account: autopilot -> "NEXUS"), over the shared ORIGIN_LABEL. */
  originLabels?: Record<string, string>
}

function WarningIcons({ warnings }: { warnings: NexusWarning[] }) {
  if (!warnings.length) return null
  return (
    <span className="pp-warns">
      {warnings.map((w, i) => {
        const Icon = w.severity === 'critical' ? OctagonAlert : w.severity === 'warn' ? TriangleAlert : Info
        const color = w.severity === 'critical' ? 'var(--neg)' : w.severity === 'warn' ? 'var(--warn)' : 'var(--info)'
        return (
          <span key={`${w.code}-${i}`} title={w.message || w.code} aria-label={w.message || w.code}>
            <Icon size={13} color={color} />
          </span>
        )
      })}
    </span>
  )
}

function TpChips({ p }: { p: Position }) {
  const hits = tpHits(p)
  const levels = [p.tp1, p.tp2, p.tp3]
  return (
    <div className="pp-tps">
      {levels.map((lvl, i) => (
        <span
          key={i}
          className={`pp-tp ${hits[i] ? 'hit' : ''}`}
          title={hits[i] ? `TP${i + 1} alcanzado` : `TP${i + 1} pendiente${isNum(lvl) && isNum(p.price) ? ` (${fmtPct(distPct(lvl, p.price))} desde el precio)` : ''}`}
        >
          <b>
            TP{i + 1}
            {hits[i] && <Check size={10} />}
          </b>
          {fmtPrice(lvl)}
        </span>
      ))}
    </div>
  )
}

function hasAdvice(p: Position): boolean {
  return !!(p.advice && p.advice.trim()) || (Array.isArray(p.warnings) && p.warnings.length > 0)
}

function Advice({ p, inline = false }: { p: Position; inline?: boolean }) {
  const level = adviceLevel(p)
  const warnings = Array.isArray(p.warnings) ? p.warnings : []
  if (level === 'none' && !warnings.length) return <span className="tiny muted">Sin aviso todavía</span>
  return (
    <div className={`pp-advice ${level === 'close' ? 'close' : ''} ${inline ? 'inline' : ''}`}>
      {inline && level !== 'close' && <span className="pp-advice-tag">NEXUS</span>}
      {level === 'close' && (
        <div className="pp-advice-head">
          <OctagonAlert size={12} /> NEXUS: cerrar
        </div>
      )}
      {p.advice && <div style={level === 'warn' ? { color: 'var(--warn)' } : undefined}>{p.advice}</div>}
      <WarningIcons warnings={warnings} />
    </div>
  )
}

const Row = memo(function Row(props: {
  p: Position
  closing: boolean
  busy: boolean
  /** true: advice in its own column; false: advice in a sub-row below the position (narrow screens). */
  inlineAdvice: boolean
  cols: number
  onClose: (p: Position) => void
  onOpenChart: (p: Position) => void
  originLabels?: Record<string, string>
}) {
  const { p, closing, busy, inlineAdvice, cols, onClose, onOpenChart, originLabels } = props
  const market = marketOf(p)
  const level = adviceLevel(p)
  const pnlTone = signTone(p.pnl)
  const slDist = distPct(p.sl, p.price)
  const held = isNum(p.entry_time) ? Math.max(0, Date.now() / 1000 - p.entry_time) : null
  const subRow = !inlineAdvice && hasAdvice(p)
  const rowCls = [level === 'close' ? 'pp-row-close' : '', subRow ? 'pp-has-sub' : ''].filter(Boolean).join(' ') || undefined
  return (
    <>
    <tr className={rowCls}>
      <td>
        <div className="pp-sym">
          <button type="button" onClick={() => onOpenChart(p)} title={`Abrir ${market.chart} en el gráfico`}>
            {market.ticker}
          </button>
          <span className="pp-sub">
            {market.sub} · {p.tf}
          </span>
        </div>
      </td>
      <td title="Cantidad abierta / cantidad inicial">
        <div className="pp-stack" style={{ alignItems: 'flex-start', gap: 2 }}>
          <Badge tone={market.sideTone}>{market.side}</Badge>
          <span className="pp-sub">
            {contractsText(p, p.qty) !== null ? `${contractsOf(p, p.qty_open)} de ${contractsText(p, p.qty)}` : `${fmtQty(p.qty_open)} de ${fmtQty(p.qty)}`}
          </span>
        </div>
      </td>
      <td className="num mono">
        <div className="pp-stack">
          <span>{fmtPrice(p.entry_price)}</span>
          <span className="pp-sub" title={fmtBarTime(p.entry_time, p.tf)}>
            hace {held !== null ? fmtDuration(held) : '—'}
          </span>
        </div>
      </td>
      <td className="num mono">{fmtPrice(p.price)}</td>
      <td className="num mono">
        <div className="pp-stack" style={{ color: toneColor(pnlTone) }}>
          <span className="bold">{fmtMoney(p.pnl, true)}</span>
          <span className="pp-sub" style={{ color: 'inherit' }}>
            {fmtPct(p.pnl_pct)}
          </span>
        </div>
      </td>
      <td className="num mono bold" style={{ color: toneColor(signTone(p.r_multiple)) }}>
        {fmtR(p.r_multiple)}
      </td>
      <td className="num mono">
        <div className="pp-stack">
          <span className="neg row gap-1 end">
            {stopProtects(p) && (
              <span title="El stop ya protege la entrada (punto de equilibrio o mejor)">
                <ShieldCheck size={12} className="pos" />
              </span>
            )}
            {fmtPrice(p.sl)}
          </span>
          <span className="pp-sub">{slDist !== null ? fmtPct(slDist) : '—'}</span>
          {isNum(p.trail) && (
            <span className="pp-sub warn" title="Stop dinámico (chandelier) activo tras TP2">
              dinámico {fmtPrice(p.trail)}
            </span>
          )}
        </div>
      </td>
      <td>
        <TpChips p={p} />
      </td>
      <td>
        <div className="pp-stack" style={{ alignItems: 'flex-start', gap: 2 }}>
          {p.gate_verdict ? <VerdictBadge verdict={p.gate_verdict} score={p.gate_score} /> : <span className="tiny muted">sin filtro</span>}
          <span className="pp-sub" style={{ color: p.origin === 'auto' ? 'var(--purple)' : undefined }}>
            {originLabels?.[p.origin] ?? ORIGIN_LABEL[p.origin] ?? p.origin}
          </span>
        </div>
      </td>
      {inlineAdvice && (
        <td>
          <Advice p={p} />
        </td>
      )}
      <td className="pp-sticky">
        <div className="pp-actions">
          <Button
            size="sm"
            variant={level === 'close' ? 'danger' : 'default'}
            loading={closing}
            disabled={busy}
            onClick={() => onClose(p)}
            title="Cerrar la posición a precio de mercado (simulado)"
          >
            Cerrar
          </Button>
          <IconButton icon={<ChartCandlestick size={14} />} title="Ver gráfico" size="sm" onClick={() => onOpenChart(p)} />
        </div>
      </td>
    </tr>
    {subRow && (
      <tr className={`pp-advice-row ${level === 'close' ? 'pp-row-close' : ''}`}>
        <td colSpan={cols}>
          <Advice p={p} inline />
        </td>
      </tr>
    )}
    </>
  )
})

function PositionsTable({ positions, closingId, onClose, onOpenChart, originLabels }: Props) {
  const inlineAdvice = useMediaQuery('(min-width: 1360px)')
  const cols = inlineAdvice ? 11 : 10
  return (
    <div className="table-wrap pp-table-wrap">
      <table className="table pp-table">
        <thead>
          <tr>
            <th>Símbolo</th>
            <th title="Lado y cantidad abierta / total">Lado · cant.</th>
            <th className="num">Entrada</th>
            <th className="num">Precio</th>
            <th className="num">P&amp;L</th>
            <th className="num">R</th>
            <th className="num" title="Stop loss actual («hasta aquí») y stop dinámico (trailing) tras TP2">
              SL · hasta aquí
            </th>
            <th title="Objetivos parciales (✓ = alcanzado)">TP1 · TP2 · TP3</th>
            <th title="Veredicto del filtro al abrir y origen de la orden">Filtro · origen</th>
            {inlineAdvice && <th title="Consejo en vivo de NEXUS para esta posición">NEXUS</th>}
            <th className="num pp-sticky">Acciones</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <Row
              key={p.id}
              p={p}
              closing={closingId === p.id}
              busy={closingId !== null}
              inlineAdvice={inlineAdvice}
              cols={cols}
              onClose={onClose}
              onOpenChart={onOpenChart}
              originLabels={originLabels}
            />
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default memo(PositionsTable)
