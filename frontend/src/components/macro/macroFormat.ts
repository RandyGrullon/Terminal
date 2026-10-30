// Spanish formatting for the market-conditions widgets: countdowns, local event times and compact event names.
import type { MacroEvent, MarketConditions } from '../../api/types'
import type { Tone } from '../../lib/format'
import { blackoutActive, pickNextEvent } from './macroModel'

const FLAGS: Record<string, string> = {
  USD: '🇺🇸',
  EUR: '🇪🇺',
  GBP: '🇬🇧',
  JPY: '🇯🇵',
  CAD: '🇨🇦',
  AUD: '🇦🇺',
  NZD: '🇳🇿',
  CHF: '🇨🇭',
  CNY: '🇨🇳',
}

export function currencyFlag(currency: string): string {
  return FLAGS[currency.toUpperCase()] ?? ''
}

/** Compact countdown for pills and badges: "45 s", "24 min", "3 h 5 min", "17 h", "2 d 4 h", "39 d". */
export function fmtCountdownShort(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return 'ahora'
  if (sec < 60) return `${Math.ceil(sec)} s`
  if (sec < 3600) return `${Math.ceil(sec / 60)} min`
  if (sec < 86400) {
    const h = Math.floor(sec / 3600)
    const m = Math.floor((sec % 3600) / 60)
    return h < 10 && m > 0 ? `${h} h ${m} min` : `${h} h`
  }
  if (sec < 3 * 86400) {
    const d = Math.floor(sec / 86400)
    const h = Math.floor((sec % 86400) / 3600)
    return h > 0 ? `${d} d ${h} h` : `${d} d`
  }
  return `${Math.round(sec / 86400)} d`
}

/** Relative countdown for lists: "en 24 min", "en 4 min 32 s", "ahora", "hace 12 min". */
export function fmtCountdown(sec: number): string {
  if (!Number.isFinite(sec)) return '—'
  if (Math.abs(sec) < 60) return sec >= 1 ? `en ${Math.ceil(sec)} s` : 'ahora'
  if (sec < 0) return `hace ${fmtCountdownShort(-sec)}`
  if (sec < 600) {
    const m = Math.floor(sec / 60)
    const s = Math.floor(sec % 60)
    return s > 0 ? `en ${m} min ${s} s` : `en ${m} min`
  }
  return `en ${fmtCountdownShort(sec)}`
}

/** Urgency tone of an event countdown: < 30 min red, < 2 h amber, otherwise neutral. */
export function countdownTone(sec: number): Tone {
  if (sec < -60) return 'muted'
  if (sec < 30 * 60) return 'neg'
  if (sec < 2 * 3600) return 'warn'
  return 'muted'
}

const capitalize = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s)

function dayDiff(ms: number, nowMs: number): number {
  const a = new Date(ms)
  const b = new Date(nowMs)
  const da = new Date(a.getFullYear(), a.getMonth(), a.getDate()).getTime()
  const db = new Date(b.getFullYear(), b.getMonth(), b.getDate()).getTime()
  return Math.round((da - db) / 86400000)
}

/** unix ms -> "14:30" (local time). */
export function fmtHM(ms: number): string {
  return new Date(ms).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })
}

function weekday(ms: number): string {
  return capitalize(new Date(ms).toLocaleDateString('es-ES', { weekday: 'short' }).replace('.', ''))
}

/** Local day + time of an event: "Hoy 14:30", "Mañana 08:30", "Vie 14:30", "Mié 12/11 20:00". */
export function fmtEventWhen(timeSec: number, nowMs: number): string {
  const ms = timeSec * 1000
  const diff = dayDiff(ms, nowMs)
  const hm = fmtHM(ms)
  if (diff === 0) return `Hoy ${hm}`
  if (diff === 1) return `Mañana ${hm}`
  if (diff === -1) return `Ayer ${hm}`
  if (diff > 1 && diff < 7) return `${weekday(ms)} ${hm}`
  return `${weekday(ms)} ${new Date(ms).toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit' })} ${hm}`
}

/** Day header for the weekly list: "Hoy · jueves 30/10", "Mañana · viernes 31/10", "Lunes 03/11". */
export function fmtDayHeader(timeSec: number, nowMs: number): string {
  const ms = timeSec * 1000
  const d = new Date(ms)
  const long = `${d.toLocaleDateString('es-ES', { weekday: 'long' })} ${d.toLocaleDateString('es-ES', { day: '2-digit', month: '2-digit' })}`
  const diff = dayDiff(ms, nowMs)
  if (diff === 0) return `Hoy · ${long}`
  if (diff === 1) return `Mañana · ${long}`
  return capitalize(long)
}

/** "14:30" when the time is today, otherwise "Vie 14:30". */
export function fmtClockRelative(ms: number, nowMs: number): string {
  return dayDiff(ms, nowMs) === 0 ? fmtHM(ms) : `${weekday(ms)} ${fmtHM(ms)}`
}

// Central-bank events are self-explanatory without a flag ("FOMC · 39 d").
const BANK_NAMES = new Set(['FOMC', 'Actas FOMC', 'Powell', 'BCE', 'Lagarde', 'BoE', 'BoJ', 'BoC', 'RBA', 'RBNZ', 'SNB'])

