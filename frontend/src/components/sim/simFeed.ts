// Live simulator feed: GET /api/sim/state snapshot + WS "sim" messages, kept consistent across reconnects,
// restarts and missed messages. Chart updates are applied immediately; React state is flushed at ~8 Hz.
import { useEffect, useRef, useState } from 'react'
import { api, errorMessage } from '../../api/client'
import type { Candle, NexusCurrent, Position, ServerMessage, SimSnapshot, SimState } from '../../api/types'
import { socket } from '../../api/ws'
import { SimFeedModel, type EmaLegend, type LiveChartView } from './liveModel'

type SimMessage = Extract<ServerMessage, { type: 'sim' }>
type ResyncKind = 'hard' | 'soft'

export interface SimView {
  state: SimState | null
  current: NexusCurrent | null
  open: Position[]
  closed: Position[]
  emas: EmaLegend[]
  bars: number
  lastBar: Candle | null
}

export const EMPTY_VIEW: SimView = { state: null, current: null, open: [], closed: [], emas: [], bars: 0, lastBar: null }

export interface SimFeedListener {
  ui: (view: SimView) => void
  loading: (loading: boolean) => void
  error: (message: string | null) => void
  /** Called after a full resync with the state of the (possibly new) session. */
  session: (state: SimState) => void
}

const FLUSH_MS = 120
const SOFT_RESYNC_MIN_MS = 4000
const MAX_QUEUE = 5000
const MAX_CLOSED = 200

function sessionKeyOf(st: SimState | null | undefined): string | null {
  if (!st || st.started_at === null || st.started_at === undefined) return null
  return `${st.started_at}|${st.mode ?? ''}|${st.symbol ?? ''}|${st.tf ?? ''}`
}

function isPosition(p: unknown): p is Position {
  const x = p as Position | null
  return !!x && typeof x === 'object' && typeof x.id === 'number' && (x.status === 'open' || x.status === 'closed')
}

const exitOrder = (p: Position): number => p.exit_time ?? p.entry_time ?? 0

export class SimFeed {
  readonly model = new SimFeedModel()
  private view: LiveChartView | null = null
  private state: SimState | null = null
  private current: NexusCurrent | null = null
  private positions = new Map<number, Position>()
  private posVersion = 0
  private cachedLists: { version: number; open: Position[]; closed: Position[] } = { version: -1, open: [], closed: [] }
  private cachedEmas: { version: number; emas: EmaLegend[] } = { version: -1, emas: [] }
  private gen = 0
  private active = false
  private syncing = false
  private again: ResyncKind | null = null
  private queue: SimMessage[] = []
  private lastSoft = 0
  private softTimer: number | null = null
  private flushTimer: number | null = null
  private sessionKey: string | null = null
  private lastBarsDone: number | null = null
  private wsWasDown = false
  private offs: Array<() => void> = []

  constructor(private readonly listener: SimFeedListener) {}

  start(): void {
    if (this.active) return
    this.active = true
    this.gen++
    this.offs.push(socket.on('sim', (m) => this.handle(m, false)))
    this.offs.push(
      socket.onStatus((s) => {
        if (s !== 'open') {
          this.wsWasDown = true
          return
        }
        if (this.wsWasDown) {
          this.wsWasDown = false
          // Messages may have been lost while disconnected: re-read the authoritative state.
          void this.resync('soft')
        }
      }),
    )
    void this.resync('hard')
  }

  stop(): void {
    this.active = false
    this.gen++
    for (const off of this.offs.splice(0)) off()
    if (this.flushTimer !== null) window.clearTimeout(this.flushTimer)
    if (this.softTimer !== null) window.clearTimeout(this.softTimer)
    this.flushTimer = null
    this.softTimer = null
    this.syncing = false
    this.again = null
    this.queue = []
  }

  /** The chart component registers its view when the chart exists (and null on unmount). */
  attachView(view: LiveChartView | null): void {
    this.view = view
    if (view) {
      view.render(this.model, true)
      view.renderLevels(this.current?.position ?? null)
    }
  }

