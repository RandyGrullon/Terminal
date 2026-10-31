// How good is the Trade Gate ("¿Tomo la operación?") at telling you when NOT to trade, measured on verified
// decisions: avoided trades that would not have reached TP1 before the stop (a loss or a time stop, not always a
// loss) and taken trades that reached it, always as "x de n" and compared with each other (no conclusion below 30
// verified cases or inside the 95 % margin of error).
import { memo } from 'react'
import { CircleCheck, CircleX } from 'lucide-react'
import type { LearningSummary } from '../../api/types'
import { fmtInt, fmtNum } from '../../lib/format'
import { plural, pluralWord } from '../../lib/plural'
import { Meter } from '../ui'
import { MIN_EVIDENCE_N, evidenceText, evidenceTone, filterEvidence, fmtOf } from './evidence'
import { fmtRate, prob01, rateTone } from './learnFormat'

type Gate = LearningSummary['gate']

const VERIFIED: [string, string] = ['verificada', 'verificadas']

function GateLearningCard({ gate }: { gate: Gate | null | undefined }) {
  const decisions = gate?.decisions ?? 0
  const taken = gate?.taken ?? 0
  const avoided = gate?.avoided ?? 0
  const avoidedLose = prob01(gate?.avoided_would_lose_rate)
  const takenWin = prob01(gate?.taken_win_rate)

  if (!gate || decisions <= 0) {
    return (
      <div className="brain-gate-empty small muted">
        El filtro todavía no tiene decisiones registradas. Cada vez que NEXUS propone una entrada, el filtro decide TOMAR, ESPERAR o NO
        TOMAR; cuando pasa el horizonte se comprueba cómo habría terminado cada decisión y aquí verás el resultado.
      </div>
    )
  }

  // Avoided (NO TOMAR) trades compared with the taken (TOMAR) ones, on the verified decisions only.
  const evidence = filterEvidence({
    total: avoided,
    resolved: gate.avoided_judged,
    winRate: avoidedLose === null ? null : 1 - avoidedLose,
    takenN: gate.taken_judged,
    takenWinRate: takenWin,
  })
  const avoidedTone = evidenceTone(evidence)
  const takenN = evidence.taken?.n ?? null
  const takenTone = takenN !== null && takenN >= MIN_EVIDENCE_N ? rateTone(gate.taken_win_rate, 50, 40) : 'muted'

  // WAIT verdicts are neither taken nor avoided: they make up the rest of the decisions.
  const waited = Math.max(0, decisions - taken - avoided)
  const share = (n: number) => (decisions ? Math.min(100, (n / decisions) * 100) : 0)
  return (
    <div className="col gap-3">
      <div className="col gap-1">
        <div className="row between small wrap">
          <span>
            <strong>{fmtInt(decisions)}</strong>{' '}
            <span className="muted">{pluralWord(decisions, 'decisión registrada', 'decisiones registradas')}</span>
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
          <div className={`huge ${avoidedTone}`}>{fmtRate(gate.avoided_would_lose_rate, 0)}</div>
          <span className="tiny muted" title="No habrían llegado a TP1 antes del stop: pérdida o cierre por tiempo (no siempre una pérdida)">
            {evidence.rejected
              ? `no habrían llegado a TP1 (${fmtOf(evidence.rejected, true, VERIFIED)})`
              : 'de las verificadas no habrían llegado a TP1'}
          </span>
          <Meter value={avoidedLose ?? 0} tone={avoidedTone} />
        </div>
        <div className="brain-gate-box">
          <div className="row gap-1 small">
            <CircleCheck size={15} className="pos" /> Cuando dijo <strong className="pos">TOMAR</strong>
          </div>
          <div className={`huge ${takenTone}`}>{fmtRate(gate.taken_win_rate, 0)}</div>
          <span className="tiny muted" title="Llegaron a TP1 antes del stop">
            {evidence.taken ? `ganaron (${fmtOf(evidence.taken, false, VERIFIED)})` : 'de las operaciones tomadas ganaron'}
          </span>
          <Meter value={takenWin ?? 0} tone={takenTone} />
        </div>
      </div>
      {(avoidedLose !== null || takenWin !== null) && <p className="small text-2 brain-p">{evidenceText(evidence, 'decisions')}</p>}
      {takenN !== null && takenN < MIN_EVIDENCE_N && (
        <p className="tiny muted brain-p">
          Con {plural(takenN, 'operación tomada verificada', 'operaciones tomadas verificadas')} el porcentaje de acierto todavía
          puede cambiar mucho.
        </p>
      )}
      {takenN === null && takenWin !== null && (
        <p className="tiny muted brain-p">
          El servidor no indica cuántas operaciones tomadas tienen resultado conocido: el porcentaje de acierto puede salir de
          muy pocos casos.
        </p>
      )}
    </div>
  )
}

export default memo(GateLearningCard)
