// Symbol search over every TradingView asset: header search (opens the chart) and symbol picker (onSelect).
// Debounced server search with cancellation, keyboard navigation, "/" global shortcut and direct
// "EXCHANGE:TICKER" entry.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { CornerDownLeft, Search, X } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { SearchResult } from '../api/types'
import { fmtPct, fmtPrice, signTone, splitSymbol, toneColor } from '../lib/format'
import { useApp } from '../store'
import { Badge, Spinner } from './ui'
import { isNum, typeLabel } from './markets/opsUtils'
import './markets/symbolSearch.css'

export interface SymbolSearchProps {
  onSelect?: (symbol: string) => void
  placeholder?: string
  autoFocus?: boolean
  compact?: boolean
}

const DEBOUNCE_MS = 250
const RESULT_LIMIT = 20
const FULL_SYMBOL_RE = /^[A-Z0-9_]{1,20}:[A-Z0-9._!-]{1,40}$/i

type Item = { kind: 'direct'; symbol: string } | { kind: 'result'; r: SearchResult }

/** Global-search inputs (instances without onSelect) in mount order; "/" focuses the first visible one. */
const shortcutOwners: HTMLInputElement[] = []

function isTypingTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false
  if (t.isContentEditable) return true
  const tag = t.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  return !!t.closest('.cm-editor, [contenteditable="true"], [role="textbox"]')
}

