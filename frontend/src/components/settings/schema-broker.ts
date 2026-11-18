import { ArrowLeftRight } from 'lucide-react'
import type { GroupDef } from './schema'

/** Settings groups of the practice-broker mirrors (appended to GROUPS by schema.ts). Defaults:
 * backend/app/settings_ext/broker.py. Alpaca PAPER and Binance's testnets only: the app has no setting for a live
 * account (their URLs are constants in the backend). */
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
  {
    id: 'broker_binance',
    title: 'Testnet de Binance (práctica)',
    icon: ArrowLeftRight,
    description:
      'Repite las posiciones de cripto de NEXUS automática en los entornos de práctica de Binance, con fondos de prueba y nunca dinero real: la cripto al contado (solo compra y luego vende) en el testnet de contado, y los perpetuos (en largo y en corto, con el apalancamiento de NEXUS automática y margen aislado) en Demo Trading de futuros. Las direcciones de esos entornos son fijas: la app no puede enviar órdenes a una cuenta real. Su libro de órdenes es más fino que el real, así que sus precios pueden alejarse del mercado.',
    fields: [
      {
        key: 'broker.binance_enabled',
        label: 'Activar el espejo en el testnet de Binance',
        kind: 'bool',
        help: 'Cada 15 s compara las posiciones de NEXUS automática con tus cuentas de práctica de Binance y envía las órdenes que faltan. Solo replica posiciones nuevas, abiertas con el espejo activado.',
      },
      {
        key: 'broker.binance_spot_key',
        label: 'API Key del testnet de contado',
        kind: 'secret',
        link: { href: 'https://testnet.binance.vision/', label: 'Crear las claves del testnet de contado' },
        help: 'Entra con tu cuenta de GitHub y genera una clave HMAC. Se guarda en tu base de datos y solo se envía al testnet de contado.',
      },
      {
        key: 'broker.binance_spot_secret',
        label: 'Secret Key del testnet de contado',
        kind: 'secret',
        help: 'El testnet solo la muestra al crearla. Las claves de una cuenta real de Binance no funcionan aquí.',
      },
      {
        key: 'broker.binance_futures_key',
        label: 'API Key de Demo Trading (futuros)',
        kind: 'secret',
        link: { href: 'https://demo.binance.com/en/my/settings/api-management', label: 'Crear las claves en Demo Trading' },
        help: 'Binance pasó su testnet de futuros a Demo Trading: entra en demo.binance.com, abre «API Management» desde tu perfil y crea una clave con permiso de futuros. Es distinta de la del testnet de contado. Se guarda en tu base de datos y solo se envía a Demo Trading.',
      },
      {
        key: 'broker.binance_futures_secret',
        label: 'Secret Key de Demo Trading (futuros)',
        kind: 'secret',
        help: 'Sin las claves de un entorno, sus posiciones no se replican y el espejo dice por qué.',
      },
      {
        key: 'broker.binance_mirror_spot',
        label: 'Replicar cripto al contado',
        kind: 'bool',
        help: 'Pares en dólares (BTCUSDT, ETHUSDT…): compra cuando NEXUS compra y vende cuando NEXUS vende. Los cortos al contado no se replican: una billetera solo vende lo que tiene.',
      },
      {
        key: 'broker.binance_mirror_perps',
        label: 'Replicar perpetuos',
        kind: 'bool',
        help: 'Símbolos .P (BINANCE:BTCUSDT.P…), en largo y en corto, con el apalancamiento de NEXUS automática y margen aislado. Los cierres son órdenes «solo reducir».',
      },
    ],
  },
]
