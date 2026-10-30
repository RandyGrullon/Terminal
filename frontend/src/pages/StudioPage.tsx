// PyChart Studio: Python (Pine-like) script editor + compiler with live preview, strategy backtest and API
// reference. Columns: scripts list | editor (toolbar, inputs, CodeMirror, problems/console/alerts) | preview.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { CircleCheck, CircleX, Hammer, LoaderCircle, Play, Save, TriangleAlert } from 'lucide-react'
import { ApiError, api, errorMessage } from '../api/client'
import type { BacktestResult, CompileResult, Script, ScriptError, ScriptRunResult, Timeframe } from '../api/types'
import { TIMEFRAMES } from '../api/types'
import { fmtInt } from '../lib/format'
import { useApp } from '../store'
import { Badge, Button, ErrorBox, Loading, Modal, Select, Tabs } from '../components/ui'
import BottomPanel, { type BottomTab } from '../components/studio/BottomPanel'
import CodeEditor, { type CodeEditorHandle } from '../components/studio/CodeEditor'
import type { Problem } from '../components/studio/diagnostics'
import { pruneOverrides } from '../components/studio/inputs'
import InputsPanel from '../components/studio/InputsPanel'
import PreviewChart from '../components/studio/PreviewChart'
import ReferencePanel from '../components/studio/ReferencePanel'
import ScriptList from '../components/studio/ScriptList'
import StrategyPanel from '../components/studio/StrategyPanel'
import { warmReference } from '../components/studio/reference'
import { idOf, keyOf, newKey, useStudio, type RunCache, type StudioState } from '../components/studio/studioStore'
import { TEMPLATES, TEMPLATE_NAMES, type TemplateKind } from '../components/studio/templates'
import '../components/studio/studio.css'

type RightTab = 'chart' | 'strategy' | 'reference'

const LIMITS = ['300', '500', '1000', '2000', '5000'] as const
const DEFAULT_META: CompileResult['meta'] = { title: '', overlay: true, kind: 'indicator' }
const arr = <T,>(v: T[] | null | undefined): T[] => (Array.isArray(v) ? v : [])

// ----------------------------------------------------------------------------- response normalization

function normalizeErrors(v: ScriptError[] | null | undefined): ScriptError[] {
  return arr(v)
    .filter((e) => e && typeof e.message === 'string')
    .map((e) => ({
      line: typeof e.line === 'number' && Number.isFinite(e.line) ? e.line : null,
      col: typeof e.col === 'number' && Number.isFinite(e.col) ? e.col : null,
      message: e.message,
      kind: e.kind ?? 'runtime',
    }))
}

function normalizeCompile(r: Partial<CompileResult> | null | undefined): CompileResult {
  return {
    ok: !!r?.ok,
    errors: normalizeErrors(r?.errors),
    warnings: arr(r?.warnings).map(String),
    inputs: arr(r?.inputs).filter((i) => i && typeof i.name === 'string'),
    meta: { ...DEFAULT_META, ...(r?.meta ?? {}) },
  }
}

function normalizeBacktest(b: BacktestResult | null | undefined): BacktestResult | null {
  if (!b || typeof b !== 'object' || !b.metrics) return null
  return {
    ...b,
    equity: arr(b.equity),
    benchmark: arr(b.benchmark),
    trades: arr(b.trades),
    markers: arr(b.markers),
    candles: arr(b.candles),
    ungated: b.ungated ?? null,
  }
}

function normalizeRun(r: Partial<ScriptRunResult> | null | undefined): ScriptRunResult {
  return {
    ok: !!r?.ok,
    errors: normalizeErrors(r?.errors),
    stdout: typeof r?.stdout === 'string' ? r.stdout : '',
    duration_ms: typeof r?.duration_ms === 'number' && Number.isFinite(r.duration_ms) ? r.duration_ms : 0,
    meta: { ...DEFAULT_META, ...(r?.meta ?? {}) },
    time: arr(r?.time),
    plots: arr(r?.plots)
      .filter((p) => p && Array.isArray(p.data))
      .map((p, i) => ({ ...p, id: p.id || `plot_${i}`, title: p.title ?? '', pane: typeof p.pane === 'number' ? p.pane : 0 })),
    markers: arr(r?.markers),
    hlines: arr(r?.hlines),
    bgcolors: arr(r?.bgcolors),
    alerts: arr(r?.alerts),
    strategy: normalizeBacktest(r?.strategy),
    candles: arr(r?.candles),
  }
}

