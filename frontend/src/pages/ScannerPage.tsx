// Escáner de mercado: scans a whole TradingView market, scores every asset with the online model, runs NEXUS +
// Trade Gate on the best candidates (deep mode) and keeps an honest record of its own accuracy (feedback loop).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Layers, Radar, X } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { ScanRow, ScanRun, ServerMessage } from '../api/types'
import { socket } from '../api/ws'
import { fmtDateTime, fmtInt } from '../lib/format'
import { Badge, Button, Card, Disclaimer, Empty, ErrorBox, IconButton, Loading, Tabs, TextInput } from '../components/ui'
import { asTimeframe, isNum, marketLabel, useInterval, useMarkets, usePersistentState } from '../components/markets/opsUtils'
import ScanControls, { DEFAULT_SCAN_PARAMS, isScanParams, type ActiveRun, type ScanParams } from '../components/scanner/ScanControls'
import ScanTable from '../components/scanner/ScanTable'
import ScanRunsCard from '../components/scanner/ScanRunsCard'
import ScanAccuracyCard, { ScanHelpCard } from '../components/scanner/ScanAccuracyCard'
import {
  FIRST_DIR,
  SCAN_FILTERS,
  countByFilter,
  filterRows,
  normalizeRows,
  normalizeRuns,
  onRunFinished,
  runStatusInfo,
  sortRows,
  type ScanFilter,
  type SortKey,
  type SortState,
} from '../components/scanner/scanUtils'
import '../components/scanner/scanner.css'

type ScannerMsg = Extract<ServerMessage, { type: 'scanner' }>

interface View {
  run: ScanRun | null
  rows: ScanRow[]
}

const RUNS_LIMIT = 20
const POLL_RUNNING_MS = 4000
const DONE_VISIBLE_MS = 8000
const ADOPT_MAX_AGE_MS = 2 * 60 * 60 * 1000 // never follow a "running" run older than this (left over by a crash)
const isFilter = (v: unknown): v is ScanFilter => typeof v === 'string' && SCAN_FILTERS.some((f) => f.id === v)
const clampInt = (v: number, lo: number, hi: number) => Math.round(Math.min(hi, Math.max(lo, v)))

