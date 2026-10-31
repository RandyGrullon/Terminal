// Stand-ins for src/api/ws.ts and src/store.ts, swapped in by tests/imp-alerts.run.mjs when components/macro/alerts.ts
// is bundled: a socket whose handlers the test triggers, and a store that records toasts and chart openings.
/* eslint-disable @typescript-eslint/no-explicit-any */

type Handler = (msg: any) => void

class FakeSocket {
  handlers = new Map<string, Set<Handler>>()
  on(type: string, fn: Handler): () => void {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set())
    this.handlers.get(type)!.add(fn)
    return () => {
      this.handlers.get(type)?.delete(fn)
    }
  }
  emit(type: string, msg: Record<string, unknown>): void {
    for (const fn of Array.from(this.handlers.get(type) ?? [])) fn({ type, ...msg })
  }
  count(type: string): number {
    return this.handlers.get(type)?.size ?? 0
  }
}

export const socket = new FakeSocket()

export const recorded = {
  toasts: [] as { message: string; level: string }[],
  charts: [] as { symbol: string; tf?: string }[],
}

const state = {
  notify: (message: string, level: string) => {
    recorded.toasts.push({ message, level })
  },
  openChart: (symbol: string, tf?: string) => {
    recorded.charts.push({ symbol, tf })
  },
}

export const useApp: any = Object.assign((sel: (s: typeof state) => unknown) => sel(state), { getState: () => state })
export type ToastLevel = 'info' | 'success' | 'warn' | 'error' | 'signal' | 'trade'
