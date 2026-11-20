// How the server itself is doing (backend app.system_monitor): GET /api/system/status -> SystemStatus. Shown in the
// simple mode's «Servidor» tab together with the event log (GET /api/events).
// One field per line: tools/contract_check.py compares these interfaces with the live server.

/** Where the server runs. */
export interface SystemHost {
  kind: string // android | windows | linux | mac
  label: string // Spanish: «Teléfono Redmi Note 9 Pro (Termux)», «PC con Windows (…)», «Servidor en la nube»
  device: string
  python: string
}

/** One start of the server and what happened before it. */
export interface SystemStart {
  at: number // unix ms
  host: string
  down_ms: number | null // since the last heartbeat (null: the first start ever)
  clean: boolean | null // the previous stop was clean (false: a crash, a power cut, the system killed it)
  last_seen: number | null // unix ms of that last heartbeat
}

/** One check of the internet (a light request to the public market-data host). */
export interface SystemNetSample {
  ts: number // unix ms
  ok: boolean
  ms: number | null
  error: string | null
}

export interface SystemNet {
  state: string // ok | slow | down | unknown | off
  text: string // Spanish, shown verbatim
  target: string
  last_ms: number | null
  avg_ms: number | null // the last hour's successful checks
  checks_1h: number
  fails_1h: number
  samples: SystemNetSample[] // the last 30, oldest first
}

export interface SystemBattery {
  percent: number
  charging: boolean
  plugged: boolean
  temperature_c: number | null
  health: string | null
  source: string // sysfs | termux-api | windows
}

/** GET /api/system/status */
export interface SystemStatus {
  host: SystemHost
  started_at: number // unix ms
  uptime_s: number
  starts: SystemStart[] // the last 10, newest first
  net: SystemNet
  battery: SystemBattery | null
  battery_note: string | null // why the battery is not known (Spanish)
  memory_mb: number | null
  db_size_bytes: number | null
  server_time: number // unix ms
}
