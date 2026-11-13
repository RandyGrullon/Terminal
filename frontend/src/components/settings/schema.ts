// Settings form schema: every key of ARCHITECTURE §4 with a Spanish label, help text, control type and limits.
import { Brain, Eye, FlaskConical, HardDrive, Radar, Satellite, Scale, ShieldCheck, Wallet, type LucideIcon } from 'lucide-react'
import { GROUPS_EXT as AI_GROUPS } from './schema-ai'
import { GROUPS_EXT as ALERT_GROUPS } from './schema-alerts'
import { GROUPS_EXT as PAPER_RISK_GROUPS } from './schema-paper-risk'
import { GROUPS_EXT as REALTIME_GROUPS } from './schema-realtime'
import { GROUPS_EXT as VALIDATION_GROUPS } from './schema-validation'

export type FieldKind =
  | 'number' // float
  | 'int'
  | 'seconds' // int seconds, shown with a human-readable duration
  | 'bool'
  | 'tf' // timeframe select
  | 'select'
  | 'multi' // several values of `options` (chips); the value is a list
  | 'text' // plain text (server name, address, chat id…): no masking, spaces allowed
  | 'secret' // API key or token (password input with show / hide; no spaces inside)
  | 'password' // password (masked with show / hide like `secret`, but spaces are allowed: passphrases, app passwords)
  | 'markets' // multi-select of scanner markets
  | 'tp3' // three ascending R multiples
  | 'symbols' // list of symbols (tags)
  | 'fee_classes' // number per asset class (risk.fee_pct_by_class)

export interface FieldDef {
  key: string
  label: string
  help: string
  kind: FieldKind
  min?: number
  max?: number
  step?: number
  unit?: string
  /** Extra hint rendered from the current value, e.g. "= 52 %". */
  format?: 'percent01'
  options?: { value: string; label: string }[]
  link?: { href: string; label: string }
  wide?: boolean
  /** text / secret / password: example shown while the field is empty. */
  placeholder?: string
  /** text: the on-screen keyboard to offer on phones (never 'numeric' when a value can start with '-' or '@'). */
  inputMode?: 'text' | 'email' | 'url' | 'numeric'
}

export interface GroupDef {
  id: string
  title: string
  icon: LucideIcon
  description: string
  fields: FieldDef[]
}

export const CRYPTO_EXCHANGES = [
  { value: 'binance', label: 'Binance' },
  { value: 'okx', label: 'OKX' },
  { value: 'coinbase', label: 'Coinbase' },
  { value: 'kraken', label: 'Kraken' },
  { value: 'kucoin', label: 'KuCoin' },
]

/** Asset classes of risk.fee_pct_by_class (backend app.engine.costs.asset_class), in display order. */
export const FEE_CLASSES = [
  { id: 'forex', label: 'Forex' },
  { id: 'index', label: 'Índices' },
  { id: 'futures', label: 'Futuros' },
  { id: 'bond', label: 'Bonos' },
  { id: 'commodity', label: 'Materias primas' },
  { id: 'cfd', label: 'CFD' },
  { id: 'stock', label: 'Acciones' },
  { id: 'fund', label: 'Fondos y ETF' },
]
export const FEE_CLASS_LABEL: Record<string, string> = Object.fromEntries(FEE_CLASSES.map((c) => [c.id, c.label]))

/** Used when /api/markets is not reachable. */
export const FALLBACK_MARKETS = [
  { id: 'crypto', name: 'Cripto' },
  { id: 'america', name: 'Acciones EE. UU.' },
  { id: 'forex', name: 'Forex' },
  { id: 'futures', name: 'Futuros' },
  { id: 'cfd', name: 'CFD' },
  { id: 'bond', name: 'Bonos' },
]

