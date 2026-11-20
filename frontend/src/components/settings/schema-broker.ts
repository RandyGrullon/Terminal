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
  {
    id: 'broker_mt5',
    title: 'MetaTrader 5 (cualquier bróker)',
    icon: ArrowLeftRight,
    description:
      'Repite las posiciones de NEXUS automática en la cuenta que tengas abierta en MetaTrader 5 en esta PC, de cualquier bróker: abre MT5, inicia sesión en tu cuenta y activa «Trading algorítmico». NEXUS no guarda tu contraseña: usa la sesión del terminal. En una cuenta DEMO opera al activarlo. En una cuenta REAL no envía nada salvo que actives «Permitir cuenta real» y escribas el número de esa cuenta. Solo funciona en Windows con el terminal abierto (no en la nube). La validación de NEXUS no encontró evidencia de ventaja: en una cuenta real puede perder dinero.',
    fields: [
      {
        key: 'broker.mt5_enabled',
        label: 'Activar el espejo en MetaTrader 5',
        kind: 'bool',
        help: 'Cada 15 s compara las posiciones de NEXUS automática con tu cuenta de MT5 y envía las órdenes que faltan, en lotes y con el símbolo de tu bróker. Solo replica posiciones nuevas, abiertas con el espejo activado.',
      },
      {
        key: 'broker.mt5_account',
        label: 'Cuenta de NEXUS que copia',
        kind: 'select',
        options: [
          { value: '1k', label: 'La cuenta de 1k (lotes pensados para 1.000 $)' },
          { value: '10k', label: 'La cuenta de 10k' },
        ],
        help: 'Copia las operaciones de esa cuenta con su mismo tamaño en lotes. Si tu cuenta de MT5 tiene unos 1.000 $, la de 1k es la que corresponde: copiar la de 10k arriesgaría unas 10 veces más.',
      },
      {
        key: 'broker.mt5_max_lots',
        label: 'Tope de lotes por orden',
        kind: 'number',
        min: 0,
        max: 100,
        step: 0.01,
        unit: 'lotes',
        help: 'Ninguna orden pasa de este tamaño, en cualquier tipo de cuenta (0 = no envía nada). Si NEXUS abre más, el espejo abre solo el tope.',
      },
      {
        key: 'broker.mt5_symbols',
        label: 'Símbolos de tu bróker',
        kind: 'text',
        wide: true,
        placeholder: 'XAU=GOLD, BTC=BTCUSD.m',
        help: 'Solo si el espejo no encuentra un mercado por sí solo. Busca XAUUSD, GOLD, XAUUSD.m, XAUUSDm, BTCUSD, el ticker de la acción… Escribe «activo=nombre en tu MT5», separados por comas.',
      },
      {
        key: 'broker.mt5_path',
        label: 'Ruta del terminal (opcional)',
        kind: 'text',
        wide: true,
        placeholder: 'C:\\Program Files\\XM Global MT5\\terminal64.exe',
        help: 'Déjalo vacío para usar el MT5 que ya está abierto. Ponlo si tienes varios MT5 instalados (uno por bróker).',
      },
      {
        key: 'broker.mt5_allow_real',
        label: 'Permitir cuenta REAL (dinero real)',
        kind: 'bool',
        help: 'Apagado, una cuenta real nunca recibe órdenes. Encendido, solo la cuenta cuyo número escribas abajo. Es tu decisión: NEXUS no tiene una ventaja comprobada y puede perder todo lo que pongas. No es asesoría financiera.',
      },
      {
        key: 'broker.mt5_real_login',
        label: 'Número de la cuenta real autorizada',
        kind: 'text',
        inputMode: 'numeric',
        placeholder: '12345678',
        help: 'El número de cuenta (login) que ves en MT5. Si el terminal está en otra cuenta real, el espejo no envía nada.',
      },
    ],
  },
]
