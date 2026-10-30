// Main chart workspace: toolbar (symbol, price, history quality, gate verdicts, layer toggles, indicators, view),
// the NEXUS chart or the official TradingView chart, and the decision sidebar (signal + "¿Tomo esta operación?"
// + watchlist). Follows the global symbol / timeframe from the header.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ChartCandlestick,
  ChartLine,
  Database,
  FlaskConical,
  History,
  KeyRound,
  Maximize2,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  TriangleAlert,
  Tv,
} from 'lucide-react'
import { api } from '../api/client'
import type { Candle, GateResult, HistoryKind, NexusCurrent, SymbolDetail } from '../api/types'
import IndicatorsDialog from '../components/chart/IndicatorsDialog'
import NexusChart, { type ChartToggles, type NexusChartHandle } from '../components/chart/NexusChart'
import TradingViewWidget from '../components/chart/TradingViewWidget'
import { isFiniteNumber } from '../components/chart/chartUtils'
import { useIndicators } from '../components/chart/indicatorsModel'
import { useMarketData } from '../components/chart/useMarketData'
import SignalPanel from '../components/nexus/SignalPanel'
import TradeCheck from '../components/nexus/TradeCheck'
import Watchlist from '../components/nexus/Watchlist'
import { Badge, Button, Empty, ErrorBox, IconButton, Loading, Spinner } from '../components/ui'
import { VERDICT_LABEL, fmtPct, fmtPrice, signTone, splitSymbol, verdictTone, type Tone } from '../lib/format'
import { useApp } from '../store'
import '../components/chart/chart.css'

type View = 'nexus' | 'tv'

interface Prefs {
  toggles: ChartToggles
  view: View
  sidebar: boolean
}

const PREFS_KEY = 'nexus.chart.prefs.v1'

const DEFAULT_TOGGLES: ChartToggles = {
  signals: true,
  rejected: true,
  levels: true,
  cone: true,
  volume: true,
  ema: true,
  scorePane: true,
}

const TOGGLES: { key: keyof ChartToggles; label: string; title: string }[] = [
  { key: 'signals', label: 'Señales NEXUS', title: 'Flechas de compra / venta, objetivos alcanzados y salidas del motor NEXUS' },
  { key: 'rejected', label: 'Rechazadas', title: 'Señales que el filtro NO dejó tomar (pasa el cursor por encima para ver el motivo)' },
  { key: 'levels', label: 'Niveles SL/TP', title: 'Líneas «hasta aquí»: stop loss y objetivos de cada operación' },
  { key: 'cone', label: 'Cono', title: 'Cono de predicción (p10 – p90) después de la última vela' },
  { key: 'volume', label: 'Volumen', title: 'Volumen en la parte inferior del gráfico' },
  { key: 'ema', label: 'EMAs', title: 'Medias móviles que usa NEXUS (rápida, lenta y de tendencia)' },
  { key: 'scorePane', label: 'Panel NEXUS', title: 'Panel inferior con la puntuación NEXUS y la probabilidad de subida' },
]

const HISTORY_INFO: Record<HistoryKind, { label: string; tone: Tone; hint: string }> = {
  full: { label: 'completo', tone: 'pos', hint: 'Historial completo del exchange o proveedor.' },
  recorder: {
    label: 'local',
    tone: 'warn',
    hint: 'Historial construido localmente con instantáneas en vivo de TradingView: crece con el tiempo.',
  },
  keyed: { label: 'clave API', tone: 'info', hint: 'Historial obtenido con tu clave API del proveedor de datos.' },
  synthetic: { label: 'sintético', tone: 'purple', hint: 'Datos sintéticos del simulador de mercado de NEXUS.' },
  none: { label: 'sin datos', tone: 'neg', hint: 'No hay historial de velas para este símbolo.' },
}

function HistoryIcon({ kind }: { kind: HistoryKind }) {
  if (kind === 'recorder') return <History size={11} />
  if (kind === 'keyed') return <KeyRound size={11} />
  if (kind === 'synthetic') return <FlaskConical size={11} />
  if (kind === 'none') return <TriangleAlert size={11} />
  return <Database size={11} />
}

