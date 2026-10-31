// "Habilidad frente a la referencia": the honest comparison of every group of predictions with the baseline — always
// the most frequent outcome of the same rows (three outcomes: 50 % is not chance), the chance level of the same mix
// of calls, the Brier score against climatology, calibration and the number of independent windows (see skill.ts).
import { memo } from 'react'
import { Scale } from 'lucide-react'
import { errorMessage } from '../../api/client'
import { fmtNum, fmtProb } from '../../lib/format'
import { plural } from '../../lib/plural'
import { Badge, Card, ErrorBox, Loading, Select } from '../ui'
import {
  SKILL_WINDOWS,
  VERDICT_SHORT,
  fmtBss,
  fmtCi,
  fmtOfN,
  fmtPp,
  periodLabel,
  skillTone,
  verdictBadgeTone,
  type SkillGroup,
  type SkillState,
  type SkillWindow,
} from './skill'

const DIRECTION_TEXT: Record<number, string> = { 1: 'alcista', [-1]: 'bajista', 0: 'neutral' }

function GroupRow({ g }: { g: SkillGroup }) {
  if (!g.n) {
    return (
      <tr>
        <td>{g.label}</td>
        <td colSpan={7} className="muted">
          Sin predicciones verificadas en este periodo
        </td>
      </tr>
    )
  }
  const tone = skillTone(g)
  return (
    <tr>
      <td>
        <div className="col gap-0">
          <span>{g.label}</span>
          {g.horizon && (
            <span className="tiny muted">
              horizonte {g.horizon}
              {g.window && g.window !== g.horizon ? ` · ventanas de ${g.window}` : ''}
            </span>
          )}
        </div>
      </td>
      <td className={`num mono ${tone === 'muted' ? '' : tone}`}>
        {fmtOfN(g.hits, g.n)}
        <div className="tiny muted">{fmtProb(g.accuracy, 1)}</div>
      </td>
      <td className="num mono" title="Acierto de decir siempre el resultado más frecuente del periodo (se conoce a posteriori)">
        {g.reference ? fmtProb(g.reference.accuracy, 1) : '—'}
        <div className="tiny muted">{g.reference?.label ?? ''}</div>
      </td>
      <td className="num mono" title="Acierto esperado con la misma mezcla de predicciones si no tuvieran relación con el resultado">
        {fmtProb(g.chance, 1)}
      </td>
      <td className={`num mono ${tone === 'muted' ? '' : tone}`} title="Acierto menos referencia; entre corchetes el intervalo del 95 %">
        {fmtPp(g.skill_pp)}
        {g.skill_ci && <div className="tiny muted">[{fmtCi(g.skill_ci)}]</div>}
      </td>
      <td className="num mono" title="Brier del modelo frente al de la climatología (decir siempre la frecuencia de subida del periodo)">
        {fmtNum(g.brier, 3)} / {fmtNum(g.brier_ref, 3)}
        <div className="tiny muted">habilidad {fmtBss(g.bss)}</div>
      </td>
      <td className="num mono" title="Error medio de calibración: distancia media entre la probabilidad dicha y la frecuencia observada">
        {g.ece_pp === null ? '—' : `${fmtNum(g.ece_pp, 1)} pp`}
      </td>
      <td className="num mono" title="Ventanas independientes del horizonte: las predicciones de una misma ventana se mueven juntas">
        {g.windows}
      </td>
    </tr>
  )
}

function Directions({ g }: { g: SkillGroup }) {
  const rows = g.by_direction.filter((d) => d.n > 0)
  if (!rows.length) return null
  return (
    <div className="brain-skill-dirs">
      {rows.map((d) => (
        <span key={d.direction} className="brain-skill-dir">
          Predicción {DIRECTION_TEXT[d.direction] ?? d.label}: <b className="mono">{fmtOfN(d.hits, d.n)}</b> ({fmtProb(d.accuracy, 1)})
          <span className="muted"> · ese resultado ocurrió en el {fmtProb(d.base_rate, 1)} de todas</span>
        </span>
      ))}
    </div>
  )
}

function SkillCard(props: { state: SkillState; range: SkillWindow; onRange: (w: SkillWindow) => void; onRetry?: () => void }) {
  const { state, range, onRange, onRetry } = props
  const report = state.report
  const groups = report?.groups ?? []
  const withData = groups.filter((g) => g.n > 0)
  return (
    <Card
      title={
        <>
          <Scale size={15} className="info" /> Habilidad frente a la referencia
        </>
      }
      sub="Hay tres resultados posibles (sube, baja o queda dentro de la banda neutral): el 50 % no es el azar."
      actions={<Select value={range} onChange={onRange} options={SKILL_WINDOWS} aria-label="Periodo del informe de habilidad" />}
    >
      {state.loading && !report && !state.unavailable ? (
        <Loading label="Calculando la habilidad frente a la referencia…" />
      ) : state.unavailable ? (
        <div className="muted small">
          Este servidor todavía no ofrece el informe de habilidad (/api/learning/skill). Mientras tanto, compara el acierto con la frecuencia
          real de cada resultado y no con un 50 %.
        </div>
      ) : state.error && !report ? (
        <ErrorBox error={`No se pudo calcular el informe: ${errorMessage(state.error)}`} onRetry={onRetry} />
      ) : !withData.length ? (
        <div className="muted small">Aún no hay predicciones verificadas en este periodo ({periodLabel(report?.days)}): no se puede medir la habilidad.</div>
      ) : (
        <div className="col gap-2">
          <div className="table-wrap">
            <table className="table brain-skill-table">
              <thead>
                <tr>
                  <th>Grupo</th>
                  <th className="num">Acierto</th>
                  <th className="num">Referencia</th>
                  <th className="num">Azar (misma mezcla)</th>
                  <th className="num">Diferencia</th>
                  <th className="num">Brier / climatología</th>
                  <th className="num">Calibración</th>
                  <th className="num">Ventanas</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((g) => (
                  <GroupRow key={g.id} g={g} />
                ))}
              </tbody>
            </table>
          </div>
          {withData.map((g) => (
            <div key={g.id} className="brain-skill-verdict">
              <div className="row gap-2 wrap">
                <Badge tone={verdictBadgeTone(g.verdict)}>{VERDICT_SHORT[g.verdict]}</Badge>
                <strong className="small">{g.label}</strong>
                <span className="tiny muted">
                  {plural(g.windows, 'ventana independiente', 'ventanas independientes')} · se necesitan {report?.min_windows ?? 30} para concluir
                </span>
              </div>
              <span className="small">{g.verdict_text}</span>
              {g.brier_text && <span className="tiny muted">{g.brier_text}</span>}
              {g.source === 'scanner' && <Directions g={g} />}
            </div>
          ))}
          <ul className="brain-skill-notes tiny muted">
            {(report?.notes ?? []).map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  )
}

export default memo(SkillCard)
