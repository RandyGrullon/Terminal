// Single WebSocket connection to the backend with auto-reconnect, ref-counted topic subscriptions and
// request/response terminal commands. In cloud mode it authenticates with ?token=<supabase access token>: the server
// accepts the socket and closes it with 4401 when the token is invalid or expires (refresh the session, then
// reconnect) or with 1013 when it cannot serve it right now (Supabase unreachable, or this instance is in standby
// during a deploy: keep the session, back off).
import { authEnabled, getAccessToken, onAuthChange, refreshSession, signOut } from '../auth/session'
import { wsUrl } from '../lib/config'
import type { CommandResult, ServerMessage, ServerMessageType } from './types'

export type WsStatus = 'connecting' | 'open' | 'closed'

type MessageOf<T extends ServerMessageType> = Extract<ServerMessage, { type: T }>
type AnyHandler = (msg: ServerMessage) => void

interface Pending {
  resolve: (r: CommandResult) => void
  reject: (e: Error) => void
  timer: number
}

export const marketTopic = (symbol: string, tf: string) => `market:${symbol.toUpperCase()}:${tf}`

/** Topics the server subscribes every client to on connect. Never unsubscribe them (that would silence the
 *  topic for the whole app): socket.subscribe() on these is a no-op; just use socket.on(). */
export const DEFAULT_TOPICS: ReadonlySet<string> = new Set(['events', 'sim', 'paper', 'scanner', 'ui', 'macro'])

/** 4401 closes in a row (with a refreshed token in between, and no socket that stayed usable for AUTH_STABLE_MS) after
 *  which this account is taken to have lost access to this NEXUS instance: sign out, like the REST client does when a
 *  fresh token is still refused. */
const MAX_AUTH_REJECTS = 3
/** An open socket is used once the server speaks (its 'hello'); servers that do not are used after this delay. */
const READY_FALLBACK_MS = 3000
/** The 4401 count starts again only once a socket has stayed usable this long (or after a new login). The hello alone
 *  proves nothing: a server whose clock runs ahead of Supabase's by more than the token lifetime says hello and closes
 *  with 4401 at once, every time, and that must still end in the sign-out instead of a refresh loop. */
const AUTH_STABLE_MS = 30_000

class NexusSocket {
  status: WsStatus = 'closed'
  private ws: WebSocket | null = null
  private ready = false // the server accepted this socket's session (first message received)
  private handlers = new Map<string, Set<AnyHandler>>()
  private statusHandlers = new Set<(s: WsStatus) => void>()
  private topics = new Map<string, number>()
  private pending = new Map<string, Pending>()
  private outbox: string[] = []
  private retry = 0
  private seq = 0
  private reconnectTimer: number | null = null
  private pingTimer: number | null = null
  private readyTimer: number | null = null
  private stableTimer: number | null = null // resets authRejects once the usable socket has lasted AUTH_STABLE_MS
  private hadSession = false
  private token: string | null = null // access token the current (or last) socket was opened with
  private serverToken: string | null = null // latest token handed to the server on this socket (handshake or 'auth')
  private awaitingToken = false // parked after a 4401 until the session has a new token
  private authRejects = 0 // 4401 closes since a socket last stayed usable for AUTH_STABLE_MS (or since the login)

  constructor() {
    // Reconnect when the user logs in or out, and after a 4401 as soon as the session has a new token. A plain token
    // refresh needs no new socket: the refreshed token is handed to the open one ({type: 'auth'}) so the server keeps
    // it past the old token's expiry; a server that ignores it closes with 4401 at expiry and we reconnect.
    onAuthChange((s) => {
      const has = !!s
      if (has !== this.hadSession) {
        this.hadSession = has
        this.authRejects = 0
        this.restart()
      } else if (s && this.awaitingToken && s.access_token !== this.token) {
        this.restart()
      } else if (s) {
        this.sendToken()
      }
    })
  }

