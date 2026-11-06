import { BellRing } from 'lucide-react'
import type { FieldDef, GroupDef } from './schema'

/** Quiet-hours choices every 30 minutes ("00:00" … "23:30"). */
export const QUIET_TIMES = Array.from({ length: 48 }, (_, i) => {
  const v = `${String(Math.floor(i / 2)).padStart(2, '0')}:${i % 2 ? '30' : '00'}`
  return { value: v, label: v }
})

export const MIN_LEVEL_OPTIONS = [
  { value: 'info', label: 'Todos' },
  { value: 'warn', label: 'Importantes y críticos' },
  { value: 'critical', label: 'Solo críticos' },
]

export const SMTP_SECURITY_OPTIONS = [
  { value: 'starttls', label: 'STARTTLS (puerto 587)' },
  { value: 'ssl', label: 'SSL/TLS (puerto 465)' },
  { value: 'none', label: 'Sin cifrado (solo servidor local, sin contraseña)' },
]

/** Plain-text fields with their own validation rules (kind 'text'). */
const TEXT_KEYS = new Set([
  'alerts.telegram_chat_id',
  'alerts.smtp_host',
  'alerts.smtp_user',
  'alerts.email_from',
  'alerts.email_to',
])

const ADDR_RE = /^[^@\s<>(),;:"[\]\\]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$/
const TOKEN_RE = /^(bot)?\d{3,20}:[A-Za-z0-9_-]{20,100}$/i
const CHAT_RE = /^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$/
const HOST_RE = /^[A-Za-z0-9.-]+$/

/** "a@b.com" or "Nombre <a@b.com>". */
export function validAddress(text: string): boolean {
  const s = text.trim()
  const m = /^[^<>]*<([^<>]+)>$/.exec(s)
  return ADDR_RE.test(m ? m[1].trim() : s)
}

/** Client-side check of the alert fields with their own rules; undefined = not an alert field with special rules
 *  (use the generic validation). Every field is optional: the channel reports what is missing. */
export function validateAlertField(key: string, value: unknown): string | null | undefined {
  const isText = TEXT_KEYS.has(key) || key === 'alerts.telegram_token'
  if (!isText) return undefined
  if (typeof value !== 'string') return 'Valor no válido'
  const v = value.trim()
  if (!v) return null
  switch (key) {
    case 'alerts.telegram_token':
      return TOKEN_RE.test(v) ? null : 'No parece un token de Telegram (números:letras, como lo da @BotFather)'
    case 'alerts.telegram_chat_id':
      return CHAT_RE.test(v) ? null : 'Usa el número del chat (p. ej. 123456789 o -100…) o @canal'
    case 'alerts.smtp_host':
      return HOST_RE.test(v) ? null : 'Solo el nombre del servidor, p. ej. smtp.gmail.com'
    case 'alerts.smtp_user':
      return /\s/.test(v) ? 'El usuario no debe contener espacios' : null
    case 'alerts.email_from':
      return validAddress(v) ? null : 'Dirección no válida (p. ej. tu@correo.com o Nombre <tu@correo.com>)'
    case 'alerts.email_to': {
      const bad = v.split(/[,;\s]+/).filter((p) => p && !ADDR_RE.test(p))
      return bad.length ? `Dirección no válida: ${bad.slice(0, 2).join(', ')}` : null
    }
    default:
      return null
  }
}

const FIELDS: FieldDef[] = [
  {
    key: 'alerts.macro',
    label: 'Semáforo de mercado',
    kind: 'bool',
    help: 'Cambios del semáforo y avisos 60, 15 y 0 min antes de datos de alto impacto (FOMC, IPC, empleo…).',
  },
  {
    key: 'alerts.signals',
    label: 'Señales NEXUS',
    kind: 'bool',
    help: 'Entradas, salidas y salidas anticipadas de tu lista de seguimiento (temporalidad vigilada). Son señales del modelo, no órdenes ni asesoría.',
  },
  {
    key: 'alerts.paper',
    label: 'Cuenta demo',
    kind: 'bool',
    help: 'Aperturas, cierres y stops de la cuenta demo. Operaciones simuladas, sin dinero real.',
  },
  {
    key: 'alerts.autopilot',
    label: 'NEXUS automática',
    kind: 'bool',
    help: 'Aperturas, cierres y stops de la cuenta propia de NEXUS, que opera sola (Cuenta demo › NEXUS automática). Operaciones simuladas, sin dinero real. Desactivado por defecto.',
  },
  {
    key: 'alerts.min_level',
    label: 'Qué avisos enviar',
    kind: 'select',
    options: MIN_LEVEL_OPTIONS,
    help: 'Críticos: semáforo en rojo, salidas anticipadas y stops con pérdida. Importantes: semáforo en ámbar, entradas, salidas y operaciones demo. Todos: también la vuelta a verde y las tomas parciales.',
  },
  {
    key: 'alerts.max_per_hour',
    label: 'Máximo por hora',
    kind: 'int',
    min: 1,
    max: 200,
    step: 1,
    unit: 'mensajes',
    help: 'Los avisos que llegan a la vez van en un solo mensaje. Los críticos pueden superar el límite hasta un tope de seguridad de 60 por hora (o el doble del límite, si es mayor); de los omitidos se avisa en el siguiente mensaje.',
  },
  {
    key: 'alerts.signal_symbols',
    label: 'Señales solo de estos símbolos',
    kind: 'symbols',
    wide: true,
    help: 'Vacío = toda tu lista de seguimiento. Solo se vigilan en segundo plano los símbolos de la lista. EXCHANGE:TICKER (BINANCE:BTCUSDT) o solo el ticker (AAPL = en cualquier exchange).',
  },
  {
    key: 'alerts.quiet_enabled',
    label: 'Horas de silencio',
    kind: 'bool',
    help: 'En esa franja (hora de tu navegador) no se envía nada; al terminar, el siguiente mensaje dice cuántos se omitieron.',
  },
  { key: 'alerts.quiet_start', label: 'Silencio desde', kind: 'select', options: QUIET_TIMES, help: 'Inicio de las horas de silencio.' },
  { key: 'alerts.quiet_end', label: 'Silencio hasta', kind: 'select', options: QUIET_TIMES, help: 'Fin de las horas de silencio.' },
  {
    key: 'alerts.quiet_critical',
    label: 'Críticos durante el silencio',
    kind: 'bool',
    help: 'Deja pasar los avisos críticos (semáforo en rojo, stops con pérdida) también en las horas de silencio.',
  },
  {
    key: 'alerts.telegram_enabled',
    label: 'Telegram',
    kind: 'bool',
    wide: true,
    help: 'Envía los avisos a tu propio bot (API oficial api.telegram.org). Crea el bot con @BotFather y escríbele una vez para abrir el chat.',
  },
  {
    key: 'alerts.telegram_token',
    label: 'Token del bot',
    kind: 'secret',
    link: { href: 'https://core.telegram.org/bots/features#botfather', label: 'Cómo crear un bot con @BotFather' },
    help: 'Se guarda en tu base de datos. No aparece en el estado ni en los registros.',
  },
  {
    key: 'alerts.telegram_chat_id',
    label: 'ID del chat',
    kind: 'text',
    placeholder: 'p. ej. 123456789',
    // plain text keyboard: group ids start with '-' and channels with '@' (a numeric keypad on phones has neither)
    inputMode: 'text',
    help: 'Escribe a tu bot y abre api.telegram.org/bot<token>/getUpdates: es el número de "chat":{"id"…}. En grupos empieza por -100.',
  },
  {
    key: 'alerts.email_enabled',
    label: 'Correo electrónico',
    kind: 'bool',
    wide: true,
    help: 'Envía los avisos con tu servidor SMTP (Gmail, Outlook, tu proveedor). En Gmail y Outlook hace falta una contraseña de aplicación.',
  },
  {
    key: 'alerts.smtp_host',
    label: 'Servidor SMTP',
    kind: 'text',
    placeholder: 'p. ej. smtp.gmail.com',
    help: 'Nombre del servidor de salida de tu proveedor.',
  },
  {
    key: 'alerts.smtp_port',
    label: 'Puerto',
    kind: 'int',
    min: 1,
    max: 65535,
    step: 1,
    help: '587 con STARTTLS · 465 con SSL/TLS.',
  },
  {
    key: 'alerts.smtp_security',
    label: 'Seguridad',
    kind: 'select',
    options: SMTP_SECURITY_OPTIONS,
    help: 'Cifrado de la conexión (el certificado se comprueba con los del sistema). NEXUS nunca envía la contraseña sin cifrar.',
  },
  {
    key: 'alerts.smtp_user',
    label: 'Usuario',
    kind: 'text',
    placeholder: 'tu@correo.com',
    inputMode: 'email',
    help: 'Normalmente tu dirección de correo. Vacío si el servidor no pide autenticación.',
  },
  {
    key: 'alerts.smtp_password',
    label: 'Contraseña',
    kind: 'password',
    help: 'Contraseña o contraseña de aplicación. Se guarda en tu base de datos; no aparece en el estado ni en los registros.',
  },
  {
    key: 'alerts.email_from',
    label: 'Remitente',
    kind: 'text',
    placeholder: 'vacío = el usuario',
    inputMode: 'email',
    help: 'Dirección que envía los avisos (vacío = el usuario).',
  },
  {
    key: 'alerts.email_to',
    label: 'Destinatarios',
    kind: 'text',
    placeholder: 'tu@correo.com, otro@correo.com',
    inputMode: 'email',
    wide: true,
    help: 'Una o varias direcciones separadas por comas.',
  },
]

/** Settings groups of the alerts feature (appended to GROUPS by schema.ts). */
export const GROUPS_EXT: GroupDef[] = [
  {
    id: 'alerts',
    title: 'Alertas',
    icon: BellRing,
    description:
      'Avisos en tu móvil o tu correo aunque la pestaña esté cerrada (el servidor de NEXUS tiene que seguir en marcha): semáforo en rojo (FOMC, IPC…), entradas y salidas de NEXUS de tu lista de seguimiento y stops de la cuenta demo (simulada). No se envía nada hasta que actives un canal; solo se usan tu propio bot de Telegram o tu servidor de correo.',
    fields: FIELDS,
  },
]
