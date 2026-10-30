// Windowed terminal output: fixed-height rows, only the visible slice (+ overscan) is rendered.
// Follow mode keeps the view pinned to the bottom; while paused, the first visible line is kept in place when
// new lines arrive or old ones are dropped from the ring buffer.
import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { useApp } from '../../store'
import { LEVEL_TAG, levelColor, splitSymbols, type TermEntry } from './termModel'

export const ROW_H = 18
const OVERSCAN = 12

function openSymbol(symbol: string): void {
  useApp.getState().openChart(symbol)
}

function Linkified({ text }: { text: string }) {
  const parts = splitSymbols(text)
  if (parts.length === 1 && typeof parts[0] === 'string') return <>{text}</>
  return (
    <>
      {parts.map((p, i) =>
        typeof p === 'string' ? (
          <Fragment key={i}>{p}</Fragment>
        ) : (
          <button
            key={i}
            type="button"
            className="term-sym"
            title={`Abrir ${p.symbol} en el gráfico`}
            onClick={(ev) => {
              ev.stopPropagation()
              openSymbol(p.symbol)
            }}
          >
            {p.symbol}
          </button>
        ),
      )}
    </>
  )
}

const TermRow = memo(function TermRow({ e, onTip }: { e: TermEntry; onTip: (cmd: string) => void }) {
  if (e.kind === 'cmd') {
    return (
      <div className="term-line term-cmd">
        <span className="term-time">{e.clock}</span>
        <span className="term-prompt">nexus&gt;</span>
        <span className="term-msg">{e.text}</span>
      </div>
    )
  }
  const color = levelColor(e.level)
  if (e.kind === 'event') {
    const tag = LEVEL_TAG[e.level] || e.level
    return (
      <div className="term-line" title={`${new Date(e.ts).toLocaleString('es-ES')} · ${tag} · ${e.source}\n${e.text}`}>
        <span className="term-time">{e.clock}</span>
        <span className={`term-tag lvl-${e.level}`}>{tag}</span>
        <span className="term-src">{e.source}</span>
        <span className="term-msg" style={{ color }}>
          <Linkified text={e.text} />
        </span>
      </div>
    )
  }
  return (
    <div className={`term-line ${e.kind === 'sys' ? 'term-sys' : ''}`}>
      <span className="term-time">{e.clock}</span>
      <span className="term-msg" style={{ color }}>
        {e.tips ? (
          <>
            {e.text}
            {e.tips.map((t, i) => (
              <Fragment key={t}>
                {i > 0 && <span className="term-dim"> · </span>}
                <button
                  type="button"
                  className="term-tip"
                  title="Escribir este comando"
                  onClick={(ev) => {
                    ev.stopPropagation()
                    onTip(t)
                  }}
                >
                  &apos;{t}&apos;
                </button>
              </Fragment>
            ))}
          </>
        ) : (
          <Linkified text={e.text} />
        )}
      </span>
    </div>
  )
})

export interface TermViewProps {
  lines: TermEntry[]
  follow: boolean
  /** Reported on every scroll: distance (px) between the viewport bottom and the end of the output. */
  onScrollState: (distanceFromBottom: number) => void
  /** The user explicitly scrolled up (wheel / keys / touch) while following. */
  onUserScrollUp: () => void
  onBackgroundClick: () => void
  onTip: (cmd: string) => void
}

export default function TermView({ lines, follow, onScrollState, onUserScrollUp, onBackgroundClick, onTip }: TermViewProps) {
  const elRef = useRef<HTMLDivElement | null>(null)
  const [vp, setVp] = useState({ top: 0, height: 240 })
  const linesRef = useRef(lines)
  linesRef.current = lines
  const prevLinesRef = useRef<TermEntry[] | null>(null)
  const followRef = useRef(follow)
  followRef.current = follow
  const anchorRef = useRef<{ entry: TermEntry; offset: number } | null>(null)
  const rafRef = useRef<number | null>(null)

  const measure = useCallback(() => {
    const el = elRef.current
    if (!el) return
    const top = el.scrollTop
    const height = el.clientHeight
    setVp((v) => (v.top === top && v.height === height ? v : { top, height }))
  }, [])

  const onScroll = useCallback(() => {
    const el = elRef.current
    if (!el) return
    const list = linesRef.current
    const idx = Math.min(list.length - 1, Math.floor(el.scrollTop / ROW_H))
    anchorRef.current = idx >= 0 ? { entry: list[idx], offset: el.scrollTop - idx * ROW_H } : null
    onScrollState(el.scrollHeight - el.scrollTop - el.clientHeight)
    if (rafRef.current === null) {
      rafRef.current = window.requestAnimationFrame(() => {
        rafRef.current = null
        measure()
      })
    }
  }, [measure, onScrollState])

  // After every change: pin the bottom (follow) or keep the anchored line in place (paused, content changed).
  useLayoutEffect(() => {
    const el = elRef.current
    if (!el) return
    const linesChanged = prevLinesRef.current !== lines
    prevLinesRef.current = lines
    if (follow) {
      el.scrollTop = el.scrollHeight
    } else if (linesChanged && anchorRef.current) {
      const idx = lines.indexOf(anchorRef.current.entry)
      if (idx >= 0) {
        const want = idx * ROW_H + anchorRef.current.offset
        if (Math.abs(el.scrollTop - want) > 1) el.scrollTop = want
      }
    }
    measure()
  }, [lines, follow, measure])

  useEffect(() => {
    const el = elRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      if (followRef.current) el.scrollTop = el.scrollHeight
      measure()
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      if (rafRef.current !== null) window.cancelAnimationFrame(rafRef.current)
      rafRef.current = null
    }
  }, [measure])

  const total = lines.length
  const visible = Math.ceil(vp.height / ROW_H) + 1
  let start: number
  let end: number
  if (follow) {
    end = total
    start = Math.max(0, end - visible - OVERSCAN)
  } else {
    start = Math.min(Math.max(0, Math.floor(vp.top / ROW_H) - OVERSCAN), Math.max(0, total - visible - OVERSCAN))
    end = Math.min(total, start + visible + OVERSCAN * 2)
  }
  const slice = lines.slice(start, end)

  const canScrollUp = () => (elRef.current?.scrollTop ?? 0) > 0

  const onMouseUp = (ev: ReactMouseEvent<HTMLDivElement>) => {
    if (ev.button !== 0) return
    const sel = window.getSelection()
    if (sel && !sel.isCollapsed && sel.toString().length > 0) return // selecting text to copy
    onBackgroundClick()
  }

  return (
    <div
      ref={elRef}
      className="term-out"
      onScroll={onScroll}
      onWheel={(ev) => {
        if (ev.deltaY < 0 && followRef.current && canScrollUp()) onUserScrollUp()
      }}
      onTouchMove={() => {
        if (followRef.current && canScrollUp()) onUserScrollUp()
      }}
      onMouseUp={onMouseUp}
      role="log"
      aria-live="off"
      aria-label="Salida de la terminal"
    >
      <div style={{ height: start * ROW_H }} />
      {slice.map((e) => (
        <TermRow key={e.id} e={e} onTip={onTip} />
      ))}
      <div style={{ height: (total - end) * ROW_H }} />
    </div>
  )
}
