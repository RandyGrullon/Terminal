// Market alerts pushed by the backend on the WS topic 'macro' ({type:'macro', conditions, alert}): one toast per
// alert (red -> error, amber -> warn, green -> success), a short beep for red ones when the viewer wants sound,
// and duplicates of the same alert within 60 s are ignored (reconnects, several tabs, repeated broadcasts).
import { useEffect } from 'react'
import type { MacroAlert } from '../../api/types'
import { socket } from '../../api/ws'
import { useApp, type ToastLevel } from '../../store'
import { normalizeAlert } from './macroModel'
import { beepIfEnabled } from './sound'

const DEDUP_MS = 60_000
const seen = new Map<string, number>()

const TOAST_LEVEL: Record<MacroAlert['level'], ToastLevel> = { red: 'error', amber: 'warn', green: 'success' }

/** True when the same alert (level + message) was already shown during the last minute. */
function isDuplicate(alert: MacroAlert, now: number): boolean {
  for (const [key, ts] of Array.from(seen)) {
    if (now - ts >= DEDUP_MS) seen.delete(key)
  }
  const key = `${alert.level}|${alert.message}`
  if (seen.has(key)) return true
  seen.set(key, now)
  return false
}

export function showMacroAlert(alert: MacroAlert): void {
  if (isDuplicate(alert, Date.now())) return
  useApp.getState().notify(alert.message, TOAST_LEVEL[alert.level])
  if (alert.level === 'red') beepIfEnabled()
}

/** Listen to WS macro alerts. Mount it once (the header pill does). */
export function useMacroAlerts(): void {
  useEffect(
    () =>
      socket.on('macro', (msg) => {
        const alert = normalizeAlert(msg.alert)
        if (alert) showMacroAlert(alert)
      }),
    [],
  )
}
