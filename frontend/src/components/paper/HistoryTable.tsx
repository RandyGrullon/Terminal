// Closed demo positions (exit reason, R, P&L) plus a breakdown of results by the gate verdict each trade had when it
// was opened, so the user can see what ignoring the filter costs.
import { memo, useMemo, useState } from 'react'
import type { Position } from '../../api/types'
import { fmtBarTime, fmtDuration, fmtPct, fmtPrice, fmtProb, fmtR, signTone, toneColor } from '../../lib/format'
import { plural } from '../../lib/plural'
import { Badge, Button, VerdictBadge } from '../ui'
import { ORIGIN_LABEL, fmtMoney, fmtQty, isNum } from '../markets/opsUtils'
import { contractsText, exitInfo, marketOf, verdictBreakdown } from './paperUtils'

const PAGE = 50

function Breakdown({ history }: { history: Position[] }) {
  const groups = useMemo(() => verdictBreakdown(history), [history])
  if (!groups.length) return null
  return (
    <div className="pp-breakdown" title="Resultado de las operaciones cerradas según el veredicto del filtro al abrirlas">
      {groups.map((g) => (
        <div key={g.verdict ?? 'none'} className="pp-bd">
          {g.verdict ? <VerdictBadge verdict={g.verdict} /> : <Badge tone="muted">Sin filtro</Badge>}
          <span className="text-2">
            {plural(g.n, 'operación', 'operaciones')} · {fmtProb(g.winRate)} ganadoras
          </span>
          <span className="mono">
            <b style={{ color: toneColor(signTone(g.pnl)) }}>{fmtMoney(g.pnl, true)}</b>
            <span className="muted"> · R medio </span>
            <b style={{ color: toneColor(signTone(g.avgR)) }}>{fmtR(g.avgR)}</b>
          </span>
        </div>
      ))}
    </div>
  )
}

/** `originLabels`: extra origin labels (e.g. NEXUS's own account: autopilot -> "NEXUS"), over the shared ORIGIN_LABEL. */
function HistoryTable({
  history,
  onOpenChart,
  originLabels,
}: {
  history: Position[]
  onOpenChart: (p: Position) => void
  originLabels?: Record<string, string>
}) {
  const [limit, setLimit] = useState(PAGE)
  const sorted = useMemo(
    () => [...history].sort((a, b) => (isNum(b.exit_time) ? b.exit_time : 0) - (isNum(a.exit_time) ? a.exit_time : 0) || b.id - a.id),
    [history],
  )
  const shown = sorted.slice(0, limit)
  return (
    <>
      <Breakdown history={history} />
      <div className="table-wrap pp-table-wrap">
        <table className="table pp-table">
          <thead>
            <tr>
              <th>Símbolo</th>
              <th>Lado</th>
              <th className="num">Cantidad</th>
              <th className="num">Entrada</th>
              <th className="num">Salida</th>
              <th>Motivo</th>
              <th className="num">R</th>
              <th className="num">P&amp;L</th>
              <th>Origen</th>
              <th title="Veredicto del filtro al abrir">Filtro</th>
              <th className="num">Duración</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((p) => {
              const market = marketOf(p)
              const ex = exitInfo(p)
              const dur = isNum(p.exit_time) && isNum(p.entry_time) ? Math.max(0, p.exit_time - p.entry_time) : null
              return (
                <tr key={p.id}>
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
                  <td>
                    <Badge tone={market.sideTone}>{market.side}</Badge>
                  </td>
                  <td className="num mono">{contractsText(p, p.qty) ?? fmtQty(p.qty)}</td>
                  <td className="num mono">
                    <div className="pp-stack">
                      <span>{fmtPrice(p.entry_price)}</span>
                      <span className="pp-sub">{fmtBarTime(p.entry_time, p.tf)}</span>
                    </div>
                  </td>
                  <td className="num mono">
                    <div className="pp-stack">
                      <span>{fmtPrice(p.exit_price)}</span>
                      <span className="pp-sub">{fmtBarTime(p.exit_time, p.tf)}</span>
                    </div>
                  </td>
                  <td>
                    <Badge tone={ex.tone}>{ex.label}</Badge>
                  </td>
                  <td className="num mono bold" style={{ color: toneColor(signTone(p.r_multiple)) }}>
                    {fmtR(p.r_multiple)}
                  </td>
                  <td className="num mono">
                    <div className="pp-stack" style={{ color: toneColor(signTone(p.pnl)) }}>
                      <span className="bold">{fmtMoney(p.pnl, true)}</span>
                      <span className="pp-sub" style={{ color: 'inherit' }}>
                        {fmtPct(p.pnl_pct)}
                      </span>
                    </div>
                  </td>
                  <td>
                    <Badge tone={p.origin === 'auto' ? 'purple' : p.origin === 'manual' ? 'muted' : 'info'}>{originLabels?.[p.origin] ?? ORIGIN_LABEL[p.origin] ?? p.origin}</Badge>
                  </td>
                  <td>{p.gate_verdict ? <VerdictBadge verdict={p.gate_verdict} score={p.gate_score} /> : <span className="tiny muted">—</span>}</td>
                  <td className="num mono text-2">{dur !== null ? fmtDuration(dur) : '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {sorted.length > limit && (
        <div className="row center" style={{ padding: 8 }}>
          <Button size="sm" variant="ghost" onClick={() => setLimit((l) => l + PAGE)}>
            Mostrar más ({plural(sorted.length - limit, 'restante', 'restantes')})
          </Button>
        </div>
      )}
    </>
  )
}

export default memo(HistoryTable)