  /** Give the server the session's current token when the open socket still uses an older one. */
  private sendToken(): void {
    const token = getAccessToken()
    if (!token || token === this.serverToken || !this.isUsable()) return
    this.serverToken = token
    this.rawSend({ type: 'auth', token })
  }

  /** Drop the current connection (if any) and connect again immediately. */
  restart(): void {
    const old = this.ws
    this.ws = null
    this.ready = false
    this.awaitingToken = false
    if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.stopSocketTimers()
    if (old) {
      old.onclose = null
      old.close()
    }
    this.retry = 0
    this.setStatus('closed')
    this.connect()
  }

  connect(): void {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return
    if (this.awaitingToken) return // the rejected token would be refused again: onAuthChange reconnects
    const token = getAccessToken()
    if (authEnabled() && !token) {
      this.setStatus('closed') // wait for login (onAuthChange reconnects)
      return
    }
    this.hadSession = !!token
    this.token = token
    this.serverToken = token
    this.ready = false
    this.setStatus('connecting')
    const ws = new WebSocket(token ? `${wsUrl()}?token=${encodeURIComponent(token)}` : wsUrl())
    this.ws = ws
    ws.onopen = () => {
      // Open is not authenticated yet: in cloud mode the server accepts first and then closes with 4401 / 1013 when it
      // cannot accept the token. Its first message (hello) confirms the session.
      if (this.ws !== ws) return
      if (this.readyTimer) window.clearTimeout(this.readyTimer)
      this.readyTimer = window.setTimeout(() => this.markReady(ws), READY_FALLBACK_MS)
    }
    ws.onmessage = (ev) => {
      let msg: ServerMessage
      try {
        msg = JSON.parse(ev.data as string)
      } catch {
        return
      }
      if (!this.ready) this.markReady(ws)
      if (msg.type === 'command_result') {
        const p = this.pending.get(msg.id)
        if (p) {
          window.clearTimeout(p.timer)
          this.pending.delete(msg.id)
          p.resolve(msg)
        }
      }
      this.dispatch(msg.type, msg)
      this.dispatch('*', msg)
    }
    ws.onclose = (ev) => {
      if (this.ws !== ws) return
      this.ws = null
      this.ready = false
      this.stopSocketTimers()
      this.setStatus('closed')
      if (ev.code === 4401 && authEnabled()) {
        this.onAuthRejected()
        return
      }
      // 1013 (try again later): the server could not verify the session right now. Transient: keep the session and
      // reconnect with backoff, like after any other drop.
      this.scheduleReconnect(ev.code === 1013 ? 2000 : 0)
    }
    ws.onerror = () => {
      /* onclose handles reconnection */
    }
  }

  /** The server spoke on this socket: the session was accepted. Resubscribe, flush the queue, start pinging. */
  private markReady(ws: WebSocket): void {
    if (this.ws !== ws || this.ready || ws.readyState !== WebSocket.OPEN) return
    this.ready = true
    this.retry = 0
    if (this.readyTimer) window.clearTimeout(this.readyTimer)
    this.readyTimer = null
    // Not authRejects = 0 here: only a socket that the server keeps open shows the session is accepted for good.
    if (this.stableTimer) window.clearTimeout(this.stableTimer)
    this.stableTimer = window.setTimeout(() => {
      this.stableTimer = null
      if (this.ws === ws && this.ready) this.authRejects = 0
    }, AUTH_STABLE_MS)
    this.setStatus('open')
    if (authEnabled()) this.sendToken() // the session may have been refreshed while connecting
    for (const topic of this.topics.keys()) this.rawSend({ type: 'subscribe', topic })
    const queued = this.outbox.splice(0)
    for (const m of queued) ws.send(m)
    if (this.pingTimer) window.clearInterval(this.pingTimer)
    this.pingTimer = window.setInterval(() => this.rawSend({ type: 'ping' }), 25000)
  }

