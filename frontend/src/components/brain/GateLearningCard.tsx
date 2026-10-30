// How good is the Trade Gate ("¿Tomo la operación?") at telling you when NOT to trade, measured on verified
// decisions: % of avoided trades that would have lost, and % of taken trades that won.
import { memo } from 'react'
import { CircleCheck, CircleX } from 'lucide-react'
import type { LearningSummary } from '../../api/types'
import { fmtInt, fmtNum } from '../../lib/format'
import { Meter } from '../ui'
import { fmtRate, ratePct, rateTone } from './learnFormat'

type Gate = LearningSummary['gate']

function GateLearningCard({ gate }: { gate: Gate | null | undefined }) {
  const decisions = gate?.decisions ?? 0
  const taken = gate?.taken ?? 0
  const avoided = gate?.avoided ?? 0
  const avoidedLose = ratePct(gate?.avoided_would_lose_rate)
  const takenWin = ratePct(gate?.taken_win_rate)

  if (!gate || decisions <= 0) {
    return (
      <div className="brain-gate-empty small muted">
        El filtro todavía no tiene decisiones registradas. Cada vez que NEXUS propone una entrada, el filtro decide TOMAR, ESPERAR o NO
        TOMAR; cuando pasa el horizonte se comprueba si acertó y aquí verás cuántas pérdidas te evitó.
      </div>
    )
  }

  // WAIT verdicts are neither taken nor avoided: they make up the rest of the decisions.
  const waited = Math.max(0, decisions - taken - avoided)
  const share = (n: number) => (decisions ? Math.min(100, (n / decisions) * 100) : 0)
  return (
    <div className="col gap-3">
      <div className="col gap-1">
        <div className="row between small wrap">
          <span>
            <strong>{fmtInt(decisions)}</strong> <span className="muted">decisiones registradas</span>
          </span>
          <span className="tiny muted">
            <span className="pos">{fmtInt(taken)} TOMAR</span> · <span className="warn">{fmtInt(waited)} ESPERAR</span> ·{' '}
            <span className="neg">{fmtInt(avoided)} NO TOMAR</span>
          </span>
        </div>
        <div
          className="brain-split"
          title={`TOMAR ${fmtNum(share(taken), 0)} % · ESPERAR ${fmtNum(share(waited), 0)} % · NO TOMAR ${fmtNum(share(avoided), 0)} %`}
        >
          <span className="pos-part" style={{ width: `${share(taken)}%` }} />
          <span className="wait-part" style={{ width: `${share(waited)}%` }} />
          <span className="neg-part" style={{ width: `${share(avoided)}%` }} />
        </div>
      </div>

      <div className="brain-gate-grid">
        <div className="brain-gate-box">
          <div className="row gap-1 small">
            <CircleX size={15} className="neg" /> Cuando dijo <strong className="neg">NO TOMAR</strong>
          </div>
          <div className={`huge ${avoidedLose === null ? 'muted' : avoidedLose >= 50 ? 'pos' : 'warn'}`}>{fmtRate(gate.avoided_would_lose_rate, 0)}</div>
          <span className="tiny muted">de esas operaciones habrían perdido (bien evitadas)</span>
          <Meter value={(avoidedLose ?? 0) / 100} tone={avoidedLose !== null && avoidedLose >= 50 ? 'pos' : 'warn'} />
        </div>
        <div className="brain-gate-box">
          <div className="row gap-1 small">
            <CircleCheck size={15} className="pos" /> Cuando dijo <strong className="pos">TOMAR</strong>
          </div>
          <div className={`huge ${rateTone(gate.taken_win_rate, 50, 40)}`}>{fmtRate(gate.taken_win_rate, 0)}</div>
          <span className="tiny muted">de las operaciones tomadas ganaron</span>
          <Meter value={(takenWin ?? 0) / 100} tone={rateTone(gate.taken_win_rate, 50, 40)} />
        </div>
      </div>
      {avoidedLose !== null && (
        <p className="small text-2 brain-p">
          De cada 10 operaciones que el filtro te dijo que no tomaras, unas <strong>{fmtNum(avoidedLose / 10, 1)}</strong> habrían terminado
          en pérdida. {avoidedLose >= 50 ? 'Hacerle caso te está ahorrando dinero.' : 'Por ahora su criterio no es mejor que el azar; sigue aprendiendo.'}
        </p>
      )}
    </div>
  )
}

export default memo(GateLearningCard)