export default function ScannerPage() {
  const { markets, loading: marketsLoading } = useMarkets()
  const [params, setParams] = usePersistentState<ScanParams>('nexus.scanner.params.v1', DEFAULT_SCAN_PARAMS, isScanParams)
  const [filter, setFilter] = usePersistentState<ScanFilter>('nexus.scanner.filter.v1', 'all', isFilter)
  const [text, setText] = useState('')
  const [sort, setSort] = useState<SortState>({ key: 'rank', dir: 'asc' })

  const [selectedId, setSelectedState] = useState<number | null>(null) // null = follow the newest run
  const [view, setView] = useState<View | null>(null)
  const [viewLoading, setViewLoading] = useState(true)
  const [viewError, setViewError] = useState<string | null>(null)

  const [runs, setRuns] = useState<ScanRun[]>([])
  const [runsLoading, setRunsLoading] = useState(true)
  const [runsError, setRunsError] = useState<string | null>(null)

  const [active, setActiveState] = useState<ActiveRun | null>(null)
  const [starting, setStarting] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [newer, setNewer] = useState<number | null>(null)
  const [accKey, setAccKey] = useState(0)

  const alive = useRef(true)
  const activeRef = useRef<ActiveRun | null>(null)
  const viewRef = useRef<View | null>(null)
  const selectedRef = useRef<number | null>(null)
  const resSeq = useRef(0)
  const runsSeq = useRef(0)
  const runsTimer = useRef<number | null>(null)
  const recent = useRef(new Map<number, ScannerMsg>())
  viewRef.current = view

  const setSelectedId = useCallback((id: number | null) => {
    selectedRef.current = id
    setSelectedState(id)
  }, [])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      if (runsTimer.current) window.clearTimeout(runsTimer.current)
    }
  }, [])

  const updateActive = useCallback((fn: (a: ActiveRun | null) => ActiveRun | null) => {
    const next = fn(activeRef.current)
    activeRef.current = next
    setActiveState(next)
  }, [])

  // ------------------------------------------------------------------ loaders
  const loadResults = useCallback(async (runId: number | null) => {
    const id = ++resSeq.current
    setViewLoading(true)
    try {
      const res = await api.scanner.results(runId ?? undefined)
      if (!alive.current || id !== resSeq.current) return
      const run = res?.run && typeof res.run === 'object' && isNum(res.run.id) ? res.run : null
      setView({ run, rows: normalizeRows(res?.results) })
      setViewError(null)
      setNewer((n) => (n !== null && run && n === run.id ? null : n))
    } catch (err) {
      if (!alive.current || id !== resSeq.current) return
      setViewError(errorMessage(err))
    } finally {
      if (alive.current && id === resSeq.current) setViewLoading(false)
    }
  }, [])

  const loadRuns = useCallback(async (quiet = false) => {
    const id = ++runsSeq.current
    if (!quiet) setRunsLoading(true)
    try {
      const res = await api.scanner.runs(RUNS_LIMIT)
      if (!alive.current || id !== runsSeq.current) return
      setRuns(normalizeRuns(res?.runs))
      setRunsError(null)
    } catch (err) {
      if (!alive.current || id !== runsSeq.current) return
      setRunsError(errorMessage(err))
    } finally {
      if (alive.current && id === runsSeq.current) setRunsLoading(false)
    }
  }, [])

  const scheduleRunsReload = useCallback(() => {
    if (runsTimer.current) return
    runsTimer.current = window.setTimeout(() => {
      runsTimer.current = null
      if (alive.current) void loadRuns(true)
    }, 400)
  }, [loadRuns])

  useEffect(() => {
    void loadResults(null)
    void loadRuns()
  }, [loadResults, loadRuns])

  // ------------------------------------------------------------------ run lifecycle
  const finishRun = useCallback(
    (runId: number, message: string | null | undefined) => {
      const a = activeRef.current
      if (!a || a.id !== runId || a.status !== 'running') return
      updateActive((x) => (x ? { ...x, status: 'done', progress: 1, message: message || 'Completado' } : x))
      const next = onRunFinished(a, selectedRef.current, viewRef.current?.run?.id ?? null)
      if (next === 'offer') {
        // A background scan never replaces the run the user picked: the banner offers the new one instead.
        setNewer(runId)
      } else {
        if (next === 'show') setSelectedId(null)
        void loadResults(runId) // 'refresh': the run on screen (picked while running) gets its final results
      }
      scheduleRunsReload()
      setAccKey((k) => k + 1)
    },
    [loadResults, scheduleRunsReload, setSelectedId, updateActive],
  )

  const failRun = useCallback(
    (runId: number, message: string | null | undefined) => {
      const a = activeRef.current
      if (!a || a.id !== runId || a.status !== 'running') return
      const msg = message || 'El escaneo falló'
      updateActive((x) => (x ? { ...x, status: 'error', message: msg } : x))
      setRunError(msg)
      // The run on screen (picked while running, or the newest one followed) now shows its error instead of "sigue en
      // curso"; a failed run never replaces another run on screen.
      const ended = { id: runId, startedByUser: false, followed: false }
      if (onRunFinished(ended, selectedRef.current, viewRef.current?.run?.id ?? null) === 'refresh') void loadResults(runId)
      scheduleRunsReload()
    },
    [loadResults, scheduleRunsReload, updateActive],
  )

  const applyMsg = useCallback(
    (msg: ScannerMsg) => {
      if (msg.event === 'done') return finishRun(msg.run_id, msg.message)
      if (msg.event === 'error') return failRun(msg.run_id, msg.message)
      updateActive((a) =>
        a && a.id === msg.run_id && a.status === 'running'
          ? { ...a, progress: isNum(msg.progress) ? Math.max(a.progress, msg.progress) : a.progress, message: msg.message ?? a.message }
          : a,
      )
    },
    [failRun, finishRun, updateActive],
  )

  // Live progress over the socket ('scanner' is a default topic: listen only, never unsubscribe it).
  useEffect(() => {
    const off = socket.on('scanner', (msg) => {
      if (!msg || !isNum(msg.run_id)) return
      const buf = recent.current
      buf.set(msg.run_id, msg)
      if (buf.size > 50) buf.delete(buf.keys().next().value as number)
      const a = activeRef.current
      if (a && a.id === msg.run_id) {
        applyMsg(msg)
        return
      }
      if (msg.event === 'started' && (!a || a.status !== 'running')) {
        // A background (automatic) scan started: follow its progress too.
        updateActive(() => ({
          id: msg.run_id,
          market: '',
          tf: '',
          progress: msg.progress ?? 0,
          message: msg.message ?? 'En curso…',
          status: 'running',
          startedByUser: false,
        }))
      } else if (msg.event === 'done' || msg.event === 'error') {
        // A run this page was not following ended (e.g. one queued behind another scan): reload it when it is the run
        // on screen, otherwise only offer it (a failed run is not offered).
        const ended = { id: msg.run_id, startedByUser: false, followed: false }
        if (onRunFinished(ended, selectedRef.current, viewRef.current?.run?.id ?? null) === 'refresh') void loadResults(msg.run_id)
        else if (msg.event === 'done') setNewer(msg.run_id)
      }
      scheduleRunsReload()
    })
    return off
  }, [applyMsg, loadResults, scheduleRunsReload, updateActive])

  // Fill market/tf of adopted runs and detect completion when the socket is down (runs list polling).
  useEffect(() => {
    const a = activeRef.current
    if (!a || a.status !== 'running') {
      const newest = runs[0]
      if (!a && newest?.status === 'running' && isNum(newest.started_at) && Date.now() - newest.started_at < ADOPT_MAX_AGE_MS) {
        const r = newest
        const last = recent.current.get(r.id)
        updateActive(() => ({
          id: r.id,
          market: r.market,
          tf: r.tf,
          progress: last?.progress ?? 0,
          message: last?.message ?? r.message ?? 'En curso…',
          status: 'running',
          startedByUser: false,
        }))
      }
      return
    }
    const r = runs.find((x) => x.id === a.id)
    if (!r) return
    if (!a.market) updateActive((x) => (x && x.id === r.id ? { ...x, market: r.market, tf: r.tf } : x))
    if (r.status === 'done') finishRun(r.id, r.message)
    else if (r.status === 'error') failRun(r.id, r.message)
  }, [runs, failRun, finishRun, updateActive])

  useInterval(() => void loadRuns(true), active?.status === 'running' ? POLL_RUNNING_MS : null)

  // Hide the "completed" progress bar after a few seconds.
  useEffect(() => {
    if (active?.status !== 'done') return
    const id = active.id
    const t = window.setTimeout(() => updateActive((a) => (a && a.id === id && a.status === 'done' ? null : a)), DONE_VISIBLE_MS)
    return () => window.clearTimeout(t)
  }, [active, updateActive])

  // ------------------------------------------------------------------ actions
  const runScan = useCallback(async () => {
    setRunError(null)
    setStarting(true)
    try {
      const res = await api.scanner.run({
        market: params.market,
        tf: params.tf,
        limit: clampInt(params.limit ?? DEFAULT_SCAN_PARAMS.limit ?? 300, 5, 1000),
        deep: params.deep,
        deep_top: clampInt(params.deepTop ?? DEFAULT_SCAN_PARAMS.deepTop ?? 12, 1, 50),
      })
      if (!alive.current) return
      const id = res?.run_id
      if (!isNum(id)) throw new Error('El servidor no devolvió el identificador del escaneo')
      updateActive(() => ({ id, market: params.market, tf: params.tf, progress: 0, message: 'Iniciando…', status: 'running', startedByUser: true }))
      const buffered = recent.current.get(id) // the socket can be faster than the HTTP response
      if (buffered) applyMsg(buffered)
      scheduleRunsReload()
    } catch (err) {
      if (alive.current) setRunError(errorMessage(err))
    } finally {
      if (alive.current) setStarting(false)
    }
  }, [params, applyMsg, scheduleRunsReload, updateActive])

  const selectRun = useCallback(
    (id: number) => {
      setSelectedId(id)
      setNewer((n) => (n === id ? null : n))
      void loadResults(id)
    },
    [loadResults, setSelectedId],
  )

  const showLatest = useCallback(() => {
    setSelectedId(null)
    setNewer(null)
    void loadResults(null)
  }, [loadResults, setSelectedId])

  const onSort = useCallback(
    (key: SortKey) => setSort((s) => (s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: FIRST_DIR[key] })),
    [],
  )

  const rows = useMemo(() => view?.rows ?? [], [view])
  const counts = useMemo(() => countByFilter(rows), [rows])
  const shown = useMemo(() => sortRows(filterRows(rows, filter, text), sort), [rows, filter, text, sort])
  const run = view?.run ?? null
  const deepCount = counts.all - counts.shallow
  const runStatus = runStatusInfo(run?.status)

  // ------------------------------------------------------------------ render
  return (
    <div className="page scn-page">
      <div className="page-header">
        <div className="col gap-0">
          <h1 className="scn-title row gap-2">
            <Radar size={18} /> Escáner de mercado
          </h1>
          <span className="sub">Puntúa cada activo del mercado, pasa los mejores por NEXUS + filtro y verifica después sus predicciones.</span>
        </div>
      </div>

      <Card>
        <ScanControls
          value={params}
          onChange={setParams}
          markets={markets}
          marketsLoading={marketsLoading}
          starting={starting}
          active={active}
          error={runError}
          onRun={runScan}
        />
      </Card>

      {newer !== null && newer !== run?.id && (
        <div className="scn-banner" role="status">
          <Radar size={14} className="info" />
          <span className="grow">Hay un escaneo más reciente disponible (#{newer}).</span>
          <Button size="sm" variant="primary" onClick={showLatest}>
            Ver
          </Button>
          <IconButton icon={<X size={14} />} title="Descartar" size="sm" onClick={() => setNewer(null)} />
        </div>
      )}

      <div className="scn-layout">
        <Card
          flush
          title={
            <>
              Resultados{run ? ` · escaneo #${run.id}` : ''}
              {run && run.status !== 'done' && <Badge tone={runStatus.tone}>{runStatus.label}</Badge>}
              {run?.deep && (
                <Badge tone="purple" title="Activos con NEXUS completo + filtro">
                  <Layers size={11} /> {fmtInt(deepCount)} en profundidad
                </Badge>
              )}
            </>
          }
          sub={
            run
              ? `${marketLabel(run.market, markets)} · ${run.tf} · ${fmtInt(run.n_symbols)} activos · ${fmtDateTime(run.started_at)}${
                  selectedId === null ? ' · el más reciente' : ''
                }`
              : 'Sin escaneos todavía'
          }
          actions={
            <Button size="sm" variant="ghost" loading={viewLoading && !!view} onClick={() => void loadResults(selectedId)} title="Recargar resultados">
              Recargar
            </Button>
          }
        >
          {!view ? (
            viewError ? (
              <div style={{ padding: 12 }}>
                <ErrorBox error={viewError} onRetry={() => void loadResults(selectedId)} />
              </div>
            ) : (
              <Loading label="Cargando resultados…" />
            )
          ) : !run ? (
            <Empty icon={<Radar size={24} />} title="Aún no hay escaneos">
              <div className="col center gap-2">
                <span>Elige un mercado y lanza el primero. El escáner automático también corre en segundo plano.</span>
                <Button variant="primary" icon={<Radar size={14} />} onClick={runScan} loading={starting} disabled={active?.status === 'running'}>
                  Escanear ahora
                </Button>
              </div>
            </Empty>
          ) : (
            <>
              <div className="scn-toolbar">
                <Tabs
                  tabs={SCAN_FILTERS.map((f) => ({
                    id: f.id,
                    label: (
                      <span title={f.title}>
                        {f.label} <span className="scn-count">{fmtInt(counts[f.id])}</span>
                      </span>
                    ),
                  }))}
                  value={filter}
                  onChange={setFilter}
                />
                <TextInput value={text} onChange={setText} placeholder="Filtrar símbolo…" aria-label="Filtrar resultados por símbolo" />
              </div>
              {run.status === 'error' && run.message && (
                <div className="notice" style={{ margin: 12 }}>
                  Este escaneo terminó con error: {run.message}
                </div>
              )}
              {run.status === 'running' && (
                <div className="notice" style={{ margin: 12 }}>
                  Este escaneo sigue en curso; los resultados aparecerán al terminar.
                </div>
              )}
              {shown.length > 0 ? (
                <ScanTable key={run.id} rows={shown} sort={sort} onSort={onSort} tf={asTimeframe(run.tf)} />
              ) : (
                <Empty title={rows.length ? 'Ningún activo coincide con el filtro' : 'Este escaneo no tiene resultados'}>
                  {rows.length ? 'Prueba con otra pestaña o borra el texto de búsqueda.' : 'Lanza un escaneo nuevo para obtener resultados.'}
                </Empty>
              )}
              {viewError && <div className="tiny neg" style={{ padding: '6px 12px' }}>{viewError}</div>}
            </>
          )}
        </Card>

        <div className="scn-side">
          <ScanRunsCard
            runs={runs}
            loading={runsLoading}
            error={runsError}
            markets={markets}
            shownId={run?.id ?? null}
            followLatest={selectedId === null}
            onSelect={selectRun}
            onLatest={showLatest}
            onRefresh={() => void loadRuns()}
          />
          <ScanAccuracyCard reloadKey={accKey} />
          <ScanHelpCard />
        </div>
      </div>

      <Disclaimer />
    </div>
  )
}
