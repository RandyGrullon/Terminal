import { ArrowLeftRight } from 'lucide-react'
import type { GroupDef } from './schema'

/** Settings group of the practice-broker mirror (appended to GROUPS by schema.ts). Defaults:
 * backend/app/settings_ext/broker.py. Alpaca PAPER only: the app has no setting for a live account. */
export const GROUPS_EXT: GroupDef[] = [
  {
    id: 'broker',
    title: 'Bróker de práctica (Alpaca)',
    icon: ArrowLeftRight,
    description:
      'Repite las posiciones de NEXUS automática en tu cuenta de práctica (paper trading) de Alpaca: acciones y ETF de EE. UU. y cripto en dólares. Las órdenes de práctica usan cotizaciones reales y nunca dinero real; NEXUS sigue decidiendo y simulando, y el espejo mide la diferencia de precio. Solo funciona con claves de una cuenta de práctica: la app no puede conectarse a una cuenta real.',
    fields: [
      {
        key: 'broker.alpaca_enabled',
        label: 'Activar el espejo en Alpaca',
        kind: 'bool',
        help: 'Cada 15 s compara las posiciones de NEXUS automática con tu cuenta de práctica y envía las órdenes que faltan. Solo replica posiciones nuevas, abiertas con el espejo activado.',
      },
      {
        key: 'broker.alpaca_key_id',
        label: 'API Key ID de la cuenta de práctica',
        kind: 'secret',
        link: { href: 'https://app.alpaca.markets/paper/dashboard/overview', label: 'Crear las claves de práctica' },
        help: 'En el panel de Alpaca, cambia a «Paper» y genera las claves de la API. Se guardan en tu base de datos y solo se envían a la API de práctica de Alpaca.',
      },
      {
        key: 'broker.alpaca_secret_key',
        label: 'Secret Key de la cuenta de práctica',
        kind: 'secret',
        help: 'Alpaca solo la muestra al crearla. Las claves de una cuenta real no funcionan aquí.',
      },
      {
        key: 'broker.mirror_stocks',
        label: 'Replicar acciones y ETF de EE. UU.',
        kind: 'bool',
        help: 'Solo con el mercado abierto (una orden a mercado con la bolsa cerrada se ejecutaría en la apertura, a otro precio). Los cortos, solo por acciones enteras.',
      },
      {
        key: 'broker.mirror_crypto',
        label: 'Replicar cripto en dólares',
        kind: 'bool',
        help: 'BTC, ETH, SOL y las demás que Alpaca tenga contra el dólar. Alpaca no admite cortos en cripto: esas posiciones no se replican.',
      },
    ],
  },
]
