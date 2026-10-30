// Ring buffer of terminal lines kept at module level, so closing and reopening the terminal panel keeps the
// output (the component re-syncs the events emitted while it was closed when it mounts again).
import { MAX_LINES, type TermEntry } from './termModel'

class TermBuffer {
  /** Display order. Mutated in place; readers re-filter on every version bump. */
  readonly lines: TermEntry[] = []
  /** Event ids currently present in `lines` (dedupe between REST history and live WS events). */
  readonly seen = new Set<number>()
  /** First line of the welcome banner (history is inserted right after the banner on the first sync). */
  welcome: TermEntry | null = null
  initialized = false
  historyLoaded = false
  /** unix ms of the last manual clear: re-syncs never bring back older events. */
  clearedAt = 0
  /** unix ms when the panel was closed (null while mounted). */
  closedAt: number | null = null
  /** unix ms when the live connection dropped (null while connected). */
  disconnectedAt: number | null = null

  append(entries: TermEntry[]): void {
    if (!entries.length) return
    for (const e of entries) {
      this.lines.push(e)
      if (e.eventId !== null) this.seen.add(e.eventId)
    }
    this.trim()
  }

  /** Insert after `marker` (or at `fallback` when the marker is gone). */
  insertAfter(marker: TermEntry | null, entries: TermEntry[], fallback: 'start' | 'end'): void {
    if (!entries.length) return
    let at = marker ? this.lines.lastIndexOf(marker) + 1 : 0
    if (at <= 0) at = fallback === 'start' ? 0 : this.lines.length
    this.lines.splice(at, 0, ...entries)
    for (const e of entries) if (e.eventId !== null) this.seen.add(e.eventId)
    this.trim()
  }

  clear(): void {
    this.lines.splice(0, this.lines.length)
    this.seen.clear()
    this.welcome = null
    this.clearedAt = Date.now()
  }

  last(): TermEntry | null {
    return this.lines.length ? this.lines[this.lines.length - 1] : null
  }

  private trim(): void {
    const extra = this.lines.length - MAX_LINES
    if (extra <= 0) return
    const dropped = this.lines.splice(0, extra)
    for (const e of dropped) {
      if (e.eventId !== null) this.seen.delete(e.eventId)
      if (e === this.welcome) this.welcome = null
    }
  }
}

export const termBuffer = new TermBuffer()
