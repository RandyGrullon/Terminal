// Bottom drawer of the live simulator: open positions and recently closed trades of the "Simulación" account.
import { memo, useState } from 'react'
import { ChevronDown, ChevronUp, TriangleAlert } from 'lucide-react'
import type { Position } from '../../api/types'
import {
  DIRECTION_LABEL,
  EXIT_REASON_LABEL,
  fmtBarTime,
  fmtDuration,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtR,
  signTone,
} from '../../lib/format'
import { Badge, IconButton, Tabs, VerdictBadge } from '../ui'

type DrawerTab = 'open' | 'closed'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function SideBadge(props: { side: Position['side'] }) {
  return <Badge tone={props.side === 'long' ? 'pos' : 'neg'}>{DIRECTION_LABEL[props.side] ?? props.side}</Badge>
}

function GateCell(props: { p: Position }) {
  if (!props.p.gate_verdict) return <span className="muted">—</span>
  return (
    <span className="sim-mini-verdict">
      <VerdictBadge verdict={props.p.gate_verdict} score={props.p.gate_score} />
    </span>
  )
}

function OpenTable(props: { rows: Position[]; tf: string | null; autoTrade: boolean }) {
  if (!props.rows.length) {
    return (
      <div className="sim-table-empty">
        Sin posiciones abiertas.{' '}
        {props.autoTrade
          ? 'El auto-trading abrirá una operación cuando NEXUS dé señal de entrada y el filtro diga TOMAR.'
          : 'Activa «Auto-trading (solo TOMAR)» para que el simulador opere en la cuenta «Simulación».'}
      </div>
    )
  }
  const tf = props.tf ?? undefined
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Lado</th>
          <th>Entrada</th>
          <th className="num">Precio entrada</th>
          <th className="num">Precio actual</th>
          <th className="num">SL · hasta aquí</th>
          <th className="num">TP1</th>
          <th className="num">TP2</th>
          <th className="num">TP3</th>
          <th className="num">Cantidad</th>
          <th className="num">P&amp;L</th>
          <th className="num">P&amp;L %</th>
          <th className="num">R</th>
          <th>Filtro</th>
          <th>Consejo NEXUS</th>
        </tr>
      </thead>
      <tbody>
        {props.rows.map((p) => {
          const warn = Array.isArray(p.warnings) ? p.warnings.find((w) => w.severity !== 'info') : undefined
          return (
            <tr key={p.id}>
              <td>
                <SideBadge side={p.side} />
              </td>
              <td className="mono">{fmtBarTime(p.entry_time, tf)}</td>
              <td className="num mono">{fmtPrice(p.entry_price)}</td>
              <td className="num mono">{fmtPrice(p.price)}</td>
              <td className="num mono neg">{fmtPrice(p.sl)}</td>
              <td className="num mono pos">{fmtPrice(p.tp1)}</td>
              <td className="num mono pos">{fmtPrice(p.tp2)}</td>
              <td className="num mono pos">{fmtPrice(p.tp3)}</td>
              <td className="num mono">
                {fmtNum(p.qty_open, 4)}
                {isNum(p.qty) && isNum(p.qty_open) && p.qty_open < p.qty && <span className="muted"> / {fmtNum(p.qty, 4)}</span>}
              </td>
              <td className={`num mono ${signTone(p.pnl)}`}>{fmtNum(p.pnl, 2)}</td>
              <td className={`num mono ${signTone(p.pnl_pct)}`}>{fmtPct(p.pnl_pct)}</td>
              <td className={`num mono ${signTone(p.r_multiple)}`}>{fmtR(p.r_multiple)}</td>
              <td>
                <GateCell p={p} />
              </td>
              <td className="sim-advice-cell" title={p.advice ?? undefined}>
                {warn && <TriangleAlert size={12} className={warn.severity === 'critical' ? 'neg' : 'warn'} />} {p.advice ?? '—'}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function ClosedTable(props: { rows: Position[]; tf: string | null }) {
  if (!props.rows.length) return <div className="sim-table-empty">Aún no hay operaciones cerradas en esta sesión.</div>
  const tf = props.tf ?? undefined
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Salida</th>
          <th>Lado</th>
          <th className="num">Precio entrada</th>
          <th className="num">Precio salida</th>
          <th className="num">P&amp;L</th>
          <th className="num">P&amp;L %</th>
          <th className="num">R</th>
          <th>Motivo</th>
          <th>Filtro</th>
          <th className="num">Duración</th>
        </tr>
      </thead>
      <tbody>
        {props.rows.map((p) => {
          const reason = p.exit_label ?? (p.exit_reason ? EXIT_REASON_LABEL[p.exit_reason] ?? p.exit_reason : '—')
          const duration = isNum(p.exit_time) && isNum(p.entry_time) ? p.exit_time - p.entry_time : null
          return (
            <tr key={p.id}>
              <td className="mono">{fmtBarTime(p.exit_time, tf)}</td>
              <td>
                <SideBadge side={p.side} />
              </td>
              <td className="num mono">{fmtPrice(p.entry_price)}</td>
              <td className="num mono">{fmtPrice(p.exit_price)}</td>
              <td className={`num mono ${signTone(p.pnl)}`}>{fmtNum(p.pnl, 2)}</td>
              <td className={`num mono ${signTone(p.pnl_pct)}`}>{fmtPct(p.pnl_pct)}</td>
              <td className={`num mono ${signTone(p.r_multiple)}`}>{fmtR(p.r_multiple)}</td>
              <td>{reason}</td>
              <td>
                <GateCell p={p} />
              </td>
              <td className="num mono">{fmtDuration(duration)}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

export const SimPositions = memo(function SimPositions(props: {
  open: Position[]
  closed: Position[]
  tf: string | null
  autoTrade: boolean
}) {
  const [tab, setTab] = useState<DrawerTab>('open')
  const [collapsed, setCollapsed] = useState(false)
  const wins = props.closed.filter((p) => p.pnl > 0).length
  const totalPnl = props.closed.reduce((s, p) => s + (isNum(p.pnl) ? p.pnl : 0), 0)

  return (
    <div className={`sim-bottom ${collapsed ? 'collapsed' : ''}`}>
      <div className="sim-bottom-head">
        <Tabs<DrawerTab>
          pills
          value={tab}
          onChange={(t) => {
            setTab(t)
            setCollapsed(false)
          }}
          tabs={[
            { id: 'open', label: `Posiciones abiertas (${props.open.length})` },
            { id: 'closed', label: `Operaciones cerradas (${props.closed.length})` },
          ]}
        />
        <span className="grow" />
        {props.closed.length > 0 && (
          <span className="small muted nowrap">
            Ganadoras: {wins} de {props.closed.length} · P&amp;L <b className={signTone(totalPnl)}>{fmtNum(totalPnl, 2)}</b>
          </span>
        )}
        <IconButton
          size="sm"
          icon={collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          title={collapsed ? 'Mostrar operaciones' : 'Ocultar operaciones'}
          onClick={() => setCollapsed((v) => !v)}
        />
      </div>
      {!collapsed && (
        <div className="sim-bottom-body table-wrap">
          {tab === 'open' ? (
            <OpenTable rows={props.open} tf={props.tf} autoTrade={props.autoTrade} />
          ) : (
            <ClosedTable rows={props.closed} tf={props.tf} />
          )}
        </div>
      )}
    </div>
  )
})
