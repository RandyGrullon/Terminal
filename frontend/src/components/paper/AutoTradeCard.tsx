// Auto-trading for the demo account: on/off, symbols (chips) and timeframe. The engine only opens trades when NEXUS
// signals AND the Trade Gate says TOMAR. Every change is saved immediately (optimistic, reverted on error).
import { memo, useEffect, useRef, useState } from 'react'
import { Bot, X } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { PaperAccount, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { splitSymbol } from '../../lib/format'
import { Card, Field, Select, Spinner, Toggle } from '../ui'
import SymbolSearch from '../SymbolSearch'
import { asTimeframe } from '../markets/opsUtils'

export interface AutoState {
  enabled: boolean
  symbols: string[]
  tf: Timeframe
}

const MAX_SYMBOLS = 20
const TF_OPTIONS = TIMEFRAMES.map((t) => ({ value: t, label: t }))

function fromAccount(a: PaperAccount): AutoState {
  return {
    enabled: !!a.auto_trade,
    symbols: Array.isArray(a.auto_symbols) ? a.auto_symbols.filter((s): s is string => typeof s === 'string' && s !== '') : [],
    tf: asTimeframe(a.auto_tf) ?? '1h',
  }
}

function normalizeAuto(raw: unknown, fallback: AutoState): AutoState {
  if (!raw || typeof raw !== 'object') return fallback
  const o = raw as { enabled?: unknown; symbols?: unknown; tf?: unknown }
  return {
    enabled: typeof o.enabled === 'boolean' ? o.enabled : fallback.enabled,
    symbols: Array.isArray(o.symbols) ? o.symbols.filter((s): s is string => typeof s === 'string' && s !== '') : fallback.symbols,
    tf: asTimeframe(typeof o.tf === 'string' ? o.tf : null) ?? fallback.tf,
  }
}

function AutoTradeCard({ account, onChanged }: { account: PaperAccount; onChanged: (auto: AutoState) => void }) {
  const [draft, setDraft] = useState<AutoState>(() => fromAccount(account))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const alive = useRef(true)
  const seq = useRef(0)
  const pending = useRef(0)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  // Follow server-side changes (other tabs, terminal command) while nothing is being saved here.
  const accKey = `${account.auto_trade}|${(account.auto_symbols ?? []).join(',')}|${account.auto_tf}`
  useEffect(() => {
    if (pending.current === 0) setDraft(fromAccount(account))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accKey])

  const save = async (next: AutoState) => {
    const prev = draft
    const id = ++seq.current
    pending.current++
    setDraft(next)
    setSaving(true)
    setError(null)
    try {
      const res = await api.paper.auto({ enabled: next.enabled, symbols: next.symbols, tf: next.tf })
      if (!alive.current || id !== seq.current) return
      const saved = normalizeAuto(res?.auto, next)
      setDraft(saved)
      onChanged(saved)
    } catch (err) {
      if (!alive.current || id !== seq.current) return
      setDraft(prev)
      setError(errorMessage(err))
    } finally {
      pending.current--
      if (alive.current && id === seq.current) setSaving(false)
    }
  }

  const addSymbol = (s: string) => {
    const sym = s.trim().toUpperCase()
    if (!sym || draft.symbols.includes(sym)) return
    if (draft.symbols.length >= MAX_SYMBOLS) {
      setError(`Máximo ${MAX_SYMBOLS} símbolos en auto-trading.`)
      return
    }
    void save({ ...draft, symbols: [...draft.symbols, sym] })
  }

  return (
    <Card
      title={
        <>
          <Bot size={14} /> Auto-trading
        </>
      }
      sub="Solo abre operaciones con señal NEXUS y el filtro en TOMAR"
      actions={saving ? <Spinner /> : undefined}
    >
      <div className="col gap-3">
        <Toggle
          checked={draft.enabled}
          onChange={(enabled) => void save({ ...draft, enabled })}
          label={<span className={draft.enabled ? 'pos bold' : 'text-2'}>{draft.enabled ? 'Activado' : 'Desactivado'}</span>}
        />

        <Field label={`Símbolos (${draft.symbols.length})`}>
          {draft.symbols.length > 0 ? (
            <div className="pp-chips">
              {draft.symbols.map((s) => (
                <span key={s} className="pp-chip" title={s}>
                  {splitSymbol(s).ticker}
                  <button
                    type="button"
                    aria-label={`Quitar ${s}`}
                    title={`Quitar ${s}`}
                    onClick={() => void save({ ...draft, symbols: draft.symbols.filter((x) => x !== s) })}
                  >
                    <X size={11} />
                  </button>
                </span>
              ))}
            </div>
          ) : (
            <span className="tiny muted">Ningún símbolo todavía.</span>
          )}
          <SymbolSearch onSelect={addSymbol} placeholder="Añadir símbolo…" compact />
        </Field>

        <Field label="Temporalidad">
          <Select value={draft.tf} onChange={(tf) => void save({ ...draft, tf })} options={TF_OPTIONS} />
        </Field>

        {draft.enabled && draft.symbols.length === 0 && <div className="tiny warn">Activado pero sin símbolos: añade al menos uno para que opere.</div>}
        {error && <div className="tiny neg">{error}</div>}
        <p className="tiny muted" style={{ margin: 0 }}>
          En cada vela cerrada NEXUS evalúa los símbolos; solo se abre la operación si NEXUS da señal de compra o venta y el filtro dice
          TOMAR en esa dirección. El motor gestiona el SL «hasta aquí», las salidas parciales en TP1/TP2, el stop dinámico y la salida
          anticipada. Riesgo por operación según Ajustes.
        </p>
      </div>
    </Card>
  )
}

export default memo(AutoTradeCard)