function normalizeScript(s: Script): Script {
  return {
    id: s.id,
    name: typeof s.name === 'string' ? s.name : `Script ${s.id}`,
    description: typeof s.description === 'string' ? s.description : '',
    is_example: !!s.is_example,
    updated_at: typeof s.updated_at === 'number' ? s.updated_at : Date.now(),
    created_at: typeof s.created_at === 'number' ? s.created_at : Date.now(),
    code: typeof s.code === 'string' ? s.code : '',
  }
}

// ----------------------------------------------------------------------------- buffer helpers (store-level)

interface Buffer {
  name: string
  code: string
  description: string
  isNew: boolean
  isExample: boolean
}

function bufferOf(st: StudioState, key: string | null): Buffer | null {
  if (!key) return null
  const d = st.drafts[key]
  const s = st.saved[key]
  const id = idOf(key)
  const meta = id !== null ? st.list?.find((m) => m.id === id) : undefined
  const isExample = !!(s?.is_example ?? meta?.is_example)
  if (d) return { name: d.name, code: d.code, description: d.description, isNew: d.isNew, isExample }
  if (s) return { name: s.name, code: s.code, description: s.description, isNew: false, isExample }
  return null
}

/** Applies an edit to the active buffer, keeping the invariant "draft exists ⇔ buffer differs from saved". */
function updateBuffer(patch: { name?: string; code?: string }): void {
  const st = useStudio.getState()
  const key = st.active
  if (!key) return
  const d = st.drafts[key]
  const s = st.saved[key]
  const name = patch.name ?? d?.name ?? s?.name ?? ''
  const code = patch.code ?? d?.code ?? s?.code ?? ''
  if (d?.isNew) {
    st.putDraft(key, { ...d, name, code, updatedAt: Date.now() })
    return
  }
  if (s && s.name === name && s.code === code) {
    st.dropDraft(key)
    return
  }
  if (!d && !s) return
  st.putDraft(key, { name, code, description: d?.description ?? s?.description ?? '', isNew: false, updatedAt: Date.now() })
}

function existingNames(st: StudioState): Set<string> {
  return new Set([...(st.list ?? []).map((m) => m.name), ...Object.values(st.drafts).map((d) => d.name)])
}

function uniqueName(base: string, names: Set<string>): string {
  if (!names.has(base)) return base
  for (let i = 2; i < 1000; i++) if (!names.has(`${base} ${i}`)) return `${base} ${i}`
  return `${base} ${Date.now().toString(36)}`
}

/** Moves per-script caches (input overrides, last run / compile, editor session) to a new key. */
function rekeyCaches(from: string, to: string): void {
  const st = useStudio.getState()
  st.moveInputs(from, to)
  if (st.lastRun?.key === from) st.setLastRun({ ...st.lastRun, key: to })
  if (st.lastCompile?.key === from) st.setLastCompile({ ...st.lastCompile, key: to })
  editorSessions.set(to, editorSessions.get(from) ?? from)
}

/** Editor instance id per script key, so saving a new script (new key) does not remount the editor. */
const editorSessions = new Map<string, string>()

function pickDefault(st: StudioState): string | null {
  const lst = st.list ?? []
  const mine = lst.filter((m) => !m.is_example).sort((a, b) => b.updated_at - a.updated_at)
  if (mine.length) return keyOf(mine[0].id)
  const fresh = Object.entries(st.drafts)
    .filter(([, d]) => d.isNew)
    .sort((a, b) => b[1].updatedAt - a[1].updatedAt)
  if (fresh.length) return fresh[0][0]
  const examples = lst.filter((m) => m.is_example).sort((a, b) => a.name.localeCompare(b.name, 'es'))
  return examples.length ? keyOf(examples[0].id) : null
}

// ----------------------------------------------------------------------------- page