const BASE_GROUPS: GroupDef[] = [
  {
    id: 'risk',
    title: 'Riesgo',
    icon: Scale,
    description: 'Cuánto arriesgas por operación y qué costes se descuentan en la cuenta demo, la simulación y los backtests.',
    fields: [
      {
        key: 'risk.account_size',
        label: 'Tamaño de la cuenta',
        kind: 'number',
        min: 100,
        max: 1e9,
        step: 100,
        unit: 'USD',
        help: 'Capital de referencia para calcular el tamaño de cada posición.',
      },
      {
        key: 'risk.per_trade_pct',
        label: 'Riesgo por operación',
        kind: 'number',
        min: 0.05,
        max: 10,
        step: 0.05,
        unit: '%',
        help: 'Lo que pierdes si salta el stop «hasta aquí». Lo prudente suele ser entre 0,5 % y 2 %.',
      },
      {
        key: 'risk.fee_pct',
        label: 'Comisión',
        kind: 'number',
        min: 0,
        max: 2,
        step: 0.01,
        unit: '%',
        help: 'Comisión descontada en cada entrada y en cada salida para cripto y para las clases de activo sin comisión propia.',
      },
      {
        key: 'risk.fee_pct_by_class',
        label: 'Comisión por clase de activo',
        kind: 'fee_classes',
        min: 0,
        max: 2,
        step: 0.005,
        unit: '%',
        wide: true,
        help: 'Comisión más diferencial típicos por lado según el tipo de activo; se usan en NEXUS, el filtro, los backtests y la cuenta demo. Son estimaciones: tu bróker real puede cobrar otra cosa.',
      },
      {
        key: 'risk.slippage_pct',
        label: 'Deslizamiento',
        kind: 'number',
        min: 0,
        max: 2,
        step: 0.01,
        unit: '%',
        help: 'Diferencia esperada entre el precio de la señal y el precio real de ejecución.',
      },
    ],
  },
  {
    id: 'nexus',
    title: 'NEXUS',
    icon: Brain,
    description: 'Motor de señales autoadaptativo: cuándo propone entrar o salir, dónde coloca el stop y los objetivos, y a qué velocidad aprende.',
    fields: [
      {
        key: 'nexus.entry_threshold',
        label: 'Umbral de entrada',
        kind: 'number',
        min: 0.05,
        max: 0.95,
        step: 0.01,
        help: 'Puntuación mínima (0–1) para proponer COMPRAR o VENDER. Más alto = menos señales, pero más claras.',
      },
      {
        key: 'nexus.exit_threshold',
        label: 'Umbral de salida',
        kind: 'number',
        min: -0.5,
        max: 0.5,
        step: 0.01,
        help: 'Si la puntuación cruza este nivel en contra de tu posición, NEXUS propone cerrar.',
      },
      {
        key: 'nexus.horizon',
        label: 'Horizonte de predicción',
        kind: 'int',
        min: 1,
        max: 100,
        step: 1,
        unit: 'velas',
        help: 'Cuántas velas hacia delante predice cada señal; también es cuando se verifica si acertó.',
      },
      {
        key: 'nexus.sl_atr',
        label: 'Stop loss',
        kind: 'number',
        min: 0.5,
        max: 10,
        step: 0.1,
        unit: '× ATR',
        help: 'Distancia mínima del stop «hasta aquí» en múltiplos del ATR (además respeta el último mínimo/máximo relevante).',
      },
      {
        key: 'nexus.tp_r',
        label: 'Objetivos TP1 · TP2 · TP3',
        kind: 'tp3',
        min: 0.1,
        max: 20,
        step: 0.1,
        unit: 'R',
        wide: true,
        help: 'Objetivos en múltiplos del riesgo (R). En TP1 se cierra 1/3 y el stop pasa a la entrada; en TP2 otro 1/3 y se activa el stop dinámico.',
      },
      {
        key: 'nexus.trail_atr',
        label: 'Stop dinámico',
        kind: 'number',
        min: 0.5,
        max: 10,
        step: 0.1,
        unit: '× ATR',
        help: 'Distancia del stop dinámico (chandelier) que protege las ganancias después de TP2.',
      },
      {
        key: 'nexus.learning_rate',
        label: 'Velocidad de aprendizaje',
        kind: 'number',
        min: 0.001,
        max: 0.5,
        step: 0.005,
        help: 'Cuánto cambia el modelo con cada resultado verificado. Alto = se adapta rápido, pero es más inestable.',
      },
      {
        key: 'nexus.allow_short',
        label: 'Permitir cortos',
        kind: 'bool',
        help: 'Si lo desactivas, NEXUS solo propone compras (largos) y nunca ventas en corto.',
      },
      {
        key: 'nexus.min_bars',
        label: 'Velas mínimas',
        kind: 'int',
        min: 50,
        max: 5000,
        step: 10,
        unit: 'velas',
        help: 'Historial mínimo para usar el motor completo; con menos, el activo recibe la lectura rápida del escáner.',
      },
      {
        key: 'nexus.default_limit',
        label: 'Velas a analizar',
        kind: 'int',
        min: 200,
        max: 10000,
        step: 100,
        unit: 'velas',
        help: 'Cuántas velas se cargan por defecto para el análisis y el gráfico.',
      },
      {
        key: 'nexus.time_stop_bars',
        label: 'Stop por tiempo',
        kind: 'int',
        min: 0,
        max: 2000,
        step: 1,
        unit: 'velas',
        help: 'Cierra la operación si no ha avanzado tras este número de velas (0 = desactivado).',
      },
    ],
  },
  {
    id: 'gate',
    title: 'Filtro «¿Tomo la operación?»',
    icon: ShieldCheck,
    description:
      'Antes de cada entrada revisa tendencia mayor, momento, espacio hasta el objetivo, riesgo/beneficio, volatilidad, volumen y la probabilidad aprendida, y responde TOMAR, ESPERAR o NO TOMAR.',
    fields: [
      {
        key: 'gate.enabled',
        label: 'Filtro activado',
        kind: 'bool',
        help: 'Si lo desactivas, NEXUS tomará todas las señales sin revisarlas (no recomendado).',
      },
      {
        key: 'gate.take_score',
        label: 'Puntuación para TOMAR',
        kind: 'number',
        min: 1,
        max: 100,
        step: 1,
        unit: '/100',
        help: 'Desde esta puntuación, y sin ningún fallo crítico, el veredicto es TOMAR.',
      },
      {
        key: 'gate.wait_score',
        label: 'Puntuación para ESPERAR',
        kind: 'number',
        min: 0,
        max: 99,
        step: 1,
        unit: '/100',
        help: 'Entre este valor y el de TOMAR el veredicto es ESPERAR confirmación; por debajo, NO TOMAR.',
      },
      {
        key: 'gate.min_rr',
        label: 'Riesgo/beneficio mínimo',
        kind: 'number',
        min: 0.5,
        max: 10,
        step: 0.1,
        unit: 'R',
        help: 'Beneficio hasta TP2 dividido entre el riesgo, ya con comisiones. Por debajo de este valor la comprobación falla.',
      },
      {
        key: 'gate.min_prob',
        label: 'Probabilidad mínima',
        kind: 'number',
        min: 0.4,
        max: 0.9,
        step: 0.01,
        format: 'percent01',
        help: 'Probabilidad aprendida de que el precio vaya a tu favor que se exige para aprobar (0,52 = 52 %).',
      },
    ],
  },
  {
    id: 'watch',
    title: 'Vigilancia',
    icon: Eye,
    description: 'Recalcula NEXUS en cada cierre de vela de tu lista de seguimiento y avisa de entradas, salidas y salidas anticipadas.',
    fields: [
      { key: 'watch.enabled', label: 'Vigilancia activada', kind: 'bool', help: 'Avisos en el terminal y notificaciones cuando cambia la señal.' },
      { key: 'watch.tf', label: 'Temporalidad vigilada', kind: 'tf', help: 'Temporalidad en la que se vigila la lista de seguimiento.' },
    ],
  },
  {
    id: 'scanner',
    title: 'Escáner',
    icon: Radar,
    description: 'Recorre mercados completos de TradingView, registra una predicción por activo y la verifica después para aprender.',
    fields: [
      { key: 'scanner.enabled', label: 'Escaneo automático', kind: 'bool', help: 'Repite el escaneo en segundo plano cada intervalo.' },
      {
        key: 'scanner.interval_s',
        label: 'Intervalo',
        kind: 'seconds',
        min: 60,
        max: 86400,
        step: 60,
        unit: 's',
        help: 'Cada cuánto se repite el escaneo automático.',
      },
      {
        key: 'scanner.markets',
        label: 'Mercados',
        kind: 'markets',
        wide: true,
        help: 'Mercados que recorre el escaneo automático (cuantos más, más predicciones para aprender y más tiempo por ciclo).',
      },
      { key: 'scanner.tf', label: 'Temporalidad', kind: 'tf', help: 'Temporalidad de los indicadores que usa el escáner.' },
      {
        key: 'scanner.limit',
        label: 'Activos por mercado',
        kind: 'int',
        min: 10,
        max: 5000,
        step: 10,
        help: 'Máximo de activos analizados por mercado, ordenados por volumen y liquidez.',
      },
      {
        key: 'scanner.deep_top',
        label: 'Análisis profundo (top N)',
        kind: 'int',
        min: 0,
        max: 100,
        step: 1,
        help: 'Los N mejores candidatos pasan por NEXUS completo y el filtro (más lento, más preciso).',
      },
      {
        key: 'scanner.horizon_s',
        label: 'Horizonte de verificación',
        kind: 'seconds',
        min: 300,
        max: 2592000,
        step: 300,
        unit: 's',
        help: 'Tiempo tras el cual se comprueba si cada predicción del escáner acertó.',
      },
      {
        key: 'scanner.replay_days',
        label: 'Días de vectores para la repetición',
        kind: 'int',
        min: 0,
        max: 30,
        step: 1,
        unit: 'días',
        help: 'Cuántos días se guardan las características de cada predicción del escáner ya resuelta para medir el modelo con «skill replay». 0 = no guardarlas. Ocupa ≈ 3 MB por día con el escaneo por defecto. Con menos de 5 días (30 ventanas de 4 h con el horizonte por defecto) la repetición no puede llegar a una conclusión.',
      },
    ],
  },
  {
    id: 'recorder',
    title: 'Grabador local',
    icon: HardDrive,
    description: 'Construye historial OHLCV propio a partir de las cotizaciones en vivo, para activos que no tienen historial público.',
    fields: [
      { key: 'recorder.enabled', label: 'Grabador activado', kind: 'bool', help: 'Guarda velas propias de los activos que sigues.' },
      {
        key: 'recorder.interval_s',
        label: 'Intervalo de captura',
        kind: 'seconds',
        min: 5,
        max: 3600,
        step: 5,
        unit: 's',
        help: 'Cada cuánto se toma una cotización para construir las velas.',
      },
    ],
  },
  {
    id: 'providers',
    title: 'Proveedores de datos',
    icon: Satellite,
    description:
      'Solo fuentes oficiales y públicas. Las claves son opcionales, amplían el historial de acciones y forex, y se guardan en tu propia base de datos: SQLite en tu equipo o, en modo nube, tu Postgres de Supabase.',
    fields: [
      {
        key: 'providers.tradingview_enabled',
        label: 'TradingView (datos públicos)',
        kind: 'bool',
        help: 'Búsqueda de activos, cotizaciones y escáner mediante los endpoints públicos de TradingView.',
      },
      {
        key: 'providers.crypto_default',
        label: 'Exchange cripto por defecto',
        kind: 'select',
        options: CRYPTO_EXCHANGES,
        help: 'Exchange que se usa cuando escribes una cripto sin prefijo (p. ej. «btc»).',
      },
      {
        key: 'providers.twelvedata_key',
        label: 'Clave de Twelve Data',
        kind: 'secret',
        link: { href: 'https://twelvedata.com/pricing', label: 'Obtener clave gratuita' },
        help: 'Historial de acciones, forex e índices (plan gratuito con límite diario de peticiones).',
      },
      {
        key: 'providers.alphavantage_key',
        label: 'Clave de Alpha Vantage',
        kind: 'secret',
        link: { href: 'https://www.alphavantage.co/support/#api-key', label: 'Obtener clave gratuita' },
        help: 'Historial diario e intradía de acciones y forex (plan gratuito limitado).',
      },
      {
        key: 'providers.polygon_key',
        label: 'Clave de Polygon',
        kind: 'secret',
        link: { href: 'https://polygon.io/', label: 'Obtener clave gratuita' },
        help: 'Historial de acciones de EE. UU. (el plan gratuito tiene datos con retraso).',
      },
    ],
  },
  {
    id: 'paper',
    title: 'Cuenta demo',
    icon: Wallet,
    description: 'Operativa simulada (papel). NEXUS nunca envía órdenes a un bróker real.',
    fields: [
      {
        key: 'paper.auto_trade',
        label: 'Operar automáticamente',
        kind: 'bool',
        help: 'Abre posiciones en la cuenta demo solo cuando el filtro dice TOMAR.',
      },
      { key: 'paper.auto_tf', label: 'Temporalidad automática', kind: 'tf', help: 'Temporalidad de las señales del modo automático.' },
      {
        key: 'paper.auto_symbols',
        label: 'Símbolos del modo automático',
        kind: 'symbols',
        wide: true,
        help: 'Activos que vigila el modo automático, en formato EXCHANGE:TICKER (p. ej. BINANCE:BTCUSDT).',
      },
      {
        key: 'paper.partial_tp',
        label: 'Tomas parciales',
        kind: 'bool',
        help: 'Cierra la posición por tercios en TP1, TP2 y TP3 en lugar de todo de una vez.',
      },
    ],
  },
  {
    id: 'sim',
    title: 'Simulador',
    icon: FlaskConical,
    description: 'Mercado sintético y reproducción de historial real vela a vela.',
    fields: [
      {
        key: 'sim.default_speed',
        label: 'Velocidad por defecto',
        kind: 'number',
        min: 0.5,
        max: 20,
        step: 0.5,
        unit: 'velas/s',
        help: 'Velas que avanza el simulador por segundo al iniciar una sesión.',
      },
    ],
  },
]

/** Every settings group: the base ones plus those contributed by feature modules (alerts, paper risk, data,
 *  validation). */
export const GROUPS: GroupDef[] = [...BASE_GROUPS, ...ALERT_GROUPS, ...PAPER_RISK_GROUPS, ...REALTIME_GROUPS, ...AI_GROUPS, ...VALIDATION_GROUPS]

export const FIELD_BY_KEY: Record<string, FieldDef> = Object.fromEntries(GROUPS.flatMap((g) => g.fields.map((f) => [f.key, f])))
