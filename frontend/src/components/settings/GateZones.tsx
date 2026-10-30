// Visual explanation of the gate thresholds: score 0..100 split into NO TOMAR / ESPERAR / TOMAR zones,
// updated live while the user edits the values.
import { memo } from 'react'

const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function GateZones({ take, wait, enabled }: { take: unknown; wait: unknown; enabled: boolean }) {
  const t = finite(take) ? Math.min(100, Math.max(0, take)) : 65
  const w = finite(wait) ? Math.min(t, Math.max(0, wait)) : Math.min(45, t)
  return (
    <div className={`set-zones ${enabled ? '' : 'off'}`}>
      <div className="set-zones-bar" role="img" aria-label={`NO TOMAR por debajo de ${w}, ESPERAR entre ${w} y ${t}, TOMAR desde ${t}`}>
        <span className="z-avoid" style={{ width: `${w}%` }}>
          {w >= 14 && 'NO TOMAR'}
        </span>
        <span className="z-wait" style={{ width: `${t - w}%` }}>
          {t - w >= 12 && 'ESPERAR'}
        </span>
        <span className="z-take" style={{ width: `${100 - t}%` }}>
          {100 - t >= 10 && 'TOMAR'}
        </span>
      </div>
      <div className="set-zones-scale">
        <span style={{ left: '0%' }}>0</span>
        <span style={{ left: `${w}%` }}>{Math.round(w)}</span>
        <span style={{ left: `${t}%` }}>{Math.round(t)}</span>
        <span style={{ left: '100%' }}>100</span>
      </div>
      <p className="tiny muted" style={{ margin: 0 }}>
        Puntuación del filtro (0–100). Un fallo crítico (R:R muy bajo, valor esperado negativo, vela de choque o mercado lateral y volátil)
        obliga a NO TOMAR aunque la puntuación sea alta.
        {!enabled && ' El filtro está desactivado: ahora mismo no se aplica.'}
      </p>
    </div>
  )
}

export default memo(GateZones)
