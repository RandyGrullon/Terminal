// "¿Tomo esta operación?": check any idea (direction + optional entry / SL / TP) against the Trade Gate, see the
// verdict with its reasons, and open it in the demo account. Demo orders always fill at MARKET: an idea with its own
// entry price is re-checked at market with the same SL / TP before ordering (actual entry, R:R and verdict shown),
// so the order is never a different trade from the one verified. If the gate says NO TOMAR the server answers 409
// and the user must explicitly confirm "Abrir de todos modos".
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Eraser, Info, Scale, ShieldCheck, ShieldX, TrendingDown, TrendingUp } from 'lucide-react'
import { ApiError, api, errorMessage } from '../../api/client'
import type { Direction, GateResult, NexusCurrent, Timeframe } from '../../api/types'
import { fmtBarTime, fmtNum, fmtPrice, priceDecimals, splitSymbol } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Card, ErrorBox, Field, Loading, Modal, NumberInput, VerdictBadge } from '../ui'
import GateChecklist, { GateVerdictBadge, gateSignal, gateSummary, gateVerdictText, noSignalNote } from './GateChecklist'
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

/** The idea as typed (null = use NEXUS' level). */
export interface Idea {
  direction: Direction
  entry: number | null
  sl: number | null
  tp: number | null
}

/** Re-check of the idea at market (same SL / TP) shown before a demo order. */
interface MarketCheck {
  side: Direction
  loading: boolean
  gate: GateResult | null
  error: unknown
  /** The SL / TP are on the wrong side of the market price (checked before asking the server). */
  problem: string | null
}

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

const sameIdea = (a: Idea | null, b: Idea): boolean =>
  !!a && a.direction === b.direction && a.entry === b.entry && a.sl === b.sl && a.tp === b.tp

/** Demo orders always fill at market, so an order in the idea's direction with the user's own entry price must first
 *  be re-checked at market (same SL / TP): otherwise the order would be a different trade from the one verified. */
export function needsMarketCheck(side: Direction, idea: Idea): boolean {
  return side === idea.direction && idea.entry !== null
}

function plain(v: number | null | undefined): string {
  return isNum(v) ? v.toFixed(priceDecimals(v)) : ''
}

