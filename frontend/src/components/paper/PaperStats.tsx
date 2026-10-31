// Account summary row of the demo account: balance, equity, return, realized / open P&L, trades and win rate.
import { memo } from 'react'
import type { PaperAccount } from '../../api/types'
import { fmtInt, fmtPct, signTone } from '../../lib/format'
import { plural } from '../../lib/plural'
import { Stat } from '../ui'
import { fmtMoney, fmtRate, rateTone } from '../markets/opsUtils'

function PaperStats({ account }: { account: PaperAccount }) {
  const a = account
  return (
    <div className="pp-stats">
      <Stat label="Balance" value={fmtMoney(a.balance)} hint={`Inicial ${fmtMoney(a.initial_balance)}`} title="Efectivo de la cuenta (sin contar posiciones abiertas)" />
      <Stat
        label="Equidad"
        value={fmtMoney(a.equity)}
        tone={signTone(a.equity - a.initial_balance)}
        hint="Balance + P&L abierto"
        title="Valor de la cuenta si se cerrara todo al precio actual"
      />
      <Stat label="Rendimiento" value={fmtPct(a.return_pct)} tone={signTone(a.return_pct)} hint="Sobre el capital inicial" />
      <Stat label="P&L realizado" value={fmtMoney(a.realized_pnl, true)} tone={signTone(a.realized_pnl)} hint="Operaciones cerradas y parciales" />
      <Stat
        label="P&L abierto"
        value={fmtMoney(a.unrealized_pnl, true)}
        tone={signTone(a.unrealized_pnl)}
        hint={plural(a.open_positions, 'posición abierta', 'posiciones abiertas')}
      />
      <Stat label="Operaciones" value={fmtInt(a.trades)} hint={`${plural(a.wins, 'ganada', 'ganadas')} · ${plural(a.losses, 'perdida', 'perdidas')}`} />
      <Stat
        label="% acierto"
        value={fmtRate(a.win_rate)}
        tone={rateTone(a.win_rate, 0.5, 0.35)}
        hint="Ganadas / cerradas"
        title="Porcentaje de operaciones cerradas con beneficio. Con objetivos de 1R o más, menos del 50 % puede seguir siendo rentable."
      />
    </div>
  )
}

export default memo(PaperStats)
