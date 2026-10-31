// A demo order refused by a portfolio-risk limit (HTTP 409 code 'risk_limit'). Unlike a gate NO TOMAR, it cannot be
// forced: the limits are the user's own rules, changed only in Ajustes › Riesgo de cartera.
import { memo } from 'react'
import { Lock } from 'lucide-react'
import type { PaperRiskBlock } from '../../api/types'
import { Button, Modal } from '../ui'

const RULE_TITLE: Record<PaperRiskBlock['rule'], string> = {
  daily_loss: 'Límite de pérdida diaria alcanzado',
  open_risk: 'Riesgo abierto máximo',
  group: 'Demasiadas posiciones correlacionadas',
}

const RULE_HELP: Record<PaperRiskBlock['rule'], string> = {
  daily_loss:
    'La cuenta demo no abre operaciones nuevas (manuales ni del auto-trading) ni ejecuta órdenes pendientes hasta las 00:00 UTC: las que lleguen a su precio antes se cancelan. Cerrar posiciones sigue permitido.',
  open_risk:
    'El riesgo abierto suma lo que perderías si saltaran todos los stops desde la entrada. Reduce el riesgo % de la orden, cierra alguna posición o protégela con el stop en punto de equilibrio.',
  group:
    'Las posiciones de un mismo grupo (por ejemplo, varias criptomonedas) suelen ganar o perder a la vez: varias son casi la misma apuesta.',
}

function RiskBlockModal({ block, onClose }: { block: PaperRiskBlock | null; onClose: () => void }) {
  return (
    <Modal
      open={!!block}
      onClose={onClose}
      width={520}
      title={
        <span className="row gap-2">
          <Lock size={16} className="neg" /> {block ? RULE_TITLE[block.rule] : 'Límite de riesgo'}
        </span>
      }
      footer={<Button onClick={onClose}>Entendido</Button>}
    >
      {block && (
        <div className="col gap-2">
          <p className="pp-gate-summary">{block.message}</p>
          <p className="small text-2" style={{ margin: 0 }}>
            {RULE_HELP[block.rule]}
          </p>
          <p className="tiny muted" style={{ margin: 0 }}>
            Es un límite de la cuenta demo (operaciones simuladas) que no se puede forzar desde aquí; puedes cambiarlo en Ajustes › Riesgo de
            cartera.
          </p>
        </div>
      )}
    </Modal>
  )
}

export default memo(RiskBlockModal)
