// PyChart Studio state that must outlive the page component (switching pages unmounts it).
// Unsaved drafts, the open script and input overrides are mirrored to localStorage (per-viewer convenience,
// every access wrapped in try/catch); server data and last results are kept in memory only.
import { create } from 'zustand'
import type { CompileResult, Script, ScriptMeta, ScriptRunResult, Timeframe } from '../../api/types'

/** Editor buffer that differs from the saved script (or a brand-new script not yet saved). */
export interface Draft {
  name: string
  code: string
  description: string
  isNew: boolean
  updatedAt: number
}

export interface RunCache {
  key: string
  viewKey: string // `${symbol}|${tf}` the run was made for
  symbol: string
  tf: Timeframe
  result: ScriptRunResult
  at: number
}

export interface CompileCache {
  key: string
  code: string
  result: CompileResult
}

export type InputOverrides = Record<string, unknown>

interface Persisted {
  v: 1
  active: string | null
  drafts: Record<string, Draft>
  inputs: Record<string, InputOverrides>
}

export interface StudioState {
  list: ScriptMeta[] | null
  saved: Record<string, Script>
  active: string | null
  drafts: Record<string, Draft>
  inputs: Record<string, InputOverrides>
  lastRun: RunCache | null
  lastCompile: CompileCache | null
  setList: (list: ScriptMeta[]) => void
  putSaved: (script: Script) => void
  dropSaved: (key: string) => void
  setActive: (key: string | null) => void
  putDraft: (key: string, draft: Draft) => void
  dropDraft: (key: string) => void
  setInputs: (key: string, values: InputOverrides) => void
  dropInputs: (key: string) => void
  moveInputs: (from: string, to: string) => void
  setLastRun: (run: RunCache | null) => void
  setLastCompile: (c: CompileCache | null) => void
}

const STORAGE_KEY = 'nexus.studio.v1'

export const keyOf = (id: number): string => `id:${id}`

export function idOf(key: string | null | undefined): number | null {
  if (!key || !key.startsWith('id:')) return null
  const n = Number(key.slice(3))
  return Number.isInteger(n) && n > 0 ? n : null
}

export function newKey(): string {
  return `new:${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`
}

function isDraft(v: unknown): v is Draft {
  if (!v || typeof v !== 'object') return false
  const d = v as Record<string, unknown>
  return typeof d.name === 'string' && typeof d.code === 'string'
}

function loadPersisted(): Persisted {
  const empty: Persisted = { v: 1, active: null, drafts: {}, inputs: {} }
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return empty
    const data = JSON.parse(raw) as Partial<Persisted> | null
    if (!data || typeof data !== 'object') return empty
    const drafts: Record<string, Draft> = {}
    for (const [k, v] of Object.entries(data.drafts ?? {})) {
      if (!isDraft(v)) continue
      drafts[k] = {
        name: v.name,
        code: v.code,
        description: typeof v.description === 'string' ? v.description : '',
        isNew: !!v.isNew || k.startsWith('new:'),
        updatedAt: typeof v.updatedAt === 'number' ? v.updatedAt : Date.now(),
      }
    }
    const inputs: Record<string, InputOverrides> = {}
    for (const [k, v] of Object.entries(data.inputs ?? {})) {
      if (v && typeof v === 'object' && !Array.isArray(v)) inputs[k] = v as InputOverrides
    }
    return { v: 1, active: typeof data.active === 'string' ? data.active : null, drafts, inputs }
  } catch {
    return empty
  }
}

const initial = loadPersisted()

export const useStudio = create<StudioState>((set) => ({
  list: null,
  saved: {},
  active: initial.active,
  drafts: initial.drafts,
  inputs: initial.inputs,
  lastRun: null,
  lastCompile: null,
  setList: (list) => set({ list }),
  putSaved: (script) => set((s) => ({ saved: { ...s.saved, [keyOf(script.id)]: script } })),
  dropSaved: (key) =>
    set((s) => {
      if (!(key in s.saved)) return {}
      const saved = { ...s.saved }
      delete saved[key]
      return { saved }
    }),
  setActive: (active) => set({ active }),
  putDraft: (key, draft) => set((s) => ({ drafts: { ...s.drafts, [key]: draft } })),
  dropDraft: (key) =>
    set((s) => {
      if (!(key in s.drafts)) return {}
      const drafts = { ...s.drafts }
      delete drafts[key]
      return { drafts }
    }),
  setInputs: (key, values) =>
    set((s) => {
      const inputs = { ...s.inputs }
      if (Object.keys(values).length) inputs[key] = values
      else delete inputs[key]
      return { inputs }
    }),
  dropInputs: (key) =>
    set((s) => {
      if (!(key in s.inputs)) return {}
      const inputs = { ...s.inputs }
      delete inputs[key]
      return { inputs }
    }),
  moveInputs: (from, to) =>
    set((s) => {
      if (!(from in s.inputs)) return {}
      const inputs = { ...s.inputs, [to]: s.inputs[from] }
      delete inputs[from]
      return { inputs }
    }),
  setLastRun: (lastRun) => set({ lastRun }),
  setLastCompile: (lastCompile) => set({ lastCompile }),
}))

// ----------------------------------------------------------------------------- persistence (debounced)

let saveTimer: number | null = null

function writeNow(): void {
  saveTimer = null
  const s = useStudio.getState()
  const data: Persisted = { v: 1, active: s.active, drafts: s.drafts, inputs: s.inputs }
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data))
  } catch {
    /* storage unavailable or full: drafts stay in memory for this session */
  }
}

useStudio.subscribe((s, prev) => {
  if (s.active === prev.active && s.drafts === prev.drafts && s.inputs === prev.inputs) return
  if (saveTimer !== null) window.clearTimeout(saveTimer)
  saveTimer = window.setTimeout(writeNow, 400)
})

window.addEventListener('beforeunload', () => {
  if (saveTimer !== null) {
    window.clearTimeout(saveTimer)
    writeNow()
  }
})