  /** Apply a SimState returned by a REST action (start / stop / config) for immediate feedback. */
  applyState(state: SimState | null | undefined, forceResync = false): void {
    if (!state || typeof state !== 'object') return
    const newSession = state.running && sessionKeyOf(state) !== null && sessionKeyOf(state) !== this.sessionKey
    this.state = state
    this.flushNow()
    if (forceResync || newSession) void this.resync('hard')
  }

  /** Re-read GET /api/sim/state. 'hard' resets the chart view (new session); 'soft' keeps zoom and closed trades. */
  async resync(kind: ResyncKind): Promise<void> {
    if (!this.active) return
    if (this.syncing) {
      this.again = this.again === 'hard' || kind === 'hard' ? 'hard' : 'soft'
      return
    }
    const gen = this.gen
    this.syncing = true
    this.queue = []
    if (kind === 'soft') this.lastSoft = Date.now()
    if (kind === 'hard') this.listener.loading(true)
    let snap: SimSnapshot | null = null
    let failure: unknown = null
    try {
      snap = await api.sim.state()
    } catch (err) {
      failure = err
    }
    if (gen !== this.gen || !this.active) return
    this.syncing = false
    const queued = this.queue
    this.queue = []
    if (snap && typeof snap === 'object') {
      this.applySnapshot(snap, kind)
      this.listener.error(null)
    } else {
      this.listener.error(failure ? errorMessage(failure) : 'Respuesta vacía del simulador')
    }
    for (const m of queued) this.handle(m, true)
    this.listener.loading(false)
    this.flushNow()
    const again = this.again
    this.again = null
    if (again) void this.resync(again)
  }

  private applySnapshot(snap: SimSnapshot, kind: ResyncKind): void {
    const st = snap.state && typeof snap.state === 'object' ? snap.state : null
    this.state = st
    this.sessionKey = sessionKeyOf(st)
    this.lastBarsDone = typeof st?.bars_done === 'number' ? st.bars_done : null
    this.model.load(Array.isArray(snap.candles) ? snap.candles : [], snap.nexus ?? null, typeof st?.tf === 'string' ? st.tf : null)
    this.current = snap.nexus?.current ?? null
    const next = new Map<number, Position>()
    if (kind === 'soft') {
      for (const [id, p] of this.positions) if (p.status === 'closed') next.set(id, p)
    }
    for (const p of Array.isArray(snap.positions) ? snap.positions : []) if (isPosition(p)) next.set(p.id, p)
    this.positions = next
    this.capClosed()
    this.posVersion++
    this.view?.render(this.model, kind === 'hard')
    this.view?.renderLevels(this.current?.position ?? null)
    if (kind === 'hard' && st) this.listener.session(st)
  }

  private handle(msg: SimMessage, replay: boolean): void {
    if (!this.active || !msg) return
    if (this.syncing && !replay) {
      this.queue.push(msg)
      if (this.queue.length > MAX_QUEUE) this.queue.shift()
      return
    }
    const st = msg.state && typeof msg.state === 'object' ? msg.state : null

    if (msg.event === 'started' || msg.event === 'reset') {
      if (st) this.state = st
      const key = sessionKeyOf(st)
      // A replayed 'started' of the session we just loaded needs no second fetch.
      if (!(replay && msg.event === 'started' && key !== null && key === this.sessionKey)) void this.resync('hard')
      this.flushSoon()
      return
    }
    if (st && !replay && st.running) {
      const key = sessionKeyOf(st)
      if (key !== null && key !== this.sessionKey) {
        // A new session started and we missed its 'started' message.
        this.state = st
        void this.resync('hard')
        return
      }
    }
    if (st) this.state = st

    if (msg.event === 'bar' && msg.bar) {
      const done = st?.bars_done
      if (typeof done === 'number' && Number.isFinite(done)) {
        if (!replay && this.lastBarsDone !== null && done > this.lastBarsDone + 1) this.requestSoftResync()
        if (this.lastBarsDone === null || done > this.lastBarsDone) this.lastBarsDone = done
      }
      const res = this.model.pushBar(msg.bar)
      if (res !== 'ignored') {
        if (this.model.trimIfNeeded()) this.view?.render(this.model, false)
        else this.view?.pushed(this.model, res)
      }
    }
    if (Array.isArray(msg.markers) && msg.markers.length > 0 && this.model.addMarkers(msg.markers)) {
      this.view?.renderMarkers(this.model)
    }
    if (msg.current !== undefined) {
      this.current = msg.current ?? null
      this.view?.renderLevels(this.current?.position ?? null)
    }
    if (Array.isArray(msg.positions)) this.mergePositions(msg.positions)
    this.flushSoon()
  }