function loadPrefs(): Prefs {
  const fallback: Prefs = { toggles: { ...DEFAULT_TOGGLES }, view: 'nexus', sidebar: true }
  try {
    const raw = localStorage.getItem(PREFS_KEY)
    if (!raw) return fallback
    const p = JSON.parse(raw) as Partial<Prefs> | null
    const toggles = { ...DEFAULT_TOGGLES }
    const t = (p?.toggles ?? {}) as Partial<Record<keyof ChartToggles, unknown>>
    for (const k of Object.keys(DEFAULT_TOGGLES) as (keyof ChartToggles)[]) {
      const v = t[k]
      if (typeof v === 'boolean') toggles[k] = v
    }
    return { toggles, view: p?.view === 'tv' ? 'tv' : 'nexus', sidebar: p?.sidebar !== false }
  } catch {
    return fallback
  }
}

function useSymbolDetail(symbol: string): SymbolDetail | null {
  const [state, setState] = useState<{ symbol: string; detail: SymbolDetail } | null>(null)
  useEffect(() => {
    let alive = true
    api
      .symbol(symbol)
      .then((d) => {
        if (alive && d && typeof d === 'object') setState({ symbol, detail: d })
      })
      .catch(() => undefined)
    return () => {
      alive = false
    }
  }, [symbol])
  return state && state.symbol === symbol ? state.detail : null
}

function GateChip({ label, gate }: { label: string; gate: GateResult | undefined }) {
  if (!gate || !gate.verdict) return null
  const tone = verdictTone(gate.verdict)
  return (
    <span className={`chart-gate-chip ${tone}`} title={`${label}: ${gate.summary || gate.verdict_label}`}>
      {label}
      <b>{gate.verdict_label || VERDICT_LABEL[gate.verdict]}</b>
      {isFiniteNumber(gate.score) && <span className="mono">{Math.round(gate.score)}</span>}
    </span>
  )
}