export default function SymbolSearch({ onSelect, placeholder, autoFocus, compact }: SymbolSearchProps) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const [results, setResults] = useState<SearchResult[]>([])
  const [searchedFor, setSearchedFor] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [active, setActive] = useState(0)
  const [pos, setPos] = useState<CSSProperties | null>(null)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const listRef = useRef<HTMLUListElement | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const timerRef = useRef<number | null>(null)
  const enterPendingRef = useRef(false)
  const navigatedRef = useRef(false)
  const posKeyRef = useRef('')
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect
  const isGlobal = !onSelect

  const choose = useCallback((symbol: string) => {
    const sym = symbol.trim().toUpperCase()
    if (!sym) return
    abortRef.current?.abort()
    enterPendingRef.current = false
    navigatedRef.current = false
    setOpen(false)
    setQ('')
    setResults([])
    setSearchedFor('')
    setError(null)
    setLoading(false)
    inputRef.current?.blur()
    const cb = onSelectRef.current
    if (cb) cb(sym)
    else useApp.getState().openChart(sym)
  }, [])

  const runSearch = useCallback(
    async (text: string) => {
      abortRef.current?.abort()
      const ctrl = new AbortController()
      abortRef.current = ctrl
      setLoading(true)
      setError(null)
      try {
        const res = await api.search(text, undefined, RESULT_LIMIT, ctrl.signal)
        if (ctrl.signal.aborted) return
        const list = (Array.isArray(res?.results) ? res.results : []).filter(
          (r): r is SearchResult => !!r && typeof r.symbol === 'string' && r.symbol !== '',
        )
        setResults(list)
        setSearchedFor(text)
        navigatedRef.current = false
        setActive(0)
        if (enterPendingRef.current) {
          enterPendingRef.current = false
          if (list.length) choose(list[0].symbol)
        }
      } catch (err) {
        if (ctrl.signal.aborted || (err as Error)?.name === 'AbortError') return
        enterPendingRef.current = false
        setResults([])
        setSearchedFor(text)
        setError(errorMessage(err))
      } finally {
        if (abortRef.current === ctrl) {
          abortRef.current = null
          setLoading(false)
        }
      }
    },
    [choose],
  )

  // Debounced search on every keystroke.
  useEffect(() => {
    const text = q.trim()
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
    if (!text) {
      abortRef.current?.abort()
      abortRef.current = null
      setResults([])
      setSearchedFor('')
      setError(null)
      setLoading(false)
      return
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      void runSearch(text)
    }, DEBOUNCE_MS)
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [q, runSearch])

  useEffect(
    () => () => {
      abortRef.current?.abort()
    },
    [],
  )

  // Display items: the typed full symbol first ("open directly"), then the server results.
  const trimmed = q.trim()
  const typedSymbol = FULL_SYMBOL_RE.test(trimmed) ? trimmed.toUpperCase() : null
  const items = useMemo<Item[]>(() => {
    const list: Item[] = []
    let rest = results
    if (typedSymbol) {
      const exact = results.find((r) => r.symbol.toUpperCase() === typedSymbol)
      if (exact) {
        list.push({ kind: 'result', r: exact })
        rest = results.filter((r) => r !== exact)
      } else {
        list.push({ kind: 'direct', symbol: typedSymbol })
      }
    }
    for (const r of rest) list.push({ kind: 'result', r })
    return list
  }, [results, typedSymbol])

  useEffect(() => {
    setActive((a) => (items.length ? Math.min(a, items.length - 1) : 0))
  }, [items.length])

  // Keep the highlighted row visible.
  useEffect(() => {
    if (!open) return
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  }, [active, open])

  // Fixed-position dropdown (escapes overflow clipping of cards, tables and modals).
  const place = useCallback(() => {
    const el = wrapRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight
    const width = Math.min(Math.max(r.width, 420), vw - 16)
    let left = r.left
    if (left + width > vw - 8) left = Math.max(8, vw - 8 - width)
    const below = vh - r.bottom - 12
    const above = r.top - 12
    const next: CSSProperties =
      below < 240 && above > below
        ? { left, width, bottom: vh - r.top + 4, maxHeight: Math.min(460, above) }
        : { left, width, top: r.bottom + 4, maxHeight: Math.min(460, Math.max(160, below)) }
    const key = JSON.stringify(next)
    if (key !== posKeyRef.current) {
      posKeyRef.current = key
      setPos(next)
    }
  }, [])

  useLayoutEffect(() => {
    if (open) place()
  }, [open, place, items.length])

  useEffect(() => {
    if (!open) return
    const onWin = () => place()
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current?.contains(e.target as Node)) return
      setOpen(false)
    }
    window.addEventListener('resize', onWin)
    window.addEventListener('scroll', onWin, true)
    document.addEventListener('mousedown', onDown, true)
    return () => {
      window.removeEventListener('resize', onWin)
      window.removeEventListener('scroll', onWin, true)
      document.removeEventListener('mousedown', onDown, true)
    }
  }, [open, place])

  // "/" focuses the global search (ignored while typing in inputs, editors or with a modal open).
  useEffect(() => {
    if (!isGlobal) return
    const el = inputRef.current
    if (!el) return
    shortcutOwners.push(el)
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return
      if (isTypingTarget(e.target) || document.querySelector('.modal-backdrop')) return
      const owner = shortcutOwners.find((o) => o.isConnected && o.offsetParent !== null)
      if (owner !== el) return
      e.preventDefault()
      el.focus()
      el.select()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      const i = shortcutOwners.indexOf(el)
      if (i >= 0) shortcutOwners.splice(i, 1)
    }
  }, [isGlobal])

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (!open) setOpen(true)
      if (items.length) {
        navigatedRef.current = true
        setActive((a) => (a + 1) % items.length)
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (items.length) {
        navigatedRef.current = true
        setActive((a) => (a - 1 + items.length) % items.length)
      }
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (!trimmed) return
      const upToDate = searchedFor === trimmed
      if (navigatedRef.current && items[active]) {
        const it = items[active]
        choose(it.kind === 'direct' ? it.symbol : it.r.symbol)
      } else if (typedSymbol) {
        choose(typedSymbol)
      } else if (upToDate && items.length) {
        const it = items[0]
        choose(it.kind === 'direct' ? it.symbol : it.r.symbol)
      } else {
        // Results not ready yet: search now and open the best match when it arrives.
        enterPendingRef.current = true
        setOpen(true)
        if (timerRef.current !== null) window.clearTimeout(timerRef.current)
        timerRef.current = null
        void runSearch(trimmed)
      }
    } else if (e.key === 'Escape') {
      e.preventDefault()
      if (open && trimmed) setOpen(false)
      else {
        setQ('')
        setOpen(false)
        inputRef.current?.blur()
      }
    }
  }

  const showDrop = open && trimmed !== '' && pos !== null
  const pending = loading || (trimmed !== '' && searchedFor !== trimmed && !error)

  return (
    <div ref={wrapRef} className={`symsearch ${compact ? 'compact' : ''}`}>
      <Search size={compact ? 13 : 14} className="symsearch-ico" />
      <input
        ref={inputRef}
        className="input symsearch-input"
        value={q}
        autoFocus={autoFocus}
        spellCheck={false}
        autoComplete="off"
        placeholder={placeholder ?? (isGlobal ? 'Buscar símbolo… (/)' : 'Buscar símbolo…')}
        aria-label={placeholder ?? 'Buscar símbolo'}
        aria-expanded={showDrop}
        aria-autocomplete="list"
        role="combobox"
        onChange={(e) => {
          setQ(e.target.value)
          setError(null)
          navigatedRef.current = false
          setOpen(e.target.value.trim() !== '')
        }}
        onFocus={() => {
          if (trimmed) setOpen(true)
        }}
        onKeyDown={onKeyDown}
      />
      {pending && trimmed !== '' ? (
        <span className="symsearch-right">
          <Spinner />
        </span>
      ) : q ? (
        <button
          type="button"
          className="symsearch-right symsearch-clear"
          title="Borrar"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => {
            setQ('')
            setOpen(false)
            inputRef.current?.focus()
          }}
        >
          <X size={13} />
        </button>
      ) : isGlobal && !compact ? (
        <kbd className="symsearch-right symsearch-kbd" title="Pulsa / para buscar">
          /
        </kbd>
      ) : null}

      {showDrop && (
        <div className="symsearch-drop" style={pos ?? undefined} role="listbox">
          {error && (
            <div className="symsearch-msg neg">
              No se pudo buscar: {error}
              {typedSymbol && <div className="muted tiny">Pulsa Enter para abrir {typedSymbol} igualmente.</div>}
            </div>
          )}
          {!error && items.length === 0 && (
            <div className="symsearch-msg">
              {pending ? (
                <span className="row gap-2">
                  <Spinner /> Buscando «{trimmed}»…
                </span>
              ) : (
                <>
                  Sin resultados para «{trimmed}».
                  <div className="muted tiny">Escribe el símbolo completo (por ejemplo NASDAQ:AAPL) y pulsa Enter para abrirlo.</div>
                </>
              )}
            </div>
          )}
          {items.length > 0 && (
            <ul ref={listRef} className="symsearch-list">
              {items.map((it, i) =>
                it.kind === 'direct' ? (
                  <li
                    key={`direct:${it.symbol}`}
                    data-idx={i}
                    className={`symsearch-item direct ${i === active ? 'active' : ''}`}
                    role="option"
                    aria-selected={i === active}
                    onMouseEnter={() => setActive(i)}
                    onMouseDown={(e) => {
                      e.preventDefault()
                      choose(it.symbol)
                    }}
                  >
                    <div className="symsearch-line">
                      <CornerDownLeft size={13} className="muted" />
                      <span>
                        Abrir <strong className="mono">{it.symbol}</strong> directamente
                      </span>
                    </div>
                  </li>
                ) : (
                  <ResultRow key={it.r.symbol} r={it.r} idx={i} active={i === active} onHover={setActive} onChoose={choose} />
                ),
              )}
            </ul>
          )}
          <div className="symsearch-foot">
            <span>↑↓ moverse · Enter {onSelect ? 'elegir' : 'abrir gráfico'} · Esc cerrar</span>
            {pending && items.length > 0 && <Spinner />}
          </div>
        </div>
      )}
    </div>
  )
}

