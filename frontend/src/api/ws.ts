// Single WebSocket connection to the backend with auto-reconnect, ref-counted topic subscriptions and
// request/response terminal commands. In cloud mode it authenticates with ?token=<supabase access token>.
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

class NexusSocket {
  status: WsStatus = 'closed'
  private ws: WebSocket | null = null
  private handlers = new Map<string, Set<AnyHandler>>()
  private statusHandlers = new Set<(s: WsStatus) => void>()
  private topics = new Map<string, number>()
  private pending = new Map<string, Pending>()
  private outbox: string[] = []
  private retry = 0
  private seq = 0
  private reconnectTimer: number | null = null
  private pingTimer: number | null = null
  private hadSession = false

  constructor() {
    // Reconnect when the user logs in or out (token refreshes do not require a new socket).
    onAuthChange((s) => {
      const has = !!s
      if (has !== this.hadSession) {
        this.hadSession = has
        this.restart()
      }
    })
  }

  /** Drop the current connection (if any) and connect again immediately. */
  restart(): void {
    const old = this.ws
    this.ws = null
    if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer)
    if (this.pingTimer) window.clearInterval(this.pingTimer)
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
    const token = getAccessToken()
    if (authEnabled() && !token) {
      this.setStatus('closed') // wait for login (onAuthChange reconnects)
      return
    }
    this.hadSession = !!token
    this.setStatus('connecting')
    const ws = new WebSocket(token ? `${wsUrl()}?token=${encodeURIComponent(token)}` : wsUrl())
    this.ws = ws
    ws.onopen = () => {
      this.retry = 0
      this.setStatus('open')
      for (const topic of this.topics.keys()) this.rawSend({ type: 'subscribe', topic })
      const queued = this.outbox.splice(0)
      for (const m of queued) ws.send(m)
      if (this.pingTimer) window.clearInterval(this.pingTimer)
      this.pingTimer = window.setInterval(() => this.rawSend({ type: 'ping' }), 25000)
    }
    ws.onmessage = (ev) => {
      let msg: ServerMessage
      try {
        msg = JSON.parse(ev.data as string)
      } catch {
        return
      }
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
      if (this.pingTimer) window.clearInterval(this.pingTimer)
      this.setStatus('closed')
      if (ev.code === 4401 && authEnabled()) {
        // Token rejected: refresh it once; if that fails the user must log in again.
        void refreshSession().then((s) => (s ? this.connect() : signOut()))
        return
      }
      const delay = Math.min(10000, 500 * 2 ** this.retry++)
      if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = window.setTimeout(() => this.connect(), delay)
    }
    ws.onerror = () => {
      /* onclose handles reconnection */
    }
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
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(data)
    else {
      this.outbox.push(data)
      if (this.outbox.length > 200) this.outbox.shift()
      this.connect()
    }
  }

  private rawSend(msg: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg))
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
