// "¿Tomo esta operación?": check any idea (direction + optional entry / SL / TP) against the Trade Gate, see the
// verdict with its reasons, and open it in the demo account. If the gate says NO TOMAR the server answers 409
// and the user must explicitly confirm "Abrir de todos modos".
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Eraser, Scale, ShieldCheck, ShieldX, TrendingDown, TrendingUp } from 'lucide-react'
import { ApiError, api, errorMessage } from '../../api/client'
import type { Direction, GateResult, NexusCurrent, Timeframe } from '../../api/types'
import { fmtBarTime, fmtPrice, priceDecimals, splitSymbol } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Card, ErrorBox, Field, Modal, NumberInput, VerdictBadge } from '../ui'
import GateChecklist from './GateChecklist'
import './nexus.css'

interface Props {
  symbol: string
  tf: Timeframe
  current: NexusCurrent | null
  /** Draw (or clear) the checked idea's levels on the chart. */
  onPreview: (gate: GateResult | null) => void
}

interface Conflict {
  side: Direction
  message: string
  gate: GateResult | null
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

function plain(v: number | null | undefined): string {
  return isNum(v) ? v.toFixed(priceDecimals(v)) : ''
}

function validate(dir: Direction, entry: number | null, sl: number | null, tp: number | null): string | null {
  if (entry !== null && entry <= 0) return 'La entrada debe ser mayor que 0.'
  if (sl !== null && sl <= 0) return 'El stop loss debe ser mayor que 0.'
  if (tp !== null && tp <= 0) return 'El objetivo debe ser mayor que 0.'
  if (entry !== null && sl !== null) {
    if (dir === 'long' && sl >= entry) return 'En un largo el stop loss («hasta aquí») debe quedar por debajo de la entrada.'
    if (dir === 'short' && sl <= entry) return 'En un corto el stop loss («hasta aquí») debe quedar por encima de la entrada.'
  }
  if (entry !== null && tp !== null) {
    if (dir === 'long' && tp <= entry) return 'En un largo el objetivo debe quedar por encima de la entrada.'
    if (dir === 'short' && tp >= entry) return 'En un corto el objetivo debe quedar por debajo de la entrada.'
  }
  return null
}

function conflictDetail(data: unknown): { message: string | null; gate: GateResult | null } {
  const detail = data && typeof data === 'object' ? (data as { detail?: unknown }).detail : null
  if (typeof detail === 'string') return { message: detail, gate: null }
  if (!detail || typeof detail !== 'object') return { message: null, gate: null }
  const o = detail as { message?: unknown; gate?: unknown }
  const gate = o.gate && typeof o.gate === 'object' && 'verdict' in o.gate ? (o.gate as GateResult) : null
  return { message: typeof o.message === 'string' ? o.message : null, gate }
}

function TradeCheck({ symbol, tf, current, onPreview }: Props) {
  const notify = useApp((s) => s.notify)
  const [direction, setDirection] = useState<Direction>('long')
  const [entry, setEntry] = useState<number | null>(null)
  const [sl, setSl] = useState<number | null>(null)
  const [tp, setTp] = useState<number | null>(null)
  const [result, setResult] = useState<GateResult | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [ordering, setOrdering] = useState<Direction | null>(null)
  const [conflict, setConflict] = useState<Conflict | null>(null)
  const reqRef = useRef(0)
  const aliveRef = useRef(true)

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const clearResult = useCallback(() => {
    reqRef.current++
    setResult(null)
    setError(null)
    setChecking(false)
    onPreview(null)
  }, [onPreview])

  // A new symbol / timeframe invalidates the idea.
  useEffect(() => {
    setEntry(null)
    setSl(null)
    setTp(null)
    setConflict(null)
    clearResult()
  }, [symbol, tf, clearResult])

  // Remove the idea's lines from the chart when this card goes away.
  useEffect(() => () => onPreview(null), [onPreview])

  const gate = current?.gate?.[direction] ?? null
  const refEntry = entry ?? (isNum(gate?.entry) ? gate.entry : isNum(current?.price) ? current.price : null)
  const problem = useMemo(() => validate(direction, refEntry, sl, tp), [direction, refEntry, sl, tp])

  const changeDirection = (d: Direction) => {
    if (d === direction) return
    setDirection(d)
    setSl(null)
    setTp(null)
    clearResult()
  }

  const verify = async () => {
    if (problem) return
    const id = ++reqRef.current
    setChecking(true)
    setError(null)
    try {
      const res = await api.gateCheck({
        symbol,
        tf,
        direction,
        entry: entry ?? undefined,
        sl: sl ?? undefined,
        tp: tp ?? undefined,
      })
      if (!aliveRef.current || id !== reqRef.current) return
      if (!res || typeof res !== 'object' || !res.verdict) throw new Error('Respuesta del filtro no válida')
      setResult(res)
      onPreview(res)
    } catch (err) {
      if (!aliveRef.current || id !== reqRef.current) return
      setError(err)
      setResult(null)
      onPreview(null)
    } finally {
      if (aliveRef.current && id === reqRef.current) setChecking(false)
    }
  }

  const order = async (side: Direction, force = false) => {
    setOrdering(side)
    const custom = side === direction && !problem
    try {
      const res = await api.paper.order({
        symbol,
        side,
        tf,
        sl: custom && sl !== null ? sl : undefined,
        tp: custom && tp !== null ? tp : undefined,
        ...(force ? { force: true } : {}),
      })
      if (!aliveRef.current) return
      setConflict(null)
      const p = res?.position
      const g = res?.gate
      notify(
        `Orden demo abierta: ${side === 'long' ? 'COMPRA' : 'VENTA'} ${splitSymbol(symbol).ticker} @ ${fmtPrice(p?.entry_price)} · SL ${fmtPrice(
          p?.sl,
        )}${g ? ` · filtro ${g.verdict_label} ${Math.round(g.score)}/100` : ''}${force ? ' (contra el filtro)' : ''}`,
        'trade',
      )
    } catch (err) {
      if (!aliveRef.current) return
      if (err instanceof ApiError && err.status === 409 && !force) {
        const d = conflictDetail(err.data)
        setConflict({ side, message: d.message ?? err.message, gate: d.gate })
      } else {
        notify(`No se pudo abrir la orden demo: ${errorMessage(err)}`, 'error')
      }
    } finally {
      if (aliveRef.current) setOrdering(null)
    }
  }

  return (
    <Card
      title={
        <>
          <Scale size={14} /> ¿Tomo esta operación?
        </>
      }
      sub="Pasa tu idea por el filtro antes de entrar"
    >
      <div className="col gap-2">
        <div className="btn-group chart-dir" role="group" aria-label="Dirección">
          <button
            type="button"
            className={`btn btn-sm ${direction === 'long' ? 'active chart-dir-long' : ''}`}
            onClick={() => changeDirection('long')}
          >
            <TrendingUp size={13} /> Largo
          </button>
          <button
            type="button"
            className={`btn btn-sm ${direction === 'short' ? 'active chart-dir-short' : ''}`}
            onClick={() => changeDirection('short')}
          >
            <TrendingDown size={13} /> Corto
          </button>
        </div>

        <div className="chart-tc-inputs">
          <Field label="Entrada">
            <NumberInput value={entry} onChange={setEntry} min={0} placeholder={plain(gate?.entry ?? current?.price)} />
          </Field>
          <Field label="SL · hasta aquí">
            <NumberInput value={sl} onChange={setSl} min={0} placeholder={plain(gate?.sl)} />
          </Field>
          <Field label="Objetivo">
            <NumberInput value={tp} onChange={setTp} min={0} placeholder={plain(gate?.tp2)} />
          </Field>
        </div>
        <div className="tiny muted">Deja un campo vacío para usar el nivel que propone NEXUS (gris).</div>
        {problem && <div className="tiny neg">{problem}</div>}

        <div className="row gap-2">
          <Button variant="primary" className="grow" icon={<ShieldCheck size={14} />} onClick={verify} loading={checking} disabled={!!problem}>
            Verificar
          </Button>
          {(result || entry !== null || sl !== null || tp !== null) && (
            <Button
              variant="ghost"
              icon={<Eraser size={14} />}
              title="Borrar la idea y sus líneas del gráfico"
              onClick={() => {
                setEntry(null)
                setSl(null)
                setTp(null)
                clearResult()
              }}
            />
          )}
        </div>

        {!!error && <ErrorBox error={error} onRetry={verify} />}

        {result && (
          <div className={`chart-tc-result ${result.verdict === 'TAKE' ? 'pos' : result.verdict === 'AVOID' ? 'neg' : 'warn'}`}>
            <div className="row between wrap gap-2">
              <VerdictBadge verdict={result.verdict} score={result.score} label={result.verdict_label || undefined} large />
              <span className="tiny muted">{result.direction === 'long' ? 'Largo' : 'Corto'} · {fmtBarTime(result.time, tf)}</span>
            </div>
            {result.summary && <p className="chart-advice">{result.summary}</p>}
            <GateChecklist gate={result} />
            <div className="tiny muted">Las líneas punteadas «IDEA» del gráfico muestran estos niveles.</div>
          </div>
        )}

        <div className="divider" />
        <div className="grid-2" style={{ gap: 8 }}>
          <Button
            variant="success"
            icon={<TrendingUp size={14} />}
            onClick={() => order('long')}
            loading={ordering === 'long'}
            disabled={ordering !== null}
            title="Abre un largo en la cuenta demo (simulado)"
          >
            Comprar (demo)
          </Button>
          <Button
            variant="danger"
            icon={<TrendingDown size={14} />}
            onClick={() => order('short')}
            loading={ordering === 'short'}
            disabled={ordering !== null}
            title="Abre un corto en la cuenta demo (simulado)"
          >
            Vender (demo)
          </Button>
        </div>
        <div className="tiny muted">Órdenes simuladas en la cuenta demo. Si el filtro dice NO TOMAR, te avisaremos antes de abrir.</div>
      </div>

      <Modal
        open={!!conflict}
        onClose={() => setConflict(null)}
        width={600}
        title={
          <span className="row gap-2">
            <ShieldX size={16} className="neg" /> El filtro dice: {conflict?.gate?.verdict_label || 'NO TOMAR'}
          </span>
        }
        footer={
          <>
            <Button onClick={() => setConflict(null)} disabled={ordering !== null}>
              Cancelar
            </Button>
            <Button variant="danger" loading={ordering !== null} onClick={() => conflict && order(conflict.side, true)}>
              Abrir de todos modos
            </Button>
          </>
        }
      >
        {conflict && (
          <div className="col gap-3">
            <div className="row gap-2 wrap">
              <VerdictBadge verdict={conflict.gate?.verdict ?? 'AVOID'} score={conflict.gate?.score} label={conflict.gate?.verdict_label || undefined} large />
              <span className="small text-2">{conflict.side === 'long' ? 'Compra (largo)' : 'Venta (corto)'} en {splitSymbol(symbol).ticker}</span>
            </div>
            <p className="chart-advice">{conflict.message}</p>
            {conflict.gate && <GateChecklist gate={conflict.gate} />}
            <p className="tiny muted">
              Abrir una operación que el filtro rechaza es decisión tuya. En «Resultados de las señales» puedes ver qué porcentaje de las señales
              rechazadas habría perdido en este activo. Es una operación simulada (cuenta demo).
            </p>
          </div>
        )}
      </Modal>
    </Card>
  )
}

export default memo(TradeCheck)
