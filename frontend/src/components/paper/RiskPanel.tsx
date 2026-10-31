// Portfolio risk of the demo account: open risk against its cap, positions per correlated group, the realized P&L of
// the UTC day against the daily loss limit, and whether new orders / pending fills are blocked (and until when).
// Limits are set in Ajustes › Riesgo de cartera; a limit at 0 is off.
import { memo } from 'react'
import { Lock, ShieldCheck } from 'lucide-react'
import type { PaperRisk } from '../../api/types'
import { fmtPct, signTone, toneColor } from '../../lib/format'
import { Badge, Card, Meter } from '../ui'
import { fmtMoney, isNum } from '../markets/opsUtils'
import { dailyLossMoney, dailyUse, usageTone, usedFraction } from './ordersUtils'

function untilText(sec: number | null): string {
  if (!isNum(sec)) return ''
  const local = new Date(sec * 1000).toLocaleString('es-ES', { weekday: 'short', hour: '2-digit', minute: '2-digit' })
  return `hasta las 00:00 UTC (${local} hora local)`
}

function Row(props: { label: string; value: string; limit: string; frac: number | null; title?: string }) {
  const tone = usageTone(props.frac)
  return (
    <div className="pp-risk-row" title={props.title}>
      <div className="row between gap-2">
        <span className="label">{props.label}</span>
        <span className="mono small">
          <b style={{ color: props.frac !== null ? toneColor(tone) : undefined }}>{props.value}</b>
          <span className="muted"> / {props.limit}</span>
        </span>
      </div>
      {props.frac !== null ? <Meter value={props.frac} tone={tone} /> : <div className="tiny muted">Sin límite (0 en Ajustes)</div>}
    </div>
  )
}

function RiskPanel({ risk }: { risk: PaperRisk | null }) {
  if (!risk) return null
  const openFrac = usedFraction(risk.open_risk_pct, risk.max_open_risk_pct)
  const dayFrac = dailyUse(risk)
  return (
    <Card
      className={risk.blocked ? 'pp-risk blocked' : 'pp-risk'}
      title={
        <>
          {risk.blocked ? <Lock size={14} className="neg" /> : <ShieldCheck size={14} />} Riesgo de cartera
          {risk.blocked ? <Badge tone="neg">Bloqueada</Badge> : <Badge tone="muted">demo</Badge>}
        </>
      }
      sub="Límites de la cuenta demo para órdenes nuevas y órdenes pendientes"
    >
      <div className="col gap-2">
        {risk.blocked && (
          <div className="pp-risk-block small">
            <b>Bloqueada {untilText(risk.blocked_until)}.</b> {risk.block_reason}. Cerrar posiciones sigue permitido.
          </div>
        )}
        <Row
          label="Riesgo abierto"
          value={isNum(risk.open_risk_pct) ? fmtPct(risk.open_risk_pct, 2, false) : '—'}
          limit={isNum(risk.max_open_risk_pct) ? fmtPct(risk.max_open_risk_pct, 1, false) : 'sin límite'}
          frac={openFrac}
          title={`Lo que perderías si saltaran todos los stops, desde la entrada (sin comisiones): ${fmtMoney(risk.open_risk)}`}
        />
        <Row
          label="Pérdida realizada hoy (UTC)"
          value={fmtMoney(dailyLossMoney(risk))}
          limit={isNum(risk.daily_loss_limit) ? `${fmtMoney(-risk.daily_loss_limit)} (${fmtPct(risk.daily_loss_limit_pct, 1, false)})` : 'sin límite'}
          frac={dayFrac}
          title="Peor resultado realizado del día (cierres y comisiones) frente al límite: el bloqueo dura hasta las 00:00 UTC aunque luego se recupere"
        />
        <div className="row between small">
          <span className="muted">Resultado realizado hoy</span>
          <span className="mono" style={{ color: toneColor(signTone(risk.daily_pnl)) }}>
            {fmtMoney(risk.daily_pnl, true)}
            {isNum(risk.daily_pnl_pct) ? ` · ${fmtPct(risk.daily_pnl_pct, 2)}` : ''}
          </span>
        </div>
        {isNum(risk.unrealized_pnl) && risk.unrealized_pnl !== 0 && (
          <div className="row between small">
            <span className="muted" title="Las posiciones abiertas cuentan para el límite diario cuando se cierran">
              P&amp;L abierto (no cuenta hasta cerrar)
            </span>
            <span className="mono" style={{ color: toneColor(signTone(risk.unrealized_pnl)) }}>
              {fmtMoney(risk.unrealized_pnl, true)}
            </span>
          </div>
        )}
        <div className="col gap-1">
          <span className="label">
            Posiciones por grupo correlacionado
            {isNum(risk.max_positions_per_group) ? ` · máx. ${risk.max_positions_per_group}` : ' · sin límite'}
          </span>
          {risk.groups.length ? (
            <div className="pp-risk-groups">
              {risk.groups.map((g) => (
                <span key={g.group} className={`pp-risk-group ${g.full ? 'full' : ''}`} title={g.symbols.join(', ')}>
                  {g.label} <b className="mono">{g.positions}{isNum(g.max) ? `/${g.max}` : ''}</b>
                </span>
              ))}
            </div>
          ) : (
            <span className="tiny muted">Sin posiciones abiertas.</span>
          )}
          <span className="tiny muted">
            {risk.crypto_one_group ? 'Las criptomonedas cuentan como un solo grupo (suelen moverse juntas); ' : 'Cada criptomoneda cuenta por separado; '}
            el resto, un grupo por clase de activo.
          </span>
        </div>
        {risk.pending_orders > 0 && (
          <div className="tiny muted">
            {risk.pending_orders === 1 ? 'La orden pendiente se comprueba' : `Las ${risk.pending_orders} órdenes pendientes se comprueban`} otra vez con estos
            límites al ejecutarse.
          </div>
        )}
        <div className="tiny muted">{risk.note}</div>
      </div>
    </Card>
  )
}

export default memo(RiskPanel)
