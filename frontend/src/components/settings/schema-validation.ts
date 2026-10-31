import { FlaskConical } from 'lucide-react'
import type { GroupDef } from './schema'

/** Timeframes offered for the walk-forward validation: the same choices as the «Opciones» form of the validation card
 *  (components/brain/ValidationCard.tsx). A saved timeframe outside this list is still shown by the field. */
export const VALIDATION_TF_OPTIONS = ['15m', '1h', '4h', '1d'].map((tf) => ({ value: tf, label: tf }))

/** Settings groups of the validation feature (appended to GROUPS by schema.ts). Defaults and limits:
 *  backend/app/settings_ext/validation.py and app/validation/runner.py (≤ 12 symbols, ≤ 24 series, 1000–5000 bars).
 *  The method parameters (validation.folds, warmup_bars, random_runs) are left out on purpose: they define how the
 *  edge is measured, not what is measured. */
export const GROUPS_EXT: GroupDef[] = [
  {
    id: 'validation',
    title: 'Validación',
    icon: FlaskConical,
    description:
      'Qué series mide por defecto la validación walk-forward (botón «Validar ahora» de Aprendizaje y comando validar): NEXUS + filtro frente a NEXUS sin filtro, entradas al azar y comprar y mantener, con las mismas reglas de riesgo y costes. El formulario «Opciones» de la tarjeta lo cambia para una sola ejecución. Es una medición del pasado, no una garantía.',
    fields: [
      {
        key: 'validation.symbols',
        label: 'Símbolos',
        kind: 'symbols',
        wide: true,
        help: 'Activos en formato EXCHANGE:TICKER (p. ej. BINANCE:BTCUSDT). Como máximo 12 símbolos y 24 series (símbolos × temporalidades). Vacío = BTC, ETH, SOL, XRP y BNB de Binance.',
      },
      {
        key: 'validation.tfs',
        label: 'Temporalidades',
        kind: 'multi',
        options: VALIDATION_TF_OPTIONS,
        help: 'Cada temporalidad es una serie más por símbolo (más descargas y más tiempo). Si no eliges ninguna se usan 1h y 4h.',
      },
      {
        key: 'validation.bars',
        label: 'Velas por serie',
        kind: 'int',
        min: 1000,
        max: 5000,
        step: 500,
        unit: 'velas',
        help: 'Velas cerradas que se descargan por serie. Con más velas hay más operaciones medidas; las primeras (calentamiento del modelo, 500 por defecto) no puntúan.',
      },
    ],
  },
]
