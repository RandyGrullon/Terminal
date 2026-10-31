// Market alerts pushed by the backend on the WS topic 'macro' ({type:'macro', conditions, alert}): one toast per
// alert (red -> error, amber -> warn, green -> success), a short beep for red ones when the viewer wants sound,
// and duplicates of the same alert within 60 s are ignored (reconnects, several tabs, repeated broadcasts).
// Desktop notifications (opt-in per browser, see sound.ts): red market alerts and NEXUS signals (entries, exits and
// early exits from the events log) are also shown with the Notification API while the tab is in the background.
// Alerts that must reach the user with the app closed go through the backend channels (Ajustes → Alertas).
import { useEffect } from 'react'
import type { MacroAlert, NexusEvent, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { socket } from '../../api/ws'
import { useApp, type ToastLevel } from '../../store'
import { normalizeAlert } from './macroModel'
import { beepIfEnabled, notifyDesktop, type DesktopNotice } from './sound'

const DEDUP_MS = 60_000
const seen = new Map<string, number>()

const TOAST_LEVEL: Record<MacroAlert['level'], ToastLevel> = { red: 'error', amber: 'warn', green: 'success' }

const SIGNAL_TITLE: Record<string, string> = {
  BUY: 'NEXUS · Señal de compra',
  SELL: 'NEXUS · Señal de venta',
  EXIT_LONG: 'NEXUS · Señal de salida',
  EXIT_SHORT: 'NEXUS · Señal de salida',
}
const NOT_ADVICE = 'Señal del modelo: no es una orden ni asesoría financiera.'

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

/** Small stable hash for notification tags (same alert text -> same tag in every tab). */
function hash(text: string): string {
  let h = 0
  for (let i = 0; i < text.length; i++) h = (Math.imul(31, h) + text.charCodeAt(i)) | 0
  return (h >>> 0).toString(36)
}

/** Desktop notification for a market alert: only red ones ("no operar"). */
export function macroDesktopNotice(alert: MacroAlert): DesktopNotice | null {
  if (alert.level !== 'red' || !alert.message) return null
  return { title: 'NEXUS · Semáforo en rojo', body: alert.message, tag: `nexus-macro-${hash(`${alert.kind}|${alert.message}`)}` }
}

/** Desktop notification for a NEXUS event: entries / exits (level 'signal') and early exits (EARLY_EXIT warning) of
 *  real markets; null for anything else (rejected signals, other sources, the synthetic SIM:* markets). */
export function signalDesktopNotice(ev: NexusEvent | null | undefined): DesktopNotice | null {
  if (!ev || ev.source !== 'nexus' || typeof ev.message !== 'string' || !ev.message) return null
  const data = ev.data && typeof ev.data === 'object' && !Array.isArray(ev.data) ? (ev.data as Record<string, unknown>) : null
  if (!data) return null
  const symbol = typeof data.symbol === 'string' ? data.symbol.trim().toUpperCase() : ''
  const tf = typeof data.tf === 'string' ? data.tf : ''
  if (!symbol || symbol.startsWith('SIM:')) return null
  let title: string | null = null
  if (ev.level === 'signal' && typeof data.action === 'string') title = SIGNAL_TITLE[data.action] ?? null
  else if (ev.level === 'warn' && data.code === 'EARLY_EXIT') title = 'NEXUS · Salida anticipada'
  if (!title) return null
  const timeframe = (TIMEFRAMES as string[]).includes(tf) ? (tf as Timeframe) : undefined
  return {
    title,
    body: `${ev.message}\n${NOT_ADVICE}`,
    tag: `nexus-event-${Number.isFinite(ev.id) ? ev.id : hash(ev.message)}`,
    onClick: () => useApp.getState().openChart(symbol, timeframe),
  }
}

export function showMacroAlert(alert: MacroAlert): void {
  if (isDuplicate(alert, Date.now())) return
  useApp.getState().notify(alert.message, TOAST_LEVEL[alert.level])
  if (alert.level === 'red') beepIfEnabled()
  const notice = macroDesktopNotice(alert)
  if (notice) notifyDesktop(notice)
}

/** Subscribe to the WS macro alerts (toasts, beep, desktop) and the NEXUS signal events (desktop only: the app shell
 *  already toasts them). Returns the unsubscribe function. */
export function startAlertListeners(): () => void {
  const offMacro = socket.on('macro', (msg) => {
    const alert = normalizeAlert(msg.alert)
    if (alert) showMacroAlert(alert)
  })
  const offEvent = socket.on('event', (msg) => {
    const notice = signalDesktopNotice(msg.event)
    if (notice) notifyDesktop(notice)
  })
  return () => {
    offMacro()
    offEvent()
  }
}

/** Listen to WS macro alerts and NEXUS signals. Mount it once (the header pill does). */
export function useMacroAlerts(): void {
  useEffect(() => startAlertListeners(), [])
}
