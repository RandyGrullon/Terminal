// Header "semáforo de mercado": a compact pill that always says whether to trade right now (Operar / Precaución /
// No operar) with a countdown to the next high-impact event; a click opens the detail panel. It also hosts the
// WS alert toasts. It never blocks the header: without data it shows a muted "Macro: sin datos" pill.
import { useEffect, useRef, useState } from 'react'
import type { MacroLevel } from '../../api/types'
import { useMacroAlerts } from './alerts'
import { MacroBoundary } from './MacroBoundary'
import { pulseCountdown } from './macroFormat'
import { LEVEL_META } from './macroModel'
import MarketPulsePanel from './MarketPulsePanel'
import { useMacro, type MacroState } from './useMacro'
import './macro.css'

export default function MarketPulse() {
  return (
    <MacroBoundary fallback={<FallbackPill />}>
      <PulseWidget />
    </MacroBoundary>
  )
}

function FallbackPill() {
  return (
    <span className="mp-pill lv-none" title="El semáforo de mercado no está disponible ahora mismo.">
      <span className="mp-dot" aria-hidden="true" />
      <span className="mp-label">Macro: sin datos</span>
    </span>
  )
}

interface PillView {
  level: MacroLevel | 'none'
  text: string
  count: string | null
  title: string
  aria: string
}

function pillView(m: MacroState): PillView {
  const c = m.conditions
  if (!c) {
    const text = m.loading ? 'Macro…' : 'Macro: sin datos'
    const title = m.loading
      ? 'Cargando condiciones de mercado…'
      : 'Sin datos de condiciones de mercado (Fed, calendario, volatilidad). Opera con cautela.'
    return { level: 'none', text, count: null, title, aria: `Semáforo de mercado: ${title}` }
  }
  const meta = LEVEL_META[c.level]
  const count = pulseCountdown(c, m.now)
  const title = m.stale ? `${c.headline}\n(Datos desactualizados)` : c.headline
  const aria = `Semáforo de mercado: ${meta.short}${count ? `, ${count}` : ''}. ${c.headline}`
  return { level: c.level, text: meta.short, count, title, aria }
}

function PulseWidget() {
  const m = useMacro()
  useMacroAlerts()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const pillRef = useRef<HTMLButtonElement>(null)

  // Popover: close on outside click and on Esc (focus goes back to the pill).
  useEffect(() => {
    if (!open) return
    const onPointer = (e: PointerEvent) => {
      const wrap = wrapRef.current
      if (wrap && e.target instanceof Node && !wrap.contains(e.target)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      pillRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointer, true)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const close = () => {
    setOpen(false)
    pillRef.current?.focus()
  }

  const v = pillView(m)
  return (
    <div className="mp-wrap" ref={wrapRef}>
      <button
        ref={pillRef}
        type="button"
        className={`mp-pill lv-${v.level}${m.stale ? ' mp-stale' : ''}${open ? ' open' : ''}`}
        title={v.title}
        aria-label={v.aria}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="mp-dot" aria-hidden="true" />
        <span className="mp-label">{v.text}</span>
        {v.count && <span className="mp-count">{v.count}</span>}
      </button>
      {open && (
        <MacroBoundary
          fallback={
            <div className="mp-panel" role="dialog" aria-label="Semáforo de mercado">
              <span className="muted small">No se pudo mostrar el detalle del semáforo.</span>
            </div>
          }
        >
          <MarketPulsePanel state={m} onClose={close} />
        </MacroBoundary>
      )}
    </div>
  )
}