/** Spanish problem with the levels of an idea entered at `entry`, or null when they are coherent. */
export function validate(dir: Direction, entry: number | null, sl: number | null, tp: number | null): string | null {
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
  // The idea `result` belongs to: editing the inputs afterwards makes the verdict stale.
  const [checked, setChecked] = useState<Idea | null>(null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [ordering, setOrdering] = useState<Direction | null>(null)
  const [conflict, setConflict] = useState<Conflict | null>(null)
  const [market, setMarket] = useState<MarketCheck | null>(null)
  const reqRef = useRef(0)
  const marketReq = useRef(0)
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
    setChecked(null)
    setError(null)
    setChecking(false)
    onPreview(null)
  }, [onPreview])

  const closeMarket = useCallback(() => {
    marketReq.current++
    setMarket(null)
  }, [])

  // A new symbol / timeframe invalidates the idea.
  useEffect(() => {
    setEntry(null)
    setSl(null)
    setTp(null)
    setConflict(null)
    closeMarket()
    clearResult()
  }, [symbol, tf, clearResult, closeMarket])

  // Remove the idea's lines from the chart when this card goes away.
  useEffect(() => () => onPreview(null), [onPreview])

  const gate = current?.gate?.[direction] ?? null
  const refEntry = entry ?? (isNum(gate?.entry) ? gate.entry : isNum(current?.price) ? current.price : null)
  const problem = useMemo(() => validate(direction, refEntry, sl, tp), [direction, refEntry, sl, tp])
  const idea = useMemo<Idea>(() => ({ direction, entry, sl, tp }), [direction, entry, sl, tp])
  const resultStale = !!result && !sameIdea(checked, idea)
  // NEXUS' action decides whether a gate TOMAR is an opportunity (null = no NEXUS reading: no signal).
  const action = current?.action ?? null
  const actionLabel = current ? current.action_label || current.action : null

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
    const asked = idea
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
      setChecked(asked)
      onPreview(res)
    } catch (err) {
      if (!aliveRef.current || id !== reqRef.current) return
      setError(err)
      setResult(null)
      setChecked(null)
      onPreview(null)
    } finally {
      if (aliveRef.current && id === reqRef.current) setChecking(false)
    }
  }

  /** The idea's SL / TP re-checked at the current price (no entry = market), exactly what the order would send. */
  const checkAtMarket = async (side: Direction) => {
    const id = ++marketReq.current
    // E.g. a breakout idea (entry above the market) whose stop is above the current price: no valid market order.
    const pre = validate(side, isNum(current?.price) ? current.price : null, sl, tp)
    if (pre) {
      setMarket({ side, loading: false, gate: null, error: null, problem: pre })
      return
    }
    setMarket({ side, loading: true, gate: null, error: null, problem: null })
    try {
      const res = await api.gateCheck({ symbol, tf, direction: side, sl: sl ?? undefined, tp: tp ?? undefined })
      if (!aliveRef.current || id !== marketReq.current) return
      if (!res || typeof res !== 'object' || !res.verdict) throw new Error('Respuesta del filtro no válida')
      setMarket({ side, loading: false, gate: res, error: null, problem: null })
    } catch (err) {
      if (!aliveRef.current || id !== marketReq.current) return
      setMarket({ side, loading: false, gate: null, error: err, problem: null })
    }
  }

  const order = async (side: Direction, force = false) => {
    setOrdering(side)
    // The user's SL / TP travel only with an order in the idea's direction (that button is disabled while they are
    // inconsistent); the other direction uses NEXUS' levels. The order itself is always at market.
    const custom = side === direction
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
      closeMarket()
      const p = res?.position
      const g = res?.gate
      notify(
        `Orden demo abierta a mercado: ${side === 'long' ? 'COMPRA' : 'VENTA'} ${splitSymbol(symbol).ticker} @ ${fmtPrice(p?.entry_price)} · SL ${fmtPrice(
          p?.sl,
        )}${g ? ` · filtro ${g.verdict_label} ${Math.round(g.score)}/100` : ''}${force ? ' (contra el filtro)' : ''}`,
        'trade',
      )
    } catch (err) {
      if (!aliveRef.current) return
      if (err instanceof ApiError && err.status === 409 && !force) {
        const d = conflictDetail(err.data)
        closeMarket()
        setConflict({ side, message: d.message ?? err.message, gate: d.gate })
      } else {
        notify(`No se pudo abrir la orden demo: ${errorMessage(err)}`, 'error')
      }
    } finally {
      if (aliveRef.current) setOrdering(null)
    }
  }

  const requestOrder = (side: Direction) => {
    if (needsMarketCheck(side, idea)) void checkAtMarket(side)
    else void order(side)
  }

  // Market re-check shown in the confirmation modal.
  const marketGate = market?.gate ?? null
  const marketEntry = marketGate && isNum(marketGate.entry) ? marketGate.entry : null
  const marketProblem = market?.problem ?? (market && marketGate ? validate(market.side, marketEntry, sl, tp) : null)
  const marketRef = marketEntry ?? (isNum(current?.price) ? current.price : null)
  // The verified verdict of this very idea (same levels and direction), to compare with the market one.
  const ideaGate = market && result && !resultStale && result.direction === market.side ? result : null
  const resultFavorable = !!result && gateSignal(result, action) === 'favorable'

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
        <div className="tiny muted">
          Deja un campo vacío para usar el nivel que propone NEXUS (gris). La entrada sirve para verificar la idea: las órdenes demo se
          abren a mercado.
        </div>
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
          <div
            className={`chart-tc-result ${resultFavorable ? 'info' : result.verdict === 'TAKE' ? 'pos' : result.verdict === 'AVOID' ? 'neg' : 'warn'} ${
              resultStale ? 'stale' : ''
            }`}
          >
            {resultStale && <div className="tiny warn">Cambiaste la idea después de verificarla: pulsa «Verificar» de nuevo.</div>}
            <div className="row between wrap gap-2">
              <GateVerdictBadge gate={result} action={action} large />
              <span className="tiny muted">{result.direction === 'long' ? 'Largo' : 'Corto'} · {fmtBarTime(result.time, tf)}</span>
            </div>
            {result.summary && <p className="chart-advice">{gateSummary(result, action)}</p>}
            {resultFavorable && <div className="chart-gate-nosignal">{noSignalNote(result.direction, actionLabel)}</div>}
            <GateChecklist gate={result} />
            {checked && checked.entry !== null && (
              <div className="tiny muted">
                Verificada con tu entrada de {fmtPrice(checked.entry)}. La orden demo es a mercado: antes de abrirla se revisa la idea al
                precio actual.
              </div>
            )}
            <div className="tiny muted">Las líneas punteadas «IDEA» del gráfico muestran estos niveles.</div>
          </div>
        )}

        <div className="divider" />
        <div className="grid-2" style={{ gap: 8 }}>
          <Button
            variant="success"
            icon={<TrendingUp size={14} />}
            onClick={() => requestOrder('long')}
            loading={ordering === 'long'}
            disabled={ordering !== null || (direction === 'long' && !!problem)}
            title={direction === 'long' && problem ? `Corrige la idea: ${problem}` : 'Abre un largo a precio de mercado en la cuenta demo (simulado)'}
          >
            Comprar (demo, a mercado)
          </Button>
          <Button
            variant="danger"
            icon={<TrendingDown size={14} />}
            onClick={() => requestOrder('short')}
            loading={ordering === 'short'}
            disabled={ordering !== null || (direction === 'short' && !!problem)}
            title={direction === 'short' && problem ? `Corrige la idea: ${problem}` : 'Abre un corto a precio de mercado en la cuenta demo (simulado)'}
          >
            Vender (demo, a mercado)
          </Button>
        </div>
        <div className="tiny muted">
          Órdenes simuladas y siempre a mercado: tu entrada no fija el precio. En la dirección de tu idea se usan tu SL y objetivo; en la
          otra, los niveles de NEXUS. Si el filtro dice NO TOMAR, te avisaremos antes de abrir.
        </div>
      </div>

      <Modal
        open={!!market}
        onClose={() => (ordering !== null ? undefined : closeMarket())}
        width={600}
        title={
          <span className="row gap-2">
            <Scale size={16} /> Orden demo a mercado
          </span>
        }
        footer={
          market && (
            <>
              <Button onClick={closeMarket} disabled={ordering !== null}>
                Cancelar
              </Button>
              {marketGate?.verdict === 'AVOID' ? (
                <Button variant="danger" loading={ordering !== null} disabled={!!marketProblem} onClick={() => void order(market.side, true)}>
                  Abrir de todos modos
                </Button>
              ) : (
                <Button
                  variant="primary"
                  loading={ordering !== null}
                  disabled={!marketGate || !!marketProblem}
                  onClick={() => void order(market.side)}
                >
                  Abrir a mercado
                </Button>
              )}
            </>
          )
        }
      >
        {market && (
          <div className="col gap-3">
            <div className="notice">
              <Info size={15} style={{ flex: 'none', marginTop: 1 }} />
              <div>
                Las órdenes demo se abren a mercado, no a tu entrada de <b className="mono">{fmtPrice(entry)}</b>. El filtro revisa de nuevo la
                idea al precio actual con {sl !== null || tp !== null ? 'tu SL y objetivo' : 'los niveles de NEXUS'}: esto es lo que se
                abriría.
              </div>
            </div>
            {market.loading ? (
              <Loading label="Revisando la idea al precio actual…" />
            ) : market.error ? (
              <ErrorBox error={market.error} onRetry={() => void checkAtMarket(market.side)} />
            ) : (
              <>
                {marketProblem && (
                  <div className="small neg">
                    A precio de mercado{marketRef !== null ? ` (≈ ${fmtPrice(marketRef)})` : ''} esta orden no es válida: {marketProblem} Espera a
                    que el precio llegue a tu entrada o ajusta el SL y el objetivo.
                  </div>
                )}
                {marketGate && (
                  <>
                    <div className="row between wrap gap-2">
                      <GateVerdictBadge gate={marketGate} action={action} large />
                      <span className="tiny muted">{market.side === 'long' ? 'Largo' : 'Corto'} · a mercado</span>
                    </div>
                    <div className="chart-tc-compare">
                      <span className="head" />
                      <span className="head">Tu idea</span>
                      <span className="head">A mercado</span>
                      <span>Entrada</span>
                      <span className="mono">{fmtPrice(entry)}</span>
                      <span className="mono">≈ {fmtPrice(marketEntry)}</span>
                      <span>SL · hasta aquí</span>
                      <span className="mono">{fmtPrice(ideaGate ? ideaGate.sl : sl)}</span>
                      <span className="mono">{fmtPrice(marketGate.sl)}</span>
                      <span>Objetivo</span>
                      <span className="mono">{fmtPrice(ideaGate ? ideaGate.tp2 : tp)}</span>
                      <span className="mono">{fmtPrice(marketGate.tp2)}</span>
                      <span>R:R</span>
                      <span className="mono">{ideaGate ? fmtNum(ideaGate.rr, 2) : '—'}</span>
                      <span className="mono">{fmtNum(marketGate.rr, 2)}</span>
                      <span>Filtro</span>
                      <span>{ideaGate ? gateVerdictText(ideaGate, action) : 'sin verificar'}</span>
                      <span>{gateVerdictText(marketGate, action)}</span>
                    </div>
                    {marketGate.summary && <p className="chart-advice">{gateSummary(marketGate, action)}</p>}
                    {gateSignal(marketGate, action) === 'favorable' && (
                      <div className="chart-gate-nosignal">{noSignalNote(marketGate.direction, actionLabel)}</div>
                    )}
                    <details className="chart-sec">
                      <summary>Ver todas las comprobaciones</summary>
                      <div className="chart-sec-body">
                        <GateChecklist gate={marketGate} showLevels={false} />
                      </div>
                    </details>
                  </>
                )}
              </>
            )}
          </div>
        )}
      </Modal>

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
              Abrir una operación que el filtro rechaza es decisión tuya. En «Resultados de las señales» puedes comparar cómo habrían terminado
              las señales rechazadas con las tomadas en este activo. Es una operación simulada a mercado (cuenta demo).
            </p>
          </div>
        )}
      </Modal>
    </Card>
  )
}

export default memo(TradeCheck)
