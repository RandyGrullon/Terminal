import { Radio } from 'lucide-react'
import type { GroupDef } from './schema'

/** Settings groups of the realtime feature (appended to GROUPS by schema.ts). Defaults:
 *  backend/app/settings_ext/realtime.py (the free plans' limits). What each plan serves in real time comes from the
 *  providers' public documentation (backend app/data/realtime.py). */
export const GROUPS_EXT: GroupDef[] = [
  {
    id: 'realtime',
    title: 'Tiempo real',
    icon: Radio,
    description:
      'Precios en vivo de acciones y forex con tu clave de proveedor (Proveedores de datos). Twelve Data Basic (gratis): tiempo real para acciones y ETF de EE. UU., forex y cripto, con 8 créditos por minuto y 800 al día. Polygon: tiempo real solo en Stocks Advanced o Currencies Starter (su plan gratuito no tiene precios en vivo). Alpha Vantage gratis: solo fin de día. Sin clave, o si tu plan no lo incluye, se usa TradingView con el retraso que anuncia (p. ej. 15 min para NASDAQ/NYSE) y el gráfico lo indica.',
    fields: [
      {
        key: 'realtime.enabled',
        label: 'Usar el tiempo real de tus claves',
        kind: 'bool',
        help: 'Solo el gráfico abierto y las posiciones demo abiertas de acciones y forex consultan precios en vivo, para no gastar los créditos del plan. Cada precio se juzga por su propia marca de tiempo: si llega con retraso no se presenta como tiempo real.',
      },
      {
        key: 'realtime.twelvedata_credits_min',
        label: 'Créditos por minuto (Twelve Data)',
        kind: 'int',
        min: 1,
        max: 100000,
        step: 1,
        link: { href: 'https://twelvedata.com/pricing', label: 'Planes de Twelve Data' },
        help: 'Límite por minuto de tu plan: Basic (gratis) = 8. Cada cotización cuesta 1 crédito por símbolo; las descargas de velas usan los mismos créditos.',
      },
      {
        key: 'realtime.twelvedata_credits_day',
        label: 'Créditos por día (Twelve Data)',
        kind: 'int',
        min: 0,
        max: 100000000,
        step: 50,
        help: 'Límite diario de tu plan: Basic (gratis) = 800; 0 = sin límite diario (planes de pago). Los créditos del día se reparten entre las horas de mercado que quedan: con el plan gratuito y un solo símbolo de EE. UU., un precio cada ~40 s durante la sesión.',
      },
      {
        key: 'realtime.polygon_calls_min',
        label: 'Peticiones por minuto (Polygon)',
        kind: 'int',
        min: 1,
        max: 100000,
        step: 1,
        help: 'Límite de tu plan: Basic (gratis) = 5, aunque ese plan no incluye cotizaciones en vivo (se detecta y se sigue con TradingView).',
      },
      {
        key: 'realtime.reserve_pct',
        label: 'Reserva para velas',
        kind: 'number',
        min: 0,
        max: 90,
        step: 5,
        unit: '%',
        help: 'Parte de los límites por minuto y por día que nunca se usa para cotizaciones: queda para descargar las velas de tus gráficos y de NEXUS.',
      },
      {
        key: 'realtime.max_symbols',
        label: 'Símbolos en tiempo real a la vez',
        kind: 'int',
        min: 1,
        max: 50,
        step: 1,
        help: 'Primero las posiciones demo abiertas y después los gráficos abiertos; el resto sigue con TradingView.',
      },
      {
        key: 'realtime.min_interval_s',
        label: 'Intervalo mínimo por consulta',
        kind: 'seconds',
        min: 5,
        max: 3600,
        step: 5,
        unit: 's',
        help: 'Nunca se consulta un símbolo más a menudo que esto, aunque el plan lo permita.',
      },
    ],
  },
]
