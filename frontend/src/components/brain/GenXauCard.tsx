// «GenXAU — oro M30» on the Aprendizaje page, under the walk-forward validation: the verdict of an external research
// (a genetic generator of strategies in MetaTrader 5's tester, XAUUSD of XM in M30, 2019 → 2026) with its funnel, in
// the same honest tone as docs/VALIDATION.md. Static figures of a finished research (genxau-mt5/README.md,
// 2026-11-18): it is not re-run here. Its holdout year is spent: anything new is validated on future data.
import { FlaskConical } from 'lucide-react'
import { Badge, Card } from '../ui'
import './validation.css'

export interface FunnelStep {
  label: string
  value: string
}

export interface Finalist {
  id: string
  r: number // holdout result in R
  pf: number // holdout profit factor
}

export const GENXAU = {
  periods: [
    { label: 'Entrenamiento', value: '2019-01-01 → 2024-06-30' },
    { label: 'Forward', value: '2024-07-01 → 2025-11-16' },
    { label: 'Holdout', value: '2025-11-17 → 2026-11-18 (ya visto)' },
  ] as FunnelStep[],
  planA: [
    { label: 'Pasadas del algoritmo genético (3 corridas)', value: '44.592' },
    { label: 'Estrategias distintas', value: '11.409' },
    { label: 'Ideas distintas (la estructura sin sus números)', value: '2.418' },
    { label: 'Se veían bien en 2019-24 (PF ≥ 1,3, Calmar ≥ 1, ≥ 60 % de años positivos)', value: '516' },
    { label: 'Pasaron el Deflated Sharpe (mejor Sharpe por operación 0,28; el azar llega a 0,31)', value: '0' },
  ] as FunnelStep[],
  prediction: [
    { label: '¿2019-24 predijo el forward? (Spearman)', value: '−0,02' },
    { label: 'Las «buenas» de 2019-24 ganaron en el forward', value: '16 % de las veces' },
    { label: 'El resto ganó en el forward', value: '54 % de las veces' },
  ] as FunnelStep[],
  finalists: [
    { id: '#1', r: -11.5, pf: 0.46 },
    { id: '#5', r: -6.4, pf: 0.81 },
    { id: '#3', r: 1.9, pf: 1.06 },
    { id: '#4', r: 1.1, pf: 1.04 },
    { id: '#6', r: 0.3, pf: 1.01 },
  ] as Finalist[],
  planB: [
    { label: 'Estrategias distintas (indicadores clásicos fijos, rejilla gruesa, 3 corridas)', value: '5.242' },
    { label: 'Pasaron los filtros de 2019-24 (estabilidad por semestre)', value: '0' },
    { label: 'Mejor Sharpe por operación', value: '0,17' },
    { label: '¿2019-24 predijo el forward? (Spearman)', value: '+0,13' },
  ] as FunnelStep[],
  costs:
    'Costos medidos en los finalistas: el swap pesa entre el 0 y el 7 % del resultado neto (cierran casi todo en el día) y el spread ~0,02–0,03R por operación. Los costos no explican los fallos.',
  verdict:
    'Sin evidencia de ventaja para estrategias de indicadores de precio en XAU M30 con los costos de XM: 16.651 estrategias probadas, 0 que aguanten datos nuevos.',
  holdoutNote:
    'El año del holdout ya se miró: cualquier búsqueda nueva tiene que validarse con datos futuros (paper trading), no volviendo a mirar ese año.',
  brought:
    'De esta investigación, NEXUS tomó el Deflated Sharpe (laboratorio e investigación), el swap del oro, las franjas de sesión del gráfico y el script de estudio de #3 y #4 en PyChart.',
}

const fmtR = (r: number) => `${r > 0 ? '+' : r < 0 ? '−' : ''}${Math.abs(r).toLocaleString('es-ES', { minimumFractionDigits: 1, maximumFractionDigits: 1 })}R`
const fmtPf = (pf: number) => pf.toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

function Steps({ rows }: { rows: FunnelStep[] }) {
  return (
    <table className="val-funnel">
      <tbody>
        {rows.map((r) => (
          <tr key={r.label}>
            <th scope="row">{r.label}</th>
            <td className="mono">{r.value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function GenXauView() {
  const g = GENXAU
  return (
    <Card
      className="val-genxau"
      title={
        <span className="row gap-2">
          <FlaskConical size={15} /> GenXAU — oro M30
        </span>
      }
      sub="Investigación externa en el probador de MetaTrader 5 (XAUUSD de XM), terminada el 18/11/2026"
      actions={<Badge tone="warn">Sin evidencia de ventaja</Badge>}
    >
      <div className="val-genxau-body">
        <div className="val-verdict val-warn" role="note">
          <strong>Sin evidencia de ventaja</strong>
          <span>{g.verdict}</span>
        </div>
        <div className="val-genxau-periods tiny">
          {g.periods.map((p) => (
            <span key={p.label}>
              <b>{p.label}</b> {p.value}
            </span>
          ))}
        </div>
        <section>
          <h4>Plan A: búsqueda amplia</h4>
          <Steps rows={g.planA} />
          <Steps rows={g.prediction} />
        </section>
        <section>
          <h4>Los 5 finalistas en el holdout: los 5 fallan</h4>
          <p className="tiny muted" style={{ margin: '0 0 6px' }}>
            MACD/señal + tendencia H4 (EMA 50) + TP parcial, en el overlap Londres-NY. Con ticks reales (Monte Carlo, 2019 → 2025-11) tenían un PF de 1,16 a 1,40; en el
            holdout, ninguno mantuvo una ventaja.
          </p>
          <table className="val-funnel">
            <thead>
              <tr>
                <th>Finalista</th>
                <th>Holdout</th>
                <th>PF</th>
              </tr>
            </thead>
            <tbody>
              {g.finalists.map((f) => (
                <tr key={f.id}>
                  <th scope="row">{f.id}</th>
                  <td className={`mono ${f.r < 0 ? 'neg' : ''}`}>{fmtR(f.r)}</td>
                  <td className="mono">{fmtPf(f.pf)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section>
          <h4>Plan B: indicadores clásicos y estabilidad por semestre</h4>
          <Steps rows={g.planB} />
        </section>
        <p className="tiny" style={{ margin: 0 }}>{g.costs}</p>
        <p className="tiny muted" style={{ margin: 0 }}>{g.holdoutNote}</p>
        <p className="tiny muted" style={{ margin: 0 }}>{g.brought} Resultados simulados con datos históricos; no es asesoría financiera.</p>
      </div>
    </Card>
  )
}

export default function GenXauCard() {
  return <GenXauView />
}
