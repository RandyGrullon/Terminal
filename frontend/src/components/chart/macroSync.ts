// When the chart has to re-read NEXUS because of the market semáforo. The backend merges the macro check (level and
// "no operar" window) into the NEXUS verdict when /api/nexus is requested, so a verdict fetched before a change keeps
// saying COMPRAR / TOMAR during a red blackout (or NO TOMAR after it ends) until something refetches it.
// No React here: ChartMacro.tsx wires these helpers to useMacro, the WebSocket and the NEXUS refresh.
import type { MarketConditions } from '../../api/types'
import { blackoutActive } from '../macro/macroModel'

/** Changes that arrive together (WS push + the symbol's REST answer) collapse into one NEXUS request. */
export const MACRO_REFRESH_DEBOUNCE_MS = 1500
/** While the semáforo is not green, NEXUS is re-read at most this often (its macro warning follows the data). */
export const MACRO_POLL_MS = 60_000

/** The part of the semáforo that changes the merged verdict: level and "no operar" window (its end to the minute,
 *  as the verdict shows it). null = unknown. */
export function macroKey(c: MarketConditions | null | undefined, nowMs: number): string | null {
  if (!c) return null
  const until = c.blackout.until === null ? '' : Math.round(c.blackout.until / 60_000)
  return `${c.level}|${blackoutActive(c, nowMs) ? 'blackout' : '-'}|${until}`
}

/** Reports real changes of a key: unknown (null) values are ignored and the first known value only seeds it
 *  (the NEXUS data on screen was fetched with that state already). */
export class ChangeDetector {
  private last: string | null = null

  /** True when `key` is known and differs from the previous known key. */
  next(key: string | null): boolean {
    if (key === null) return false
    const prev = this.last
    this.last = key
    return prev !== null && prev !== key
  }

  /** Forget the previous key: the next known value is a new initial value (e.g. another symbol). */
  reset(): void {
    this.last = null
  }
}

/** NEXUS must be re-read periodically while the semáforo is amber / red or a "no operar" window runs. */
export function needsMacroPoll(c: MarketConditions | null | undefined, nowMs: number): boolean {
  return !!c && (c.level !== 'green' || blackoutActive(c, nowMs))
}

/** Poll tick: refresh only when a NEXUS verdict is on screen and neither a NEXUS update nor a previous poll
 *  happened within MACRO_POLL_MS (so polling never runs more often than that, even when requests fail). */
export function macroPollDue(nowMs: number, lastNexusAt: number | null, lastPollAt: number | null): boolean {
  if (lastNexusAt === null) return false
  return nowMs - Math.max(lastNexusAt, lastPollAt ?? -Infinity) >= MACRO_POLL_MS
}

export interface Debouncer {
  /** (Re)start the wait: `fn` runs once, `ms` after the last trigger. */
  trigger: () => void
  cancel: () => void
  pending: () => boolean
}

export function createDebouncer(fn: () => void, ms: number): Debouncer {
  let timer: ReturnType<typeof setTimeout> | null = null
  const cancel = () => {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }
  return {
    trigger: () => {
      cancel()
      timer = setTimeout(() => {
        timer = null
        fn()
      }, ms)
    },
    cancel,
    pending: () => timer !== null,
  }
}
