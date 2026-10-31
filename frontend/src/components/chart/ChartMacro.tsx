// Semáforo of the charted symbol in the toolbar, and the NEXUS verdict kept in step with it (see macroSync.ts):
//  - the symbol's level / "no operar" window changes (REST, 60 s) -> debounced NEXUS refresh,
//  - the global semáforo pushed over the WebSocket changes -> re-read the symbol's semáforo + NEXUS,
//  - while the level is not green -> NEXUS re-read at most every 60 s.
// Lives in its own component so the badge's 1 s countdown clock does not re-render the whole chart page.
import { useEffect, useMemo, useRef } from 'react'
import { socket } from '../../api/ws'
import { MacroBadge } from '../macro/MacroBadge'
import { normalizeConditions } from '../macro/macroModel'
import { useMacro } from '../macro/useMacro'
import { ChangeDetector, MACRO_POLL_MS, MACRO_REFRESH_DEBOUNCE_MS, createDebouncer, macroKey, macroPollDue, needsMacroPoll } from './macroSync'

interface Props {
  symbol: string
  /** `${symbol}|${tf}` of the NEXUS data on screen. */
  scope: string
  /** Unix ms of the last NEXUS update on screen (null = none yet). */
  nexusUpdatedAt: number | null
  /** Re-reads NEXUS for the current symbol / timeframe. */
  refreshNexus: () => void
}

export default function ChartMacro({ symbol, scope, nexusUpdatedAt, refreshNexus }: Props) {
  const macro = useMacro(symbol)
  const refreshRef = useRef(refreshNexus)
  refreshRef.current = refreshNexus
  const refreshMacroRef = useRef(macro.refresh)
  refreshMacroRef.current = macro.refresh
  const lastNexusRef = useRef(nexusUpdatedAt)
  lastNexusRef.current = nexusUpdatedAt
  const debouncer = useMemo(() => createDebouncer(() => refreshRef.current(), MACRO_REFRESH_DEBOUNCE_MS), [])

  // A pending refresh belongs to the previous symbol / timeframe (the new one is loading fresh data anyway).
  useEffect(() => () => debouncer.cancel(), [scope, debouncer])

  // Level / blackout of the charted symbol. The first value of each symbol only seeds the detector.
  const symbolChanges = useRef(new ChangeDetector())
  useEffect(() => {
    symbolChanges.current.reset()
  }, [symbol])
  const key = macroKey(macro.conditions, macro.now)
  useEffect(() => {
    if (symbolChanges.current.next(key)) debouncer.trigger()
  }, [key, debouncer])

  // Global semáforo pushed on the 'macro' topic every refresh cycle: act only when it changes.
  useEffect(() => {
    const globalChanges = new ChangeDetector()
    return socket.on('macro', (msg) => {
      const c = normalizeConditions(msg?.conditions)
      if (!c || c.symbol) return
      if (globalChanges.next(macroKey(c, Date.now()))) {
        refreshMacroRef.current()
        debouncer.trigger()
      }
    })
  }, [debouncer])

  // Amber / red: the merged warning (VIX, next event, ...) changes without a level change.
  const polling = needsMacroPoll(macro.conditions, macro.now)
  useEffect(() => {
    if (!polling) return
    let lastPoll: number | null = null
    const id = window.setInterval(() => {
      if (document.hidden) return
      const now = Date.now()
      if (!macroPollDue(now, lastNexusRef.current, lastPoll)) return
      lastPoll = now
      refreshRef.current()
    }, MACRO_POLL_MS / 4)
    return () => window.clearInterval(id)
  }, [polling, scope])

  return <MacroBadge symbol={symbol} state={macro} />
}
