// Types of the alerts feature (re-exported by types.ts). Same contract rules as types.ts.
// Alerts outside the app (backend app.alerts): GET /api/alerts/status -> AlertsStatus,
// POST /api/alerts/test {channel?} -> AlertsTestResponse. Secrets (bot token, SMTP password) are never returned.

export type AlertChannelId = 'telegram' | 'email'
export type AlertCategory = 'macro' | 'signals' | 'paper'
/** info = todos · warn = importantes y críticos · critical = solo críticos. */
export type AlertPriority = 'info' | 'warn' | 'critical'
export type AlertDeliveryStatus = 'sent' | 'partial' | 'failed' | 'quiet' | 'rate_limited' | 'no_channel'

/** Result of one delivery attempt on one channel (an alert message or a test). */
export interface AlertChannelResult {
  channel: AlertChannelId | string
  label: string
  ok: boolean
  /** Spanish error, null when delivered. */
  error: string | null
  /** Test only: the channel was not tried (incomplete or invalid configuration; `error` says why). */
  skipped: boolean
  test: boolean
  /** unix ms */
  at: number
  duration_ms: number
  /** Test results only: whether the channel is enabled for real alerts. */
  enabled?: boolean
}

export interface AlertChannelStatus {
  id: AlertChannelId | string
  label: string
  enabled: boolean
  /** Every required field present and valid. */
  configured: boolean
  /** enabled && configured: alerts are sent there. */
  active: boolean
  /** Missing fields, Spanish ("el token del bot", "el destinatario"...). */
  missing: string[]
  /** Invalid value, Spanish (bad token format, password without encryption...). */
  problem: string | null
  /** Where it sends ("chat 123456789", the recipients); never a secret. */
  target: string | null
  /** Last delivery of a real alert (tests not included). */
  last: AlertChannelResult | null
}

export interface AlertRecord {
  /** unix ms */
  at: number
  category: AlertCategory | string
  kind: string
  priority: AlertPriority
  text: string
  symbol: string | null
  status: AlertDeliveryStatus
  results: { channel: string; ok: boolean; error: string | null }[]
}

export interface AlertsCounters {
  received: number
  sent: number
  failed: number
  filtered: number
  deduped: number
  quiet: number
  rate_limited: number
  no_channel: number
  dropped: number
}

export interface AlertsTestResult {
  /** unix ms */
  at: number
  ok: boolean
  message: string
  results: AlertChannelResult[]
}

export interface AlertsStatus {
  running: boolean
  /** NEXUS_OFFLINE=1: nothing is sent. */
  offline: boolean
  channels: AlertChannelStatus[]
  active_channels: number
  categories: Record<AlertCategory, boolean>
  min_level: AlertPriority
  signal_symbols: string[]
  max_per_hour: number
  sent_last_hour: number
  quiet: { enabled: boolean; start: string; end: string; critical: boolean; active: boolean; timezone: string }
  /** Since `since` (unix ms; kept across restarts). */
  counters: AlertsCounters
  since: number
  /** Alerts queued or being delivered. */
  pending: number
  /** Alerts left out (hourly limit / quiet hours) not announced yet. */
  omitted: { rate: number; quiet: number }
  /** Newest first (≤ 30). */
  recent: AlertRecord[]
  last_test: AlertsTestResult | null
  /** Spanish notices (offline mode, watch disabled, invalid quiet hours...). */
  warnings: string[]
}

export interface AlertsTestResponse {
  /** At least one channel was tried and every tried channel delivered. */
  ok: boolean
  /** Spanish summary, e.g. "Telegram: enviado · Correo: El servidor de correo rechazó…". */
  message: string
  results: AlertChannelResult[]
}