  /** 4401: the server refused this socket's token (expired, revoked or no longer allowed). */
  private onAuthRejected(): void {
    this.authRejects += 1
    if (this.authRejects >= MAX_AUTH_REJECTS) {
      // Even refreshed tokens are refused: this account can no longer use this NEXUS instance.
      this.authRejects = 0
      void signOut({ scope: 'local' })
      return
    }
    const current = getAccessToken()
    if (current && current !== this.token) {
      this.scheduleReconnect() // the session was refreshed while this socket was open: use the new token
      return
    }
    // Wait for a new token: onAuthChange reconnects once the refresh succeeds. If Supabase rejects the refresh token
    // for good the session ends (login screen); if it cannot be reached the session module retries with backoff.
    this.awaitingToken = true
    refreshSession().catch(() => undefined)
  }

  private scheduleReconnect(minDelayMs = 0): void {
    const delay = Math.max(minDelayMs, Math.min(10000, 500 * 2 ** this.retry++))
    if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private stopSocketTimers(): void {
    if (this.pingTimer) window.clearInterval(this.pingTimer)
    if (this.readyTimer) window.clearTimeout(this.readyTimer)
    if (this.stableTimer) window.clearTimeout(this.stableTimer)
    this.pingTimer = null
    this.readyTimer = null
    this.stableTimer = null
  }

  /** Listen to one message type ('*' = all). Returns an unsubscribe function. */
  on<T extends ServerMessageType>(type: T, fn: (msg: MessageOf<T>) => void): () => void
  on(type: '*', fn: (msg: ServerMessage) => void): () => void
  on(type: string, fn: (msg: never) => void): () => void {
    let set = this.handlers.get(type)
    if (!set) {
      set = new Set()
      this.handlers.set(type, set)
    }
    const h = fn as unknown as AnyHandler
    set.add(h)
    return () => {
      set?.delete(h)
    }
  }

  onStatus(fn: (s: WsStatus) => void): () => void {
    this.statusHandlers.add(fn)
    fn(this.status)
    return () => {
      this.statusHandlers.delete(fn)
    }
  }

  /** Ref-counted topic subscription (re-sent automatically after reconnects). Returns an unsubscribe fn. */
  subscribe(topic: string): () => void {
    if (DEFAULT_TOPICS.has(topic)) return () => undefined // always on: see DEFAULT_TOPICS
    const n = this.topics.get(topic) ?? 0
    this.topics.set(topic, n + 1)
    if (n === 0) this.rawSend({ type: 'subscribe', topic })
    let done = false
    return () => {
      if (done) return
      done = true
      const left = (this.topics.get(topic) ?? 1) - 1
      if (left <= 0) {
        this.topics.delete(topic)
        this.rawSend({ type: 'unsubscribe', topic })
      } else {
        this.topics.set(topic, left)
      }
    }
  }

  /** Run a terminal command over the socket. */
  command(text: string, timeoutMs = 180000): Promise<CommandResult> {
    const id = `c${Date.now().toString(36)}${(++this.seq).toString(36)}`
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id)
        reject(new Error('El comando tardó demasiado en responder'))
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      this.send({ type: 'command', id, text })
    })
  }

  send(msg: Record<string, unknown>): void {
    const data = JSON.stringify(msg)
    if (this.isUsable()) this.ws!.send(data)
    else {
      // Queued until the server accepts a socket (a socket it is about to close with 4401 would lose it).
      this.outbox.push(data)
      if (this.outbox.length > 200) this.outbox.shift()
      this.connect()
    }
  }

  private isUsable(): boolean {
    return !!this.ws && this.ready && this.ws.readyState === WebSocket.OPEN
  }

  private rawSend(msg: Record<string, unknown>): void {
    if (this.isUsable()) this.ws!.send(JSON.stringify(msg))
  }

  private dispatch(type: string, msg: ServerMessage): void {
    const set = this.handlers.get(type)
    if (!set) return
    for (const fn of Array.from(set)) {
      try {
        fn(msg)
      } catch (err) {
        console.error('ws handler error', err)
      }
    }
  }

  private setStatus(s: WsStatus): void {
    this.status = s
    for (const fn of Array.from(this.statusHandlers)) fn(s)
  }
}

export const socket = new NexusSocket()
