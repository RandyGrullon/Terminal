// Session bands of the main chart (intraday bars up to 1h): the London-New York overlap (12:00–16:00 UTC, Monday to
// Friday), the broker's rollover window (23:00–01:00 of XM's server, plus Monday before 02:00: the weekly open) and
// the Friday close (Friday from 21:00 of the server: no new entries, forced close at 23:00). They are the windows of
// the GenXAU research (XAU M30 on XM): the overlap was its most consistent pattern, and still no strategy held up on
// new data, so the bands are context, never a signal. XM's server is GMT+2, GMT+3 while the US is on daylight
// saving time (second Sunday of March 2:00 → first Sunday of November 2:00, New York time). Pure functions.

export type SessionBand = 'overlap' | 'rollover' | 'friday'

/** Bars longer than this get no bands (a 4h or daily bar spans several windows). */
export const SESSION_MAX_TF_S = 3600

export const SESSION_COLORS: Record<SessionBand, string> = {
  overlap: 'rgba(79, 140, 255, 0.10)',
  rollover: 'rgba(240, 180, 41, 0.13)',
  friday: 'rgba(240, 80, 80, 0.13)',
}

export const SESSION_LABELS: Record<SessionBand, { label: string; hint: string }> = {
  overlap: { label: 'Overlap Londres-NY', hint: '12:00–16:00 UTC, de lunes a viernes' },
  rollover: { label: 'Rollover', hint: '23:00–01:00 del servidor de XM (se cobra el swap) y lunes antes de las 02:00' },
  friday: { label: 'Cierre del viernes', hint: 'Viernes desde las 21:00 del servidor de XM; a las 23:00 se cierra todo' },
}

export const SESSION_NOTE =
  'Contexto de la investigación GenXAU (oro M30): el overlap fue su patrón más consistente y, aun así, ninguna estrategia aguantó datos nuevos. No es una señal.'

const DAY = 86_400

/** Unix seconds (UTC midnight) of the n-th Sunday of a month (month 1-12). */
function nthSunday(year: number, month: number, n: number): number {
  const first = Date.UTC(year, month - 1, 1) / 1000
  const dow = new Date(first * 1000).getUTCDay() // 0 = Sunday
  return first + ((7 - dow) % 7) * DAY + (n - 1) * 7 * DAY
}

/** True while the US is on daylight saving time: second Sunday of March 2:00 EST (07:00 UTC) → first Sunday of
 *  November 2:00 EDT (06:00 UTC). */
export function usDst(tsSec: number): boolean {
  const year = new Date(tsSec * 1000).getUTCFullYear()
  return tsSec >= nthSunday(year, 3, 2) + 7 * 3600 && tsSec < nthSunday(year, 11, 1) + 6 * 3600
}

/** XM's server offset from UTC in hours: 3 with the US daylight saving time, else 2. */
export function xmServerOffsetHours(tsSec: number): 2 | 3 {
  return usDst(tsSec) ? 3 : 2
}

/** The band of a bar that opens at `tsSec` (unix seconds UTC), or null. */
export function sessionBandOf(tsSec: number): SessionBand | null {
  if (!Number.isFinite(tsSec)) return null
  const srv = tsSec + xmServerOffsetHours(tsSec) * 3600
  const hour = Math.floor(srv / 3600) % 24
  const dow = (Math.floor(srv / DAY) + 3) % 7 // 0 = Monday … 4 = Friday (server days)
  if (dow === 4 && hour >= 21) return 'friday'
  if (dow <= 4) {
    if ((hour >= 23 && dow <= 3) || (hour < 1 && dow >= 1) || (dow === 0 && hour < 2)) return 'rollover'
  }
  const utcHour = Math.floor(tsSec / 3600) % 24
  const utcDow = (Math.floor(tsSec / DAY) + 3) % 7
  if (utcDow <= 4 && utcHour >= 12 && utcHour < 16) return 'overlap'
  return null
}

/** [{time, band}] of the bars that fall in a band; empty for timeframes above 1h. */
export function sessionBands(times: readonly number[], tfSeconds: number): { time: number; band: SessionBand }[] {
  if (!(tfSeconds > 0) || tfSeconds > SESSION_MAX_TF_S) return []
  const out: { time: number; band: SessionBand }[] = []
  for (const t of times) {
    const band = sessionBandOf(t)
    if (band) out.push({ time: t, band })
  }
  return out
}