  /** `positions` is the account's current list: upsert by id; an open position that disappears was closed. */
  private mergePositions(list: Position[]): void {
    const seen = new Set<number>()
    for (const p of list) {
      if (!isPosition(p)) continue
      this.positions.set(p.id, p)
      seen.add(p.id)
    }
    let vanished = false
    for (const [id, p] of this.positions) {
      if (p.status === 'open' && !seen.has(id)) {
        this.positions.delete(id)
        vanished = true
      }
    }
    this.capClosed()
    this.posVersion++
    // The closed version (exit price, P&L, reason) comes with the next snapshot.
    if (vanished) this.requestSoftResync()
  }

  private capClosed(): void {
    const closed: Position[] = []
    for (const p of this.positions.values()) if (p.status === 'closed') closed.push(p)
    if (closed.length <= MAX_CLOSED) return
    closed.sort((a, b) => exitOrder(a) - exitOrder(b))
    for (const p of closed.slice(0, closed.length - MAX_CLOSED)) this.positions.delete(p.id)
  }

  private requestSoftResync(): void {
    if (this.softTimer !== null || this.syncing || !this.active) return
    const wait = Math.max(0, SOFT_RESYNC_MIN_MS - (Date.now() - this.lastSoft))
    this.softTimer = window.setTimeout(() => {
      this.softTimer = null
      void this.resync('soft')
    }, wait)
  }

  private flushSoon(): void {
    if (this.flushTimer !== null) return
    this.flushTimer = window.setTimeout(() => {
      this.flushTimer = null
      this.flushNow()
    }, FLUSH_MS)
  }

  private flushNow(): void {
    if (this.flushTimer !== null) {
      window.clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (!this.active) return
    if (this.cachedLists.version !== this.posVersion) {
      const open: Position[] = []
      const closed: Position[] = []
      for (const p of this.positions.values()) (p.status === 'open' ? open : closed).push(p)
      open.sort((a, b) => (b.entry_time ?? 0) - (a.entry_time ?? 0))
      closed.sort((a, b) => exitOrder(b) - exitOrder(a))
      this.cachedLists = { version: this.posVersion, open, closed }
    }
    if (this.cachedEmas.version !== this.model.version) {
      this.cachedEmas = { version: this.model.version, emas: this.model.emaLegend() }
    }
    this.listener.ui({
      state: this.state,
      current: this.current,
      open: this.cachedLists.open,
      closed: this.cachedLists.closed,
      emas: this.cachedEmas.emas,
      bars: this.model.candles.length,
      lastBar: this.model.lastCandle(),
    })
  }
}

/** React binding: one SimFeed per mounted simulator, started/stopped with the component. */
export function useSimFeed(onSession: (state: SimState) => void) {
  const [view, setView] = useState<SimView>(EMPTY_VIEW)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const sessionRef = useRef(onSession)
  sessionRef.current = onSession
  const feedRef = useRef<SimFeed | null>(null)
  if (feedRef.current === null) {
    feedRef.current = new SimFeed({
      ui: setView,
      loading: setLoading,
      error: setError,
      session: (s) => sessionRef.current(s),
    })
  }
  useEffect(() => {
    const feed = feedRef.current
    if (!feed) return
    feed.start()
    return () => feed.stop()
  }, [])
  return { feed: feedRef.current, view, loading, error }
}