const SHORT_NAMES: [RegExp, string][] = [
  [/FOMC (Meeting )?Minutes|actas del FOMC/i, 'Actas FOMC'],
  [/Powell|Fed Chair/i, 'Powell'],
  [/\bFOMC\b|Federal Funds Rate|tipos de la Fed/i, 'FOMC'],
  [/\bADP\b/i, 'ADP'],
  [/Non-?Farm (Employment|Payrolls)|\bNFP\b|n[óo]minas no agr[íi]colas/i, 'NFP'],
  [/\bPCE\b/i, 'PCE'],
  [/Core CPI/i, 'IPC subyac.'],
  [/\bCPI\b|\bIPC\b/i, 'IPC'],
  [/\bPPI\b|\bIPP\b/i, 'IPP'],
  [/\bGDP\b|\bPIB\b/i, 'PIB'],
  [/Unemployment Claims|Jobless Claims/i, 'Paro semanal'],
  [/Unemployment Rate|tasa de (desempleo|paro)/i, 'Desempleo'],
  [/Average Hourly Earnings/i, 'Salarios'],
  [/Retail Sales|ventas minoristas/i, 'Ventas minor.'],
  [/ISM Manufacturing/i, 'ISM manuf.'],
  [/ISM (Services|Non-Manufacturing)/i, 'ISM serv.'],
  [/JOLTS/i, 'JOLTS'],
  [/UoM|Michigan/i, 'Michigan'],
  [/Lagarde/i, 'Lagarde'],
  [/Main Refinancing Rate|ECB|\bBCE\b/i, 'BCE'],
  [/Official Bank Rate|\bBOE\b/i, 'BoE'],
  [/\bBOJ\b/i, 'BoJ'],
  [/Overnight Rate|\bBOC\b/i, 'BoC'],
  [/\bRBA\b/i, 'RBA'],
  [/\bRBNZ\b/i, 'RBNZ'],
  [/\bSNB\b/i, 'SNB'],
]

/** Compact event name for the pill: "IPC 🇺🇸", "NFP 🇺🇸", "FOMC", "PIB 🇪🇺". */
export function eventShortName(ev: MacroEvent): string {
  const text = `${ev.title} · ${ev.title_es}`
  let name = ''
  if (ev.source === 'federalreserve') name = /minutes|actas/i.test(text) ? 'Actas FOMC' : 'FOMC'
  for (const [re, short] of SHORT_NAMES) {
    if (name) break
    if (re.test(text)) name = short
  }
  if (!name) {
    const base = ev.title_es || ev.title
    const acronym = /\(([A-ZÁÉÍÓÚÑ]{2,6})\)/.exec(base)
    let s = acronym ? acronym[1] : base.replace(/\([^)]*\)/g, ' ').replace(/\s+/g, ' ').trim()
    const cut = s.search(/\s(de|del|en)\s/i)
    if (cut > 0 && cut <= 18) s = s.slice(0, cut)
    name = s.length > 18 ? `${s.slice(0, 17).trimEnd()}…` : s
  }
  if (BANK_NAMES.has(name)) return name
  const flag = currencyFlag(ev.currency)
  return flag ? `${name} ${flag}` : ev.currency ? `${name} ${ev.currency}` : name
}

/** Compact countdown shown next to the level: "hasta 14:30" while the blackout runs (with a known end),
 *  otherwise "IPC 🇺🇸 · 24 min" / "FOMC · 39 d" for the next high-impact event; null when there is none. */
export function pulseCountdown(c: MarketConditions, nowMs: number): string | null {
  if (blackoutActive(c, nowMs) && c.blackout.until !== null) return `hasta ${fmtClockRelative(c.blackout.until, nowMs)}`
  const ev = pickNextEvent(c, nowMs)
  return ev ? `${eventShortName(ev)} · ${fmtCountdownShort(ev.time - nowMs / 1000)}` : null
}

/** Banner text while the blackout is running: "No operar hasta 14:30 — FOMC". Null when not active. */
export function blackoutText(c: MarketConditions, nowMs: number): string | null {
  if (!blackoutActive(c, nowMs)) return null
  const until = c.blackout.until !== null ? ` hasta ${fmtClockRelative(c.blackout.until, nowMs)}` : ' ahora'
  return `No operar${until}${c.blackout.event ? ` — ${c.blackout.event}` : ''}`
}

/** Human scope: "global" -> "Global", "crypto" -> "Cripto", "forex:EUR/USD" -> "Forex EUR/USD". */
export function scopeLabel(scope: string): string {
  const [kind, rest] = scope.split(':', 2)
  const names: Record<string, string> = {
    global: 'Global',
    crypto: 'Cripto',
    forex: 'Forex',
    us_equities: 'Bolsa EE. UU.',
    equities: 'Acciones',
    indices: 'Índices',
    commodities: 'Materias primas',
  }
  const base = names[kind] ?? capitalize(kind.replace(/_/g, ' '))
  return rest ? `${base} ${rest}` : base
}