export default function ChartPage() {
  const symbol = useApp((s) => s.symbol)
  const tf = useApp((s) => s.tf)
  const [prefs, setPrefs] = useState<Prefs>(loadPrefs)
  const prefsRef = useRef(prefs)
  prefsRef.current = prefs
  const chartRef = useRef<NexusChartHandle | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [focusUid, setFocusUid] = useState<string | null>(null)
  const [preview, setPreview] = useState<GateResult | null>(null)

  const onCandle = useCallback((c: Candle) => chartRef.current?.updateCandle(c), [])
  const md = useMarketData(symbol, tf, onCandle)
  const candles = md.candles?.candles ?? null
  const ind = useIndicators({
    symbol,
    tf,
    ready: !!candles && candles.length > 0,
    dataVersion: md.dataVersion,
    barVersion: md.barVersion,
  })
  const detail = useSymbolDetail(symbol)

  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs))
    } catch {
      /* storage unavailable: preferences live for this session only */
    }
  }, [prefs])

  useEffect(() => {
    setPreview(null)
  }, [symbol, tf])

  const openIndicators = useCallback((uid: string | null = null) => {
    setFocusUid(uid)
    setDialogOpen(true)
  }, [])
  const closeIndicators = useCallback(() => setDialogOpen(false), [])
  const setView = useCallback((view: View) => setPrefs((p) => ({ ...p, view })), [])
  const openTradingView = useCallback(() => setView('tv'), [setView])
  const toggle = useCallback(
    (key: keyof ChartToggles) => setPrefs((p) => ({ ...p, toggles: { ...p.toggles, [key]: !p.toggles[key] } })),
    [],
  )

  // "i" opens the indicators dialog (not while typing, not when another dialog is open).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key !== 'i' && e.key !== 'I') || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return
      const el = e.target as HTMLElement | null
      if (el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.closest('[contenteditable], .cm-editor'))) return
      if (document.querySelector('.modal-backdrop')) return
      if (prefsRef.current.view !== 'nexus') return
      e.preventDefault()
      openIndicators(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [openIndicators])

  const { retry: retryIndicator, reloadCatalog } = ind
  const indicatorItems = ind.items
  const onRetryIndicator = useCallback(
    (uid: string) => {
      const it = indicatorItems.find((i) => i.uid === uid)
      if (it && !it.spec) reloadCatalog()
      else retryIndicator(uid)
    },
    [indicatorItems, reloadCatalog, retryIndicator],
  )

  // ------------------------------------------------------------------ derived values
  const nexus = md.nexus
  const current: NexusCurrent | null = nexus?.ok ? (nexus.current ?? null) : null
  const nexusParams = nexus?.params
  const entryThreshold = useMemo(() => {
    const v = Number(nexusParams?.entry_threshold)
    return Number.isFinite(v) && v > 0 && v < 1 ? v : 0.35
  }, [nexusParams])

  const { ticker, exchange } = splitSymbol(symbol)
  const lastCandle = candles && candles.length ? candles[candles.length - 1] : null
  const quote = detail?.quote ?? null
  const price = md.live?.price ?? lastCandle?.close ?? (isFiniteNumber(quote?.price) ? quote.price : null)

  const change = useMemo(() => {
    if (price === null) return null
    if (quote && isFiniteNumber(quote.price) && isFiniteNumber(quote.change) && quote.price - quote.change > 0) {
      const prevClose = quote.price - quote.change
      return { pct: (price / prevClose - 1) * 100, basis: 'Variación del día' }
    }
    if (quote && isFiniteNumber(quote.change_pct) && quote.price === price) return { pct: quote.change_pct, basis: 'Variación del día' }
    if (candles && candles.length >= 2) {
      const live = md.live
      const prev = live && lastCandle && live.time > lastCandle.time ? lastCandle.close : candles[candles.length - 2].close
      if (prev > 0) return { pct: (price / prev - 1) * 100, basis: 'Variación respecto al cierre de la vela anterior' }
    }
    return null
  }, [price, quote, candles, lastCandle, md.live])

  const history = md.candles?.history ?? null
  const historyInfo = history ? (HISTORY_INFO[history] ?? null) : null
  const historyTitle = historyInfo
    ? [historyInfo.hint, md.candles?.provider ? `Proveedor: ${md.candles.provider}.` : '', md.candles?.note ?? '']
        .filter(Boolean)
        .join('\n')
    : ''

  const isNexusView = prefs.view === 'nexus'
  const activeCount = ind.items.length

  // ------------------------------------------------------------------ chart overlays
  // Memoized so the memoized chart is not re-rendered by unrelated page updates (live ticks).
  const candlesError = md.candlesError
  const candlesNote = md.candles?.note ?? null
  const reload = md.reload
  const overlay = useMemo(() => {
    if (!candles && candlesError) {
      return (
        <div className="chart-overlay">
          <div className="chart-overlay-box">
            <ErrorBox error={candlesError} onRetry={reload} />
            <Button size="sm" icon={<Tv size={13} />} onClick={openTradingView}>
              Ver {ticker} en TradingView
            </Button>
          </div>
        </div>
      )
    }
    if (!candles) {
      return (
        <div className="chart-overlay">
          <Loading label={`Cargando ${ticker} · ${tf}…`} />
        </div>
      )
    }
    if (candles.length === 0) {
      return (
        <div className="chart-overlay">
          <div className="chart-overlay-box">
            <Empty icon={<ChartCandlestick size={22} />} title={`Sin velas para ${ticker} en ${tf}`}>
              {candlesNote || 'El proveedor no devolvió historial para este símbolo y temporalidad.'}
            </Empty>
            <Button size="sm" icon={<Tv size={13} />} onClick={openTradingView}>
              Ver en TradingView
            </Button>
          </div>
        </div>
      )
    }
    return null
  }, [candles, candlesError, candlesNote, reload, openTradingView, ticker, tf])

  return (
    <div className="page-full chart-page">
      <div className="chart-toolbar">
        <div className="chart-tb-row">
          <div className="chart-sym" title={detail?.description ? `${symbol} · ${detail.description}` : symbol}>
            <span className="chart-sym-ticker">{ticker}</span>
            {exchange && <span className="chart-sym-ex">{exchange}</span>}
            {detail?.description && <span className="chart-sym-desc">{detail.description}</span>}
          </div>
          <div className="chart-px">
            <span className="chart-price mono">{fmtPrice(price)}</span>
            {change && (
              <span className={`chart-chg mono ${signTone(change.pct) === 'muted' ? 'muted' : signTone(change.pct)}`} title={change.basis}>
                {fmtPct(change.pct)}
              </span>
            )}
          </div>
          {historyInfo && history && (
            <Badge tone={historyInfo.tone} title={historyTitle}>
              <HistoryIcon kind={history} /> {historyInfo.label}
            </Badge>
          )}
          {current?.gate && (
            <span className="row gap-1" aria-label="Filtro de operaciones">
              <GateChip label="Largo" gate={current.gate.long} />
              <GateChip label="Corto" gate={current.gate.short} />
            </span>
          )}
          {(md.nexusLoading || md.nexusRefreshing || (md.candlesLoading && !!candles)) && (
            <span className="row gap-1 tiny muted nowrap" title={md.nexusLoading ? 'Calculando NEXUS…' : 'Actualizando datos…'}>
              <Spinner /> {md.nexusLoading ? 'NEXUS…' : ''}
            </span>
          )}
          <div className="chart-toolbar-spacer" />
          <div className="row gap-1">
            <div className="btn-group" role="group" aria-label="Vista del gráfico">
              <button
                type="button"
                className={`btn btn-sm ${isNexusView ? 'active' : ''}`}
                onClick={() => setView('nexus')}
                title="Gráfico NEXUS con señales, filtro, niveles y tus indicadores"
              >
                <ChartCandlestick size={13} /> NEXUS
              </button>
              <button
                type="button"
                className={`btn btn-sm ${!isNexusView ? 'active' : ''}`}
                onClick={() => setView('tv')}
                title="Gráfico oficial de TradingView: cualquier activo y todos sus indicadores"
              >
                <Tv size={13} /> TradingView
              </button>
            </div>
            <IconButton size="sm" icon={<RefreshCw size={14} />} title="Recargar velas y NEXUS" onClick={md.reload} />
            <IconButton
              size="sm"
              icon={prefs.sidebar ? <PanelRightClose size={15} /> : <PanelRightOpen size={15} />}
              title={prefs.sidebar ? 'Ocultar el panel lateral' : 'Mostrar el panel lateral'}
              onClick={() => setPrefs((p) => ({ ...p, sidebar: !p.sidebar }))}
            />
          </div>
        </div>

        {isNexusView && (
          <div className="chart-tb-row chart-tb-layers">
            <div className="chart-toggles" role="group" aria-label="Capas del gráfico">
              {TOGGLES.map((t) => (
                <button
                  key={t.key}
                  type="button"
                  className={`chart-chip ${prefs.toggles[t.key] ? 'on' : ''}`}
                  title={t.title}
                  aria-pressed={prefs.toggles[t.key]}
                  onClick={() => toggle(t.key)}
                >
                  <span className="dot" />
                  {t.label}
                </button>
              ))}
            </div>
            <div className="chart-toolbar-spacer" />
            <Button
              size="sm"
              icon={ind.computing ? <Spinner /> : <ChartLine size={14} />}
              title="Añadir o editar indicadores (atajo: i)"
              onClick={() => openIndicators(null)}
            >
              Indicadores{activeCount ? ` (${activeCount})` : ''}
            </Button>
            <IconButton size="sm" icon={<Maximize2 size={14} />} title="Restablecer la vista (velas recientes)" onClick={() => chartRef.current?.resetView()} />
          </div>
        )}
      </div>

      {isNexusView && nexus && !nexus.ok && (
        <div className="notice chart-notice" role="status">
          <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
          <div className="grow">
            <b>NEXUS todavía no puede analizar {ticker} en {tf}.</b> {nexus.message || 'No hay suficiente historial.'} En el panel lateral
            tienes la lectura rápida con los técnicos de TradingView.
          </div>
          <Button size="sm" icon={<Tv size={13} />} onClick={openTradingView}>
            Vista TradingView
          </Button>
        </div>
      )}

      <div className="chart-main">
        <div className="chart-area">
          <div className="chart-stage" style={{ display: isNexusView ? 'flex' : 'none' }}>
            <NexusChart
              ref={chartRef}
              viewKey={`${symbol}|${tf}`}
              candles={candles}
              nexus={nexus}
              indicators={indicatorItems}
              toggles={prefs.toggles}
              preview={preview}
              entryThreshold={entryThreshold}
              onEditIndicator={openIndicators}
              onRemoveIndicator={ind.remove}
              onRetryIndicator={onRetryIndicator}
            >
              {overlay}
            </NexusChart>
          </div>
          {!isNexusView && <TradingViewWidget symbol={symbol} tf={tf} />}
        </div>

        {prefs.sidebar && (
          <aside className="chart-sidebar" aria-label="Panel NEXUS">
            <SignalPanel
              nexus={nexus}
              loading={md.nexusLoading}
              error={md.nexusError}
              refreshing={md.nexusRefreshing}
              updatedAt={md.nexusUpdatedAt}
              tf={tf}
              onRetry={md.refreshNexus}
              onRefresh={md.refreshNexus}
              onOpenTradingView={openTradingView}
            />
            <TradeCheck symbol={symbol} tf={tf} current={current} onPreview={setPreview} />
            <Watchlist />
          </aside>
        )}
      </div>

      <IndicatorsDialog
        open={dialogOpen}
        onClose={closeIndicators}
        catalog={ind.catalog}
        catalogError={ind.catalogError}
        onRetryCatalog={reloadCatalog}
        items={indicatorItems}
        focusUid={focusUid}
        onAdd={ind.add}
        onUpdate={ind.update}
        onReset={ind.reset}
        onRemove={ind.remove}
        onClear={ind.clear}
      />
    </div>
  )
}