function ResultRow(props: {
  r: SearchResult
  idx: number
  active: boolean
  onHover: (i: number) => void
  onChoose: (symbol: string) => void
}) {
  const { r, idx, active } = props
  const { exchange, ticker } = splitSymbol(r.symbol)
  const hasPrice = isNum(r.price)
  const hasChange = isNum(r.change_pct)
  return (
    <li
      data-idx={idx}
      className={`symsearch-item ${active ? 'active' : ''}`}
      role="option"
      aria-selected={active}
      title={r.symbol}
      onMouseEnter={() => props.onHover(idx)}
      onMouseDown={(e) => {
        e.preventDefault()
        props.onChoose(r.symbol)
      }}
    >
      <div className="symsearch-line">
        <span className="symsearch-ticker">{r.ticker || ticker}</span>
        <Badge tone="muted">{r.exchange || exchange || '—'}</Badge>
        {r.type && (
          <Badge tone="info" outline>
            {typeLabel(r.type)}
          </Badge>
        )}
        {r.history === 'recorder' && (
          <Badge
            tone="warn"
            title="Sin proveedor con historial completo: NEXUS construye el historial localmente a partir de las cotizaciones en vivo"
          >
            historial local
          </Badge>
        )}
        <span className="grow" />
        {hasPrice && <span className="mono symsearch-price">{fmtPrice(r.price)}</span>}
        {hasChange && (
          <span className="mono symsearch-chg" style={{ color: toneColor(signTone(r.change_pct)) }}>
            {fmtPct(r.change_pct)}
          </span>
        )}
      </div>
      {r.description && <div className="symsearch-desc">{r.description}</div>}
    </li>
  )
}
