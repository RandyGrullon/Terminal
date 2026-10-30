// Metrics grid of a backtest run.
import type { BacktestMetrics as Metrics } from '../../api/types'
import { fmtInt, fmtNum, fmtPct, fmtR, signTone, type Tone } from '../../lib/format'
import { Stat } from '../ui'
import { asFraction, ddValue, fmtDd, fmtRate } from './labels'

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function ratioTone(v: number | null | undefined, good: number, bad: number): Tone {
  if (!isNum(v)) return 'muted'
  return v >= good ? 'pos' : v <= bad ? 'neg' : 'warn'
}

export function BacktestMetrics(props: { metrics: Metrics }) {
  const m = props.metrics
  const win = asFraction(m.win_rate)
  const beatsHold = isNum(m.total_return_pct) && isNum(m.buy_hold_return_pct) ? m.total_return_pct - m.buy_hold_return_pct : null
  const dd = ddValue(m.max_dd_pct)

  return (
    <div className="col gap-2">
      <div className="sim-metrics">
        <Stat
          label="Rendimiento"
          value={fmtPct(m.total_return_pct)}
          tone={signTone(m.total_return_pct)}
          hint={`Capital final ${fmtNum(m.final_equity, 2)}`}
        />
        <Stat
          label="Buy & Hold"
          value={fmtPct(m.buy_hold_return_pct)}
          tone={signTone(m.buy_hold_return_pct)}
          hint={beatsHold === null ? undefined : beatsHold >= 0 ? `NEXUS lo supera por ${fmtPct(beatsHold, 1, false)}` : `Queda ${fmtPct(-beatsHold, 1, false)} por debajo`}
        />
        <Stat label="CAGR" value={fmtPct(m.cagr_pct)} tone={signTone(m.cagr_pct)} hint="Rendimiento anualizado" />
        <Stat label="Sharpe" value={fmtNum(m.sharpe, 2)} tone={ratioTone(m.sharpe, 1, 0)} hint="Retorno / volatilidad" />
        <Stat label="Sortino" value={fmtNum(m.sortino, 2)} tone={ratioTone(m.sortino, 1.5, 0)} hint="Retorno / volatilidad bajista" />
        <Stat label="Max DD" value={fmtDd(m.max_dd_pct)} tone={dd === null ? 'muted' : dd > -10 ? 'warn' : 'neg'} hint="Peor caída desde un máximo" />
        <Stat label="Profit factor" value={fmtNum(m.profit_factor, 2)} tone={ratioTone(m.profit_factor, 1.3, 1)} hint="Ganancias / pérdidas brutas" />
        <Stat
          label="% acierto"
          value={fmtRate(m.win_rate)}
          tone={win === null ? 'muted' : win >= 0.5 ? 'pos' : 'warn'}
          hint={`${fmtPct(m.avg_win_pct, 2)} medio ganador · ${fmtPct(m.avg_loss_pct, 2)} perdedor`}
        />
        <Stat label="Expectativa" value={fmtPct(m.expectancy_pct)} tone={signTone(m.expectancy_pct)} hint="Media por operación" />
        <Stat label="R medio" value={fmtR(m.avg_r)} tone={signTone(m.avg_r)} hint="Resultado medio en múltiplos del riesgo" />
        <Stat label="Operaciones" value={fmtInt(m.trades)} hint={`Racha perdedora máx. ${fmtInt(m.max_consecutive_losses)}`} />
        <Stat
          label="Exposición"
          value={fmtPct(m.exposure_pct, 1, false)}
          hint={isNum(m.avg_bars_in_trade) ? `${fmtNum(m.avg_bars_in_trade, 1)} velas por operación` : 'Tiempo dentro del mercado'}
        />
        <Stat label="Comisiones" value={fmtNum(m.fees_paid, 2)} hint="Comisiones + slippage pagados" />
      </div>
      <div className="sim-metrics-extra small">
        <span>
          <span className="muted">Mejor operación</span> <b className={signTone(m.best_trade_pct)}>{fmtPct(m.best_trade_pct)}</b>
        </span>
        <span>
          <span className="muted">Peor operación</span> <b className={signTone(m.worst_trade_pct)}>{fmtPct(m.worst_trade_pct)}</b>
        </span>
        <span>
          <span className="muted">Calmar</span> <b>{fmtNum(m.calmar, 2)}</b>
        </span>
        <span>
          <span className="muted">Capital inicial</span> <b>{fmtNum(m.initial_capital, 2)}</b>
        </span>
      </div>
    </div>
  )
}
