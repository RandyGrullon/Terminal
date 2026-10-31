import { ShieldAlert } from 'lucide-react'
import type { GroupDef } from './schema'

/** Settings groups of the paper-risk feature (appended to GROUPS by schema.ts). Defaults:
 *  backend/app/settings_ext/paper_risk.py. */
export const GROUPS_EXT: GroupDef[] = [
  {
    id: 'paper_risk',
    title: 'Riesgo de cartera',
    icon: ShieldAlert,
    description:
      'Límites de la cuenta demo (operaciones simuladas). Bloquean órdenes nuevas, manuales y del auto-trading, y la ejecución de órdenes pendientes; cerrar posiciones siempre está permitido. Un límite a 0 queda desactivado.',
    fields: [
      {
        key: 'paper.max_open_risk_pct',
        label: 'Riesgo abierto máximo',
        kind: 'number',
        min: 0,
        max: 100,
        step: 0.5,
        unit: '%',
        help: 'Suma de lo que perderías si saltaran todos los stops, medido desde la entrada y sin comisiones, en % del capital. Una orden que lo superaría se rechaza. 0 = sin límite.',
      },
      {
        key: 'paper.max_positions_per_group',
        label: 'Posiciones por grupo correlacionado',
        kind: 'int',
        min: 0,
        max: 50,
        step: 1,
        help: 'Máximo de posiciones abiertas a la vez en un mismo grupo: cada clase de activo (acciones, forex, índices…) es un grupo. 0 = sin límite.',
      },
      {
        key: 'paper.crypto_one_group',
        label: 'Cripto como un solo grupo',
        kind: 'bool',
        help: 'Las criptomonedas suelen moverse juntas, así que por defecto cuentan como un único grupo. Desactívalo para contar cada moneda por separado (BTC en cualquier par es la misma).',
      },
      {
        key: 'paper.daily_loss_limit_pct',
        label: 'Límite de pérdida diaria',
        kind: 'number',
        min: 0,
        max: 50,
        step: 0.5,
        unit: '%',
        help: 'Pérdida realizada en el día UTC (cierres y comisiones) en % del saldo con el que empezó el día. Al alcanzarla, la cuenta demo no abre operaciones nuevas ni ejecuta órdenes pendientes hasta las 00:00 UTC, aunque después se recupere (las órdenes pendientes que lleguen a su precio antes se cancelan). 0 = sin límite.',
      },
    ],
  },
]
