// Plain-language explanation of the feedback loop: predict -> wait -> verify -> learn.
import { memo } from 'react'
import { Brain, ChevronRight, Hourglass, NotebookPen, ShieldCheck, Target } from 'lucide-react'

const STEPS = [
  {
    icon: NotebookPen,
    title: '1 · Predice y lo anota',
    text: 'Cada señal de NEXUS, cada activo del escáner y cada decisión del filtro se guarda ANTES de saber qué pasará.',
  },
  {
    icon: Hourglass,
    title: '2 · Espera el horizonte',
    text: 'La predicción queda pendiente hasta su fecha objetivo (p. ej. 5 velas o 4 horas).',
  },
  {
    icon: Target,
    title: '3 · Verifica con el precio real',
    text: 'Al vencer, se compara con lo que realmente hizo el precio: acierto o fallo, sin trampas.',
  },
  {
    icon: Brain,
    title: '4 · Aprende',
    text: 'Los modelos ajustan sus pesos y su calibración con ese resultado; el filtro aprende qué comprobaciones evitan pérdidas.',
  },
]

function LoopExplainer() {
  return (
    <div className="brain-loop">
      <div className="brain-steps">
        {STEPS.map((s, i) => (
          <div key={s.title} className="brain-step-wrap">
            <div className="brain-step">
              <s.icon size={18} className="accent" />
              <strong>{s.title}</strong>
              <span>{s.text}</span>
            </div>
            {i < STEPS.length - 1 && <ChevronRight size={16} className="brain-step-arrow" />}
          </div>
        ))}
      </div>
      <div className="brain-honest">
        <ShieldCheck size={16} className="pos" />
        <span>
          Todo el acierto que ves aquí es <strong>fuera de muestra</strong>: se mide sobre predicciones hechas antes de conocer el
          resultado, nunca sobre datos que el modelo ya había visto. Un 50 % equivale a lanzar una moneda; por encima de 55 % empieza a
          haber ventaja real.
        </span>
      </div>
    </div>
  )
}

export default memo(LoopExplainer)