export default function StudioPage() {
  const globalSymbol = useApp((s) => s.symbol)
  const globalTf = useApp((s) => s.tf)
  const notify = useApp((s) => s.notify)

  const list = useStudio((s) => s.list)
  const saved = useStudio((s) => s.saved)
  const active = useStudio((s) => s.active)
  const drafts = useStudio((s) => s.drafts)
  const inputsAll = useStudio((s) => s.inputs)

  const [listLoading, setListLoading] = useState(false)
  const [listError, setListError] = useState<unknown>(null)
  const [scriptLoading, setScriptLoading] = useState(false)
  const [scriptError, setScriptError] = useState<unknown>(null)

  const [symbol, setSymbol] = useState(globalSymbol)
  const [symbolText, setSymbolText] = useState(globalSymbol)
  const [tf, setTf] = useState<Timeframe>(globalTf)
  const [limit, setLimit] = useState<(typeof LIMITS)[number]>('1000')

  const [compile, setCompile] = useState<CompileResult | null>(null)
  const [compiling, setCompiling] = useState(false)
  const [compileError, setCompileError] = useState<string | null>(null)
  const [run, setRun] = useState<RunCache | null>(null)
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [bottomTab, setBottomTab] = useState<BottomTab>('problems')
  const [bottomCollapsed, setBottomCollapsed] = useState(false)
  const [rightTab, setRightTab] = useState<RightTab>('chart')

  const rootRef = useRef<HTMLDivElement | null>(null)
  const editorRef = useRef<CodeEditorHandle | null>(null)
  const symbolRef = useRef(symbol)
  const tfRef = useRef(tf)
  const limitRef = useRef(limit)
  symbolRef.current = symbol
  tfRef.current = tf
  limitRef.current = limit
  const runSeq = useRef(0)
  const compileSeq = useRef(0)
  const openSeq = useRef(0)
  const compiledFor = useRef<string | null>(null)
  const savingRef = useRef(false)
  const inputTimer = useRef<number | null>(null)

  // ------------------------------------------------------------------ derived state of the open script
  const activeDraft = active ? drafts[active] : undefined
  const activeSaved = active ? saved[active] : undefined
  const activeId = idOf(active)
  const activeMeta = activeId !== null ? list?.find((m) => m.id === activeId) : undefined
  const isExample = !!(activeSaved?.is_example ?? activeMeta?.is_example)
  const isNew = !!activeDraft?.isNew
  const ready = !!activeDraft || !!activeSaved
  const name = activeDraft?.name ?? activeSaved?.name ?? activeMeta?.name ?? ''
  const code = activeDraft?.code ?? activeSaved?.code ?? ''
  const dirty = !!activeDraft
  const overrides = useMemo(() => (active ? inputsAll[active] ?? {} : {}), [active, inputsAll])
  const readyRef = useRef(ready)
  readyRef.current = ready

  // ------------------------------------------------------------------ server calls
  const refreshList = useCallback(async () => {
    setListLoading(true)
    try {
      const r = await api.scripts.list()
      const scripts = arr(r?.scripts).filter((s) => s && typeof s.id === 'number')
      const st = useStudio.getState()
      st.setList(scripts)
      for (const m of scripts) {
        const k = keyOf(m.id)
        const cached = st.saved[k]
        if (cached && k !== st.active && cached.updated_at !== m.updated_at) st.dropSaved(k)
      }
      setListError(null)
    } catch (e) {
      setListError(e)
    } finally {
      setListLoading(false)
    }
  }, [])

  const doCompile = useCallback(
    async (force: boolean) => {
      const st = useStudio.getState()
      const key = st.active
      const buf = bufferOf(st, key)
      if (!key || !buf) return
      const tag = `${key}\u0000${buf.code}`
      if (!force && compiledFor.current === tag) return
      const seq = ++compileSeq.current
      setCompiling(true)
      try {
        const res = normalizeCompile(await api.scripts.compile(buf.code))
        if (seq !== compileSeq.current) return
        compiledFor.current = tag
        setCompile(res)
        setCompileError(null)
        const s2 = useStudio.getState()
        s2.setLastCompile({ key, code: buf.code, result: res })
        const cur = s2.inputs[key]
        if (res.ok && cur) {
          const pruned = pruneOverrides(res.inputs, cur)
          if (Object.keys(pruned).length !== Object.keys(cur).length) s2.setInputs(key, pruned)
        }
        if (force) {
          if (res.errors.length) {
            setBottomTab('problems')
            setBottomCollapsed(false)
          }
          notify(res.errors.length ? `Compilación: ${res.errors.length} error(es)` : 'Compilado sin errores', res.errors.length ? 'warn' : 'success')
        }
      } catch (e) {
        if (seq !== compileSeq.current) return
        setCompileError(errorMessage(e))
        if (force) notify(`No se pudo compilar: ${errorMessage(e)}`, 'error')
      } finally {
        if (seq === compileSeq.current) setCompiling(false)
      }
    },
    [notify],
  )

  const doRun = useCallback(async () => {
    const st = useStudio.getState()
    const key = st.active
    const buf = bufferOf(st, key)
    if (!key || !buf) return
    const sym = symbolRef.current.trim().toUpperCase()
    if (!sym) {
      notify('Escribe un símbolo para ejecutar el script', 'warn')
      return
    }
    const tfv = tfRef.current
    const seq = ++runSeq.current
    setRunning(true)
    setRunError(null)
    try {
      const raw = await api.scripts.run({ code: buf.code, symbol: sym, tf: tfv, limit: Number(limitRef.current) || 1000, inputs: st.inputs[key] ?? {} })
      if (seq !== runSeq.current) return
      const result = normalizeRun(raw)
      const cache: RunCache = { key, viewKey: `${sym}|${tfv}`, symbol: sym, tf: tfv, result, at: Date.now() }
      setRun(cache)
      useStudio.getState().setLastRun(cache)
      if (!result.ok || result.errors.length) {
        setBottomTab('problems')
        setBottomCollapsed(false)
      }
      // The server answered: retry a compile that failed earlier (no-op when already compiled).
      void doCompile(false)
    } catch (e) {
      if (seq !== runSeq.current) return
      setRunError(errorMessage(e))
    } finally {
      if (seq === runSeq.current) setRunning(false)
    }
  }, [notify, doCompile])

  const openScript = useCallback(async (key: string) => {
    const st = useStudio.getState()
    if (st.active !== key) st.setActive(key)
    setScriptError(null)
    const id = idOf(key)
    const seq = ++openSeq.current
    if (id === null || st.saved[key]) {
      setScriptLoading(false)
      return
    }
    setScriptLoading(true)
    try {
      const script = normalizeScript(await api.scripts.get(id))
      useStudio.getState().putSaved(script)
    } catch (e) {
      const s2 = useStudio.getState()
      if (e instanceof ApiError && e.status === 404) {
        // Deleted elsewhere: keep any unsaved work as a new script, otherwise forget it.
        const d = s2.drafts[key]
        s2.setList((s2.list ?? []).filter((m) => m.id !== id))
        if (d) {
          const nk = newKey()
          s2.putDraft(nk, { ...d, isNew: true, updatedAt: Date.now() })
          s2.dropDraft(key)
          s2.moveInputs(key, nk)
          if (s2.active === key) s2.setActive(nk)
          notify('Ese script ya no existe en el servidor; tu borrador se conservó como script nuevo', 'warn')
        } else if (seq === openSeq.current && s2.active === key) {
          setScriptError(new Error('El script ya no existe. Elige otro de la lista.'))
        }
      } else if (seq === openSeq.current && s2.active === key) {
        setScriptError(e)
      }
    } finally {
      if (seq === openSeq.current) setScriptLoading(false)
    }
  }, [notify])

  const createNew = useCallback((kind: TemplateKind) => {
    const st = useStudio.getState()
    const key = newKey()
    st.putDraft(key, {
      name: uniqueName(TEMPLATE_NAMES[kind], existingNames(st)),
      code: TEMPLATES[kind],
      description: '',
      isNew: true,
      updatedAt: Date.now(),
    })
    st.setActive(key)
    setScriptError(null)
    setScriptLoading(false)
  }, [])

  const doSave = useCallback(async () => {
    const st = useStudio.getState()
    const key = st.active
    const buf = bufferOf(st, key)
    if (!key || !buf || savingRef.current) return
    const nm = buf.name.trim()
    if (!nm) {
      notify('El nombre del script no puede estar vacío', 'warn')
      return
    }
    if (!buf.isNew && !buf.isExample && !st.drafts[key]) {
      notify('No hay cambios que guardar')
      return
    }
    savingRef.current = true
    setSaving(true)
    try {
      const id = idOf(key)
      const createFrom = async (name: string, msg: (n: string) => string, dropOld: boolean) => {
        const created = normalizeScript(await api.scripts.create({ name, code: buf.code, description: buf.description }))
        const nk = keyOf(created.id)
        const s2 = useStudio.getState()
        s2.putSaved(created)
        if (dropOld) {
          rekeyCaches(key, nk)
          s2.dropDraft(key)
        }
        s2.setActive(nk)
        notify(msg(created.name), 'success')
      }
      if (buf.isNew || id === null) {
        await createFrom(nm, (n) => `Script «${n}» guardado`, true)
      } else if (buf.isExample) {
        const savedName = st.saved[key]?.name
        const copyName = nm === savedName ? uniqueName(`${nm} (copia)`, existingNames(st)) : nm
        await createFrom(copyName, (n) => `Ejemplo guardado como «${n}» en Mis scripts`, true)
      } else {
        try {
          const updated = normalizeScript(await api.scripts.update(id, { name: nm, code: buf.code }))
          const s2 = useStudio.getState()
          s2.putSaved(updated)
          const cur = s2.drafts[key]
          if (cur && cur.code === updated.code && cur.name.trim() === updated.name) s2.dropDraft(key)
          notify('Script guardado', 'success')
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) {
            await createFrom(nm, (n) => `El original ya no existía: se creó «${n}»`, true)
          } else throw e
        }
      }
      void refreshList()
    } catch (e) {
      notify(`No se pudo guardar: ${errorMessage(e)}`, 'error')
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }, [notify, refreshList])

  const doDuplicate = useCallback(async () => {
    const st = useStudio.getState()
    const key = st.active
    const buf = bufferOf(st, key)
    if (!key || !buf || savingRef.current) return
    const nm = uniqueName(`${buf.name.trim() || 'Script'} (copia)`, existingNames(st))
    if (buf.isNew) {
      const nk = newKey()
      st.putDraft(nk, { name: nm, code: buf.code, description: buf.description, isNew: true, updatedAt: Date.now() })
      if (st.inputs[key]) st.setInputs(nk, { ...st.inputs[key] })
      st.setActive(nk)
      return
    }
    savingRef.current = true
    setSaving(true)
    try {
      const created = normalizeScript(await api.scripts.create({ name: nm, code: buf.code, description: buf.description }))
      const s2 = useStudio.getState()
      s2.putSaved(created)
      if (s2.inputs[key]) s2.setInputs(keyOf(created.id), { ...s2.inputs[key] })
      s2.setActive(keyOf(created.id))
      notify(`Duplicado como «${created.name}»`, 'success')
      void refreshList()
    } catch (e) {
      notify(`No se pudo duplicar: ${errorMessage(e)}`, 'error')
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }, [notify, refreshList])

  const doDelete = useCallback(async () => {
    const st = useStudio.getState()
    const key = st.active
    const buf = bufferOf(st, key)
    if (!key || buf?.isExample) return
    const id = idOf(key)
    setDeleting(true)
    try {
      if (id !== null) {
        try {
          await api.scripts.remove(id)
        } catch (e) {
          if (!(e instanceof ApiError && e.status === 404)) throw e
        }
      }
      const s2 = useStudio.getState()
      s2.dropDraft(key)
      s2.dropSaved(key)
      s2.dropInputs(key)
      if (s2.lastRun?.key === key) s2.setLastRun(null)
      if (s2.lastCompile?.key === key) s2.setLastCompile(null)
      if (id !== null) s2.setList((s2.list ?? []).filter((m) => m.id !== id))
      setConfirmDelete(false)
      notify(buf?.isNew ? 'Borrador descartado' : `Script «${buf?.name ?? ''}» eliminado`, 'success')
      const next = pickDefault(useStudio.getState())
      if (next) void openScript(next)
      else createNew('indicator')
      if (id !== null) void refreshList()
    } catch (e) {
      notify(`No se pudo eliminar: ${errorMessage(e)}`, 'error')
    } finally {
      setDeleting(false)
    }
  }, [notify, openScript, createNew, refreshList])

  // ------------------------------------------------------------------ effects

  // Initial load: list + reopen the last script (or a sensible default).
  useEffect(() => {
    warmReference()
    let alive = true
    void (async () => {
      await refreshList()
      if (!alive) return
      const st = useStudio.getState()
      const valid = (k: string | null): k is string =>
        !!k && (!!st.drafts[k] || !!st.saved[k] || (st.list ?? []).some((m) => keyOf(m.id) === k))
      const key = valid(st.active) ? st.active : pickDefault(st)
      if (key) void openScript(key)
      else createNew('indicator')
    })()
    return () => {
      alive = false
    }
  }, [refreshList, openScript, createNew])

  // When a script becomes visible: compile right away and show its cached run or run it.
  useEffect(() => {
    if (!active || !ready) return
    runSeq.current++
    compileSeq.current++
    setRunning(false)
    setCompiling(false)
    setCompileError(null)
    setRunError(null)
    const st = useStudio.getState()
    const lc = st.lastCompile
    if (lc && lc.key === active) {
      setCompile(lc.result)
      compiledFor.current = `${active}\u0000${lc.code}`
    } else {
      setCompile(null)
      compiledFor.current = null
    }
    void doCompile(false)
    const lr = st.lastRun
    if (lr && lr.key === active && lr.symbol === symbolRef.current.trim().toUpperCase() && lr.tf === tfRef.current) setRun(lr)
    else {
      setRun(null)
      void doRun()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, ready])

  // Debounced live diagnostics while typing.
  useEffect(() => {
    if (!active || !ready) return
    const t = window.setTimeout(() => void doCompile(false), 800)
    return () => window.clearTimeout(t)
  }, [active, ready, code, doCompile])

  // Re-run when symbol / timeframe / bar count change.
  const paramsKey = `${symbol}|${tf}|${limit}`
  const prevParams = useRef(paramsKey)
  useEffect(() => {
    if (prevParams.current === paramsKey) return
    prevParams.current = paramsKey
    if (useStudio.getState().active && readyRef.current) void doRun()
  }, [paramsKey, doRun])

  // Follow the global symbol / timeframe chosen in the header.
  const prevGlobal = useRef(`${globalSymbol}|${globalTf}`)
  useEffect(() => {
    const k = `${globalSymbol}|${globalTf}`
    if (prevGlobal.current === k) return
    prevGlobal.current = k
    setSymbol(globalSymbol)
    setSymbolText(globalSymbol)
    setTf(globalTf)
  }, [globalSymbol, globalTf])

  useEffect(
    () => () => {
      if (inputTimer.current !== null) window.clearTimeout(inputTimer.current)
    },
    [],
  )

  // Keyboard shortcuts (capture phase so CodeMirror's own Mod-Enter binding does not fire as well).
  const actions = useRef({ run: doRun, compile: () => doCompile(true), save: doSave })
  actions.current = { run: doRun, compile: () => doCompile(true), save: doSave }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return
      const root = rootRef.current
      const target = e.target instanceof Node ? e.target : null
      if (root && target && target !== document.body && !root.contains(target)) return
      if (document.querySelector('.modal-backdrop')) return
      const k = e.key.toLowerCase()
      let fn: (() => unknown) | null = null
      if (k === 'enter' && !e.shiftKey) fn = actions.current.run
      else if (k === 's' && !e.shiftKey) fn = actions.current.save
      else if (k === 'b' && e.shiftKey) fn = actions.current.compile
      if (!fn) return
      e.preventDefault()
      e.stopPropagation()
      void fn()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [])

  // ------------------------------------------------------------------ handlers
  const onCodeChange = useCallback((value: string) => updateBuffer({ code: value }), [])

  const scheduleInputRun = useCallback(() => {
    if (inputTimer.current !== null) window.clearTimeout(inputTimer.current)
    inputTimer.current = window.setTimeout(() => {
      inputTimer.current = null
      void doRun()
    }, 500)
  }, [doRun])

  const onInputChange = useCallback(
    (inputName: string, value: unknown) => {
      const st = useStudio.getState()
      const key = st.active
      if (!key) return
      const def = (compile?.inputs ?? []).find((d) => d.name === inputName)
      const next = { ...(st.inputs[key] ?? {}) }
      if (def && value === def.default) delete next[inputName]
      else next[inputName] = value
      st.setInputs(key, next)
      scheduleInputRun()
    },
    [compile, scheduleInputRun],
  )

  const onInputsReset = useCallback(() => {
    const key = useStudio.getState().active
    if (!key) return
    useStudio.getState().dropInputs(key)
    scheduleInputRun()
  }, [scheduleInputRun])

  const applySymbol = () => {
    const s = symbolText.trim().toUpperCase()
    if (!s) {
      setSymbolText(symbol)
      return
    }
    setSymbolText(s)
    if (s !== symbol) setSymbol(s)
  }

  const onGoto = useCallback((line: number, col: number | null) => editorRef.current?.gotoLine(line, col), [])
  const onInsert = useCallback((text: string) => editorRef.current?.insert(text), [])
  const onOpen = useCallback((key: string) => void openScript(key), [openScript])
  const onRetryList = useCallback(() => void refreshList(), [refreshList])
  const onDuplicate = useCallback(() => void doDuplicate(), [doDuplicate])
  const onAskDelete = useCallback(() => setConfirmDelete(true), [])
  const onRetryRun = useCallback(() => void doRun(), [doRun])
  const onToggleBottom = useCallback(() => setBottomCollapsed((c) => !c), [])

  // ------------------------------------------------------------------ view model
  const runForActive = run && run.key === active ? run : null
  const problems = useMemo<Problem[]>(() => {
    const out: Problem[] = []
    const seen = new Set<string>()
    const add = (e: ScriptError, origin: Problem['origin']) => {
      const k = `${e.line}|${e.col}|${e.message}`
      if (seen.has(k)) return
      seen.add(k)
      out.push({ ...e, origin })
    }
    compile?.errors.forEach((e) => add(e, 'compile'))
    runForActive?.result.errors.forEach((e) => add(e, 'run'))
    return out
  }, [compile, runForActive])
  const warnings = useMemo(() => compile?.warnings ?? [], [compile])

  const kind = compile?.meta.kind ?? runForActive?.result.meta.kind ?? 'indicator'
  const isStrategy = kind === 'strategy'
  const effectiveRight: RightTab = rightTab === 'strategy' && !isStrategy ? 'chart' : rightTab
  const strategy = runForActive?.result.strategy ?? null

  const runInfo = runForActive
    ? `${fmtInt(runForActive.result.candles.length)} velas · ${runForActive.symbol} ${runForActive.tf} · ${Math.round(runForActive.result.duration_ms)} ms`
    : null

  const nErrors = compile?.errors.length ?? 0
  const status = compiling ? (
    <span className="studio-status">
      <LoaderCircle size={12} className="studio-spin" /> Compilando…
    </span>
  ) : compileError ? (
    <span className="studio-status warn" title={`Compilador no disponible: ${compileError}`}>
      <TriangleAlert size={12} /> Sin compilador
    </span>
  ) : compile ? (
    nErrors ? (
      <span className="studio-status neg">
        <CircleX size={12} /> {nErrors} error(es)
      </span>
    ) : (
      <span className="studio-status pos">
        <CircleCheck size={12} /> Compilado
      </span>
    )
  ) : null

  const rightTabs: { id: RightTab; label: ReactNode }[] = [
    { id: 'chart', label: 'Gráfico' },
    ...(isStrategy
      ? [
          {
            id: 'strategy' as RightTab,
            label: (
              <>
                Estrategia
                {strategy && <span className="studio-count accent">{fmtInt(strategy.metrics.trades)}</span>}
              </>
            ),
          },
        ]
      : []),
    { id: 'reference', label: 'Referencia' },
  ]

  const canDelete = !!active && ready && !isExample
  const saveLabel = isExample ? 'Guardar como copia' : 'Guardar'

  return (
    <div className="page-full" ref={rootRef}>
      <div className="studio">
        <ScriptList
          list={list}
          drafts={drafts}
          active={active}
          loading={listLoading}
          error={listError}
          canDelete={canDelete}
          canDuplicate={!!active && ready && !saving}
          onOpen={onOpen}
          onNew={createNew}
          onDuplicate={onDuplicate}
          onDelete={onAskDelete}
          onRetry={onRetryList}
        />

        <section className="studio-main">
          <div className="studio-toolbar">
            <div className="studio-toolbar-row">
              <input
                className="input studio-name"
                value={name}
                onChange={(e) => updateBuffer({ name: e.target.value })}
                placeholder="Nombre del script"
                aria-label="Nombre del script"
                disabled={!ready}
                maxLength={120}
              />
              {dirty && <span className="studio-dot" title="Cambios sin guardar" />}
              {isExample && (
                <Badge tone="info" title="Los ejemplos no se modifican: al guardar se crea una copia en Mis scripts">
                  Ejemplo
                </Badge>
              )}
              {isNew && <Badge tone="warn">Sin guardar</Badge>}
              {compile && <Badge tone={isStrategy ? 'purple' : 'accent'}>{isStrategy ? 'Estrategia' : 'Indicador'}</Badge>}
              <Button
                size="sm"
                variant={dirty || isNew ? 'primary' : 'default'}
                icon={<Save size={14} />}
                onClick={() => void doSave()}
                loading={saving}
                disabled={!ready || (!dirty && !isExample && !isNew)}
                title={`${saveLabel} (Ctrl+S)`}
              >
                {saveLabel}
              </Button>
            </div>
            <div className="studio-toolbar-row">
              <Button size="sm" icon={<Hammer size={14} />} onClick={() => void doCompile(true)} disabled={!ready} title="Compilar (Ctrl+Shift+B)">
                Compilar
              </Button>
              <Button size="sm" variant="success" icon={<Play size={14} />} onClick={() => void doRun()} loading={running} disabled={!ready} title="Ejecutar (Ctrl+Enter)">
                Ejecutar
              </Button>
              <input
                className="input mono studio-symbol"
                value={symbolText}
                onChange={(e) => setSymbolText(e.target.value)}
                onBlur={applySymbol}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    applySymbol()
                  }
                }}
                placeholder="BINANCE:BTCUSDT"
                aria-label="Símbolo"
                title="Símbolo para la vista previa (Enter para aplicar)"
              />
              <Select value={tf} onChange={(v) => setTf(v)} options={TIMEFRAMES} title="Temporalidad" />
              <Select
                value={limit}
                onChange={(v) => setLimit(v)}
                options={LIMITS.map((l) => ({ value: l, label: `${l} velas` }))}
                title="Velas a cargar"
              />
              <div className="grow" />
              {status}
            </div>
          </div>

          {ready && <InputsPanel defs={compile?.inputs ?? []} overrides={overrides} onChange={onInputChange} onReset={onInputsReset} />}

          <div className="studio-editor-wrap">
            {ready && active ? (
              <CodeEditor key={editorSessions.get(active) ?? active} ref={editorRef} value={code} onChange={onCodeChange} problems={problems} />
            ) : (
              <div className="studio-editor-overlay">
                {scriptError ? (
                  <div style={{ width: '100%', maxWidth: 440 }}>
                    <ErrorBox error={scriptError} onRetry={active ? () => void openScript(active) : undefined} />
                  </div>
                ) : scriptLoading || list === null ? (
                  <Loading label="Abriendo script…" />
                ) : (
                  <Loading label="Preparando editor…" />
                )}
              </div>
            )}
          </div>

          <BottomPanel
            tab={bottomTab}
            onTab={setBottomTab}
            collapsed={bottomCollapsed}
            onToggle={onToggleBottom}
            problems={problems}
            warnings={warnings}
            run={runForActive?.result ?? null}
            runInfo={runInfo}
            compileError={compileError}
            onGoto={onGoto}
          />
        </section>

        <section className="studio-preview">
          <Tabs tabs={rightTabs} value={effectiveRight} onChange={setRightTab} />
          <div className="studio-preview-body">
            <div style={{ display: effectiveRight === 'chart' ? 'flex' : 'none', flex: 1, minHeight: 0, flexDirection: 'column' }}>
              <PreviewChart
                result={runForActive?.result ?? null}
                viewKey={runForActive?.viewKey ?? ''}
                running={running}
                error={runError}
                symbol={runForActive?.symbol ?? symbol}
                tf={runForActive?.tf ?? tf}
                onRetry={onRetryRun}
              />
            </div>
            {effectiveRight === 'strategy' && <StrategyPanel result={strategy} />}
            {effectiveRight === 'reference' && <ReferencePanel onInsert={onInsert} />}
          </div>
        </section>
      </div>

      <Modal
        open={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        title={isNew ? 'Descartar borrador' : 'Eliminar script'}
        width={440}
        footer={
          <>
            <Button onClick={() => setConfirmDelete(false)}>Cancelar</Button>
            <Button variant="danger" loading={deleting} onClick={() => void doDelete()}>
              {isNew ? 'Descartar' : 'Eliminar'}
            </Button>
          </>
        }
      >
        <p style={{ margin: 0 }}>
          {isNew ? (
            <>
              ¿Descartar el borrador <strong>{name || 'sin título'}</strong>? Todavía no se ha guardado y se perderá.
            </>
          ) : (
            <>
              ¿Eliminar <strong>{name}</strong> definitivamente? Esta acción no se puede deshacer.
            </>
          )}
        </p>
      </Modal>
    </div>
  )
}
