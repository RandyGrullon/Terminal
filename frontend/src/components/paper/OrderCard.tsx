// New demo order ticket: symbol, side, order type (at market, limit or stop), timeframe, risk % and optional SL / TP.
// The Trade Gate verdict is shown BEFORE submitting (for a limit / stop, at the order's own price; a TOMAR without
// NEXUS' own entry signal is only "Filtro favorable · sin señal NEXUS"); if the server answers 409 because the filter
// says NO TOMAR the user must explicitly confirm "Abrir / Dejar de todos modos". A pending order is asked again at the
// fill, where a NO TOMAR cancels it. A 409 from a portfolio-risk limit cannot be forced (RiskBlockModal).
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChartCandlestick, RefreshCw, ShieldCheck, ShieldX, TrendingDown, TrendingUp } from 'lucide-react'
import { ApiError, api, errorMessage } from '../../api/client'
import type { Direction, GateResult, NexusAction, PaperOrderKind, PaperRiskBlock, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { fmtPrice, priceDecimals, splitSymbol } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Card, Field, IconButton, Modal, NumberInput, Select, VerdictBadge } from '../ui'
import SymbolSearch from '../SymbolSearch'
import GateChecklist, { gateSignal } from '../nexus/GateChecklist'
import { isNum, useInterval } from '../markets/opsUtils'
import GatePreview from './GatePreview'
import ExpiryPicker from './ExpiryPicker'
import RiskBlockModal from './RiskBlockModal'
import { paperOrdersApi } from './ordersApi'
import { DEFAULT_EXPIRY, KIND_HELP, expiryRequest, kindLabel, orderKindFor, riskBlockDetail, validatePendingOrder, type ExpiryDraft } from './ordersUtils'
import { conflictDetail, isGateResult, validateLevels } from './paperUtils'

type OrderType = 'market' | PaperOrderKind

const TYPE_OPTIONS: { value: OrderType; label: string }[] = [
  { value: 'market', label: 'A mercado' },
  { value: 'limit', label: 'Límite' },
  { value: 'stop', label: 'Stop' },
]
const QUOTE_REFRESH_MS = 15_000

interface Preview {
  key: string // symbol|side|tf the verdict belongs to
  gate: GateResult
}

/** NEXUS' current action for symbol|tf (null = no NEXUS reading), read when the preview says TOMAR. */
interface SignalRead {
  key: string
  at: number
  action: NexusAction | null
  label: string | null
}

const SIGNAL_TTL_MS = 30_000

interface Conflict {
  message: string
  gate: GateResult | null
}

const TF_OPTIONS = TIMEFRAMES.map((t) => ({ value: t, label: t }))
const PREVIEW_DEBOUNCE_MS = 450
const MAX_RISK_PCT = 10

const plain = (v: number | null | undefined): string => (isNum(v) ? v.toFixed(priceDecimals(v)) : '')

function OrderCard({ onOpened }: { onOpened: () => void }) {
  const notify = useApp((s) => s.notify)
  const [symbol, setSymbol] = useState(() => useApp.getState().symbol)
  const [side, setSide] = useState<Direction>('long')
  const [tf, setTf] = useState<Timeframe>(() => useApp.getState().tf)
  const [riskPct, setRiskPct] = useState<number | null>(null)
  const [riskDefault, setRiskDefault] = useState<number | null>(null)
  const [sl, setSl] = useState<number | null>(null)
  const [tp, setTp] = useState<number | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [checking, setChecking] = useState(false)
  const [gateError, setGateError] = useState<string | null>(null)
  const [ordering, setOrdering] = useState(false)
  const [conflict, setConflict] = useState<Conflict | null>(null)
  const [signal, setSignal] = useState<SignalRead | null>(null)
  const [orderType, setOrderType] = useState<OrderType>('market')
  const [orderPrice, setOrderPrice] = useState<number | null>(null)
  const [expiry, setExpiry] = useState<ExpiryDraft>(DEFAULT_EXPIRY)
  // last live quote of the symbol (pending orders: which side of the market the level is on)
  const [market, setMarket] = useState<{ symbol: string; price: number | null; at: number } | null>(null)
  const [riskBlock, setRiskBlock] = useState<PaperRiskBlock | null>(null)
  const alive = useRef(true)
  const reqRef = useRef(0)
  const pending = orderType !== 'market'

  useEffect(() => {
    alive.current = true
    // Default risk per trade from the settings (placeholder only; empty = server default).
    api.settings
      .get()
      .then((res) => {
        const v = res?.settings?.['risk.per_trade_pct']
        if (alive.current && isNum(v)) setRiskDefault(v)
      })
      .catch(() => undefined)
    return () => {
      alive.current = false
    }
  }, [])

  // The verdict shown belongs to this idea: a pending order is checked at its own price, a market order at market.
  const key = `${symbol}|${side}|${tf}|${pending ? `@${orderPrice ?? ''}` : 'mkt'}`
  const gate = preview && preview.key === key ? preview.gate : null
  const marketPrice = market && market.symbol === symbol && isNum(market.price) ? market.price : null
  const refPrice = pending ? orderPrice : gate && isNum(gate.entry) ? gate.entry : null
  const riskProblem = riskPct !== null && !(riskPct > 0 && riskPct <= MAX_RISK_PCT) ? `El riesgo debe estar entre 0 y ${MAX_RISK_PCT} %.` : null
  const expiryCheck = useMemo(() => expiryRequest(expiry, Math.floor(Date.now() / 1000)), [expiry])
  const problem = useMemo(
    () =>
      pending
        ? validatePendingOrder(side, orderType as PaperOrderKind, orderPrice, marketPrice, sl, tp) ?? (expiryCheck.ok ? null : expiryCheck.problem)
        : validateLevels(side, refPrice, sl, tp),
    [pending, side, orderType, orderPrice, marketPrice, sl, tp, refPrice, expiryCheck],
  )
  const suggested = pending ? orderKindFor(side, orderPrice, marketPrice) : null

  // Live quote while a pending type is selected (refreshed every 15 s): a stale price is never used as the market.
  const loadQuote = useCallback(async () => {
    if (!symbol) return
    try {
      const res = await api.quotes([symbol])
      const q = res?.quotes?.[symbol]
      if (!alive.current) return
      setMarket({ symbol, price: q && !q.stale && isNum(q.price) ? q.price : null, at: Date.now() })
    } catch {
      if (alive.current) setMarket({ symbol, price: null, at: Date.now() })
    }
  }, [symbol])
  useEffect(() => {
    if (pending) void loadQuote()
  }, [pending, loadQuote])
  useInterval(() => void loadQuote(), pending ? QUOTE_REFRESH_MS : null)

  const check = useCallback(async () => {
    if (!symbol) return
    if (pending && !(isNum(orderPrice) && orderPrice > 0)) return
    const id = ++reqRef.current
    const k = `${symbol}|${side}|${tf}|${pending ? `@${orderPrice ?? ''}` : 'mkt'}`
    setChecking(true)
    setGateError(null)
    try {
      const res = await api.gateCheck({
        symbol,
        tf,
        direction: side,
        entry: pending && orderPrice !== null ? orderPrice : undefined,
        sl: sl ?? undefined,
        tp: tp ?? undefined,
      })
      if (!alive.current || id !== reqRef.current) return
      if (!isGateResult(res)) throw new Error('Respuesta del filtro no válida')
      setPreview({ key: k, gate: res })
    } catch (err) {
      if (!alive.current || id !== reqRef.current) return
      setGateError(errorMessage(err))
    } finally {
      if (alive.current && id === reqRef.current) setChecking(false)
    }
  }, [symbol, side, tf, sl, tp, pending, orderPrice])

  // Re-check the gate (debounced) whenever the idea changes and is valid.
  useEffect(() => {
    if (!symbol || problem) return
    const t = window.setTimeout(() => void check(), PREVIEW_DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [symbol, side, tf, sl, tp, pending, orderPrice, problem, check])

  // A gate TOMAR is an opportunity only with NEXUS' own entry signal (BUY / SELL): read the current action when the
  // preview says TOMAR (cached for 30 s per symbol and timeframe). A failed read leaves the TOMAR unconfirmed.
  const signalKey = `${symbol}|${tf}`
  const takeShown = preview?.key === key && preview.gate.verdict === 'TAKE'
  useEffect(() => {
    if (!takeShown || (signal && signal.key === signalKey && Date.now() - signal.at < SIGNAL_TTL_MS)) return
    let cancelled = false
    api
      .nexus(symbol, tf)
      .then((res) => {
        if (cancelled || !alive.current) return
        const cur = res && res.ok ? res.current : null
        setSignal({ key: signalKey, at: Date.now(), action: cur ? cur.action : null, label: cur ? cur.action_label || cur.action : null })
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [takeShown, preview, signal, signalKey, symbol, tf])

  const changeSymbol = (s: string) => {
    const next = s.trim().toUpperCase()
    if (!next || next === symbol) return
    setSymbol(next)
    setSl(null)
    setTp(null)
    setOrderPrice(null)
    setGateError(null)
  }

  const changeSide = (d: Direction) => {
    if (d === side) return
    setSide(d)
    setSl(null)
    setTp(null)
  }

  /** A 409 is either a portfolio-risk limit (cannot be forced) or the gate's NO TOMAR (can, explicitly). */
  const handleConflict = (err: unknown, force: boolean): boolean => {
    if (!(err instanceof ApiError) || err.status !== 409) return false
    const block = riskBlockDetail(err.data)
    if (block) {
      setConflict(null)
      setRiskBlock(block)
      return true
    }
    if (force) return false
    const d = conflictDetail(err.data)
    setConflict({ message: d.message ?? err.message, gate: d.gate ?? gate })
    return true
  }

  const placePending = async (force: boolean) => {
    if (!expiryCheck.ok || orderPrice === null) return
    const kind = orderType as PaperOrderKind
    const res = await paperOrdersApi.place({
      symbol,
      side,
      kind,
      price: orderPrice,
      tf,
      risk_pct: riskPct ?? undefined,
      sl: sl ?? undefined,
      tp: tp ?? undefined,
      ...expiryCheck.fields,
      ...(force ? { force: true } : {}),
    })
    if (!alive.current) return
    setConflict(null)
    const o = res?.order
    notify(
      `Orden demo pendiente: ${kindLabel(side, kind).toLowerCase()} ${splitSymbol(symbol).ticker} a ${fmtPrice(orderPrice)} · ${
        o?.expiry_text ?? 'válida hasta cancelarla'
      }${force ? ' (contra el filtro: se revisa otra vez al ejecutarse)' : ''}`,
      'trade',
    )
    setOrderPrice(null)
    setSl(null)
    setTp(null)
    onOpened()
  }

  const order = async (force = false) => {
    if (!symbol || problem || riskProblem) return
    setOrdering(true)
    if (pending) {
      try {
        await placePending(force)
      } catch (err) {
        if (!alive.current) return
        if (!handleConflict(err, force)) notify(`No se pudo dejar la orden demo: ${errorMessage(err)}`, 'error')
      } finally {
        if (alive.current) setOrdering(false)
      }
      return
    }
    try {
      const res = await api.paper.order({
        symbol,
        side,
        tf,
        risk_pct: riskPct ?? undefined,
        sl: sl ?? undefined,
        tp: tp ?? undefined,
        ...(force ? { force: true } : {}),
      })
      if (!alive.current) return
      setConflict(null)
      const p = res?.position
      const g = res?.gate
      notify(
        `Orden demo abierta: ${side === 'long' ? 'LARGO' : 'CORTO'} ${splitSymbol(symbol).ticker} @ ${fmtPrice(p?.entry_price)} · SL ${fmtPrice(p?.sl)}${
          g ? ` · filtro ${g.verdict_label} ${Math.round(g.score)}/100` : ''
        }${force ? ' (contra el filtro)' : ''}`,
        'trade',
      )
      setSl(null)
      setTp(null)
      onOpened()
    } catch (err) {
      if (!alive.current) return
      if (!handleConflict(err, force)) notify(`No se pudo abrir la orden demo: ${errorMessage(err)}`, 'error')
    } finally {
      if (alive.current) setOrdering(false)
    }
  }

  const { exchange, ticker } = splitSymbol(symbol)
  const verdict = gate?.verdict ?? null
  // undefined = NEXUS' action not read (yet): a TOMAR stays unconfirmed.
  const signalAction = signal && signal.key === signalKey ? signal.action : undefined
  const signalLabel = signal && signal.key === signalKey ? signal.label : null
  const sig = gateSignal(gate, signalAction)
  const openVariant = sig === 'take' ? 'success' : verdict === 'AVOID' ? 'danger' : 'primary'
  const sideWord = side === 'long' ? 'largo' : 'corto'
  const verb = pending
    ? `Dejar ${kindLabel(side, orderType as PaperOrderKind).toLowerCase()}${isNum(orderPrice) ? ` a ${fmtPrice(orderPrice)}` : ''}`
    : `Abrir ${sideWord}`
  const openLabel =
    verdict === 'AVOID'
      ? `${verb} (filtro: NO TOMAR)`
      : verdict === 'WAIT'
        ? `${verb} (filtro: ESPERAR)`
        : sig === 'favorable'
          ? `${verb} (sin señal NEXUS)`
          : verb

  return (
    <Card
      title={
        <>
          <ShieldCheck size={14} /> Nueva orden demo
        </>
      }
      sub="El filtro opina antes de que abras"
    >
      <div className="col gap-2">
        <Field label="Símbolo">
          <div className="pp-symbol">
            <b>{ticker || '—'}</b>
            <span className="tiny muted">{exchange}</span>
            <span className="grow" />
            <IconButton icon={<ChartCandlestick size={14} />} title="Ver gráfico" size="sm" onClick={() => useApp.getState().openChart(symbol, tf)} />
          </div>
          <SymbolSearch onSelect={changeSymbol} placeholder="Cambiar símbolo…" compact />
        </Field>

        <div className="btn-group pp-dir" role="group" aria-label="Lado">
          <button type="button" className={`btn btn-sm ${side === 'long' ? 'active long' : ''}`} onClick={() => changeSide('long')} aria-pressed={side === 'long'}>
            <TrendingUp size={13} /> Largo
          </button>
          <button type="button" className={`btn btn-sm ${side === 'short' ? 'active short' : ''}`} onClick={() => changeSide('short')} aria-pressed={side === 'short'}>
            <TrendingDown size={13} /> Corto
          </button>
        </div>

        <div className="btn-group pp-type" role="group" aria-label="Tipo de orden">
          {TYPE_OPTIONS.map((t) => (
            <button
              key={t.value}
              type="button"
              className={`btn btn-sm ${orderType === t.value ? 'active' : ''}`}
              onClick={() => setOrderType(t.value)}
              aria-pressed={orderType === t.value}
            >
              {t.label}
            </button>
          ))}
        </div>
        {pending && (
          <div className="col gap-1">
            <Field label={`Precio de la ${kindLabel(side, orderType as PaperOrderKind).toLowerCase()}`}>
              <NumberInput value={orderPrice} onChange={setOrderPrice} min={0} placeholder={plain(marketPrice)} />
            </Field>
            <div className="tiny muted">
              {marketPrice !== null ? (
                <>
                  Precio actual en vivo ≈ <span className="mono">{fmtPrice(marketPrice)}</span>.{' '}
                </>
              ) : (
                <>Sin precio en vivo ahora: el servidor comprueba el lado de la orden al dejarla, y solo se ejecuta con precios en vivo (no con datos retrasados). </>
              )}
              {KIND_HELP[orderType as PaperOrderKind]}
            </div>
            {suggested && suggested !== orderType && (
              <Button size="sm" variant="ghost" onClick={() => setOrderType(suggested)}>
                Cambiar a {kindLabel(side, suggested).toLowerCase()}
              </Button>
            )}
            <ExpiryPicker value={expiry} onChange={setExpiry} tf={tf} />
          </div>
        )}

        <div className="pp-grid2">
          <Field label="Temporalidad">
            <Select value={tf} onChange={setTf} options={TF_OPTIONS} />
          </Field>
          <Field label="Riesgo %">
            <NumberInput value={riskPct} onChange={setRiskPct} min={0.05} max={MAX_RISK_PCT} step={0.1} placeholder={riskDefault !== null ? String(riskDefault) : '1'} />
          </Field>
          <Field label="SL · hasta aquí">
            <NumberInput value={sl} onChange={setSl} min={0} placeholder={plain(gate?.sl)} />
          </Field>
          <Field label="Objetivo (TP)">
            <NumberInput value={tp} onChange={setTp} min={0} placeholder={plain(gate?.tp2)} />
          </Field>
        </div>
        <div className="tiny muted">
          Deja SL / objetivo vacíos para usar los niveles de NEXUS (en gris). El tamaño se calcula con el riesgo % y la distancia al SL.
          {pending && ' En una orden pendiente, los niveles de NEXUS y el tamaño se calculan al ejecutarse, desde el precio de ejecución.'}
        </div>
        {(problem || riskProblem) && !(pending && orderPrice === null && !riskProblem) && <div className="tiny neg">{problem ?? riskProblem}</div>}

        <div className="row between">
          <span className="label">{pending ? 'Veredicto del filtro a ese precio' : 'Veredicto del filtro'}</span>
          <IconButton icon={<RefreshCw size={13} />} title="Volver a consultar el filtro" size="sm" onClick={() => void check()} disabled={checking || !!problem} />
        </div>
        {pending && orderPrice === null ? (
          <div className="tiny muted">Indica el precio de la orden para ver qué dice el filtro si entras a ese precio.</div>
        ) : (
          <GatePreview
            gate={gate}
            loading={checking}
            error={gateError}
            onRetry={() => void check()}
            tf={tf}
            action={signalAction}
            actionLabel={signalLabel}
          />
        )}

        <Button
          variant={openVariant}
          size="lg"
          icon={side === 'long' ? <TrendingUp size={15} /> : <TrendingDown size={15} />}
          onClick={() => void order()}
          loading={ordering}
          disabled={!symbol || !!problem || !!riskProblem || (pending && orderPrice === null)}
          title={pending ? 'Deja la orden pendiente en la cuenta demo (simulada)' : 'Abre la operación en la cuenta demo (simulada)'}
        >
          {openLabel}
        </Button>
        <div className="tiny muted">
          {pending
            ? 'Orden pendiente simulada en la cuenta demo (nunca se envía a un bróker). Al ejecutarse se consulta otra vez el filtro: si dice NO TOMAR, la orden se cancela y verás el motivo.'
            : 'Orden simulada a mercado en la cuenta demo. Si el filtro dice NO TOMAR te pediremos confirmación.'}
        </div>
      </div>

      <RiskBlockModal block={riskBlock} onClose={() => setRiskBlock(null)} />

      <Modal
        open={!!conflict}
        onClose={() => setConflict(null)}
        width={620}
        title={
          <span className="row gap-2">
            <ShieldX size={16} className="neg" /> El filtro dice {conflict?.gate?.verdict_label || 'NO TOMAR'}
          </span>
        }
        footer={
          <>
            <Button onClick={() => setConflict(null)} disabled={ordering}>
              Cancelar
            </Button>
            <Button variant="danger" loading={ordering} onClick={() => void order(true)}>
              {pending ? 'Dejar la orden de todos modos' : 'Abrir de todos modos'}
            </Button>
          </>
        }
      >
        {conflict && (
          <div className="col gap-3">
            <div className="row gap-2 wrap">
              <VerdictBadge verdict={conflict.gate?.verdict ?? 'AVOID'} score={conflict.gate?.score} label={conflict.gate?.verdict_label || undefined} large />
              <span className="small text-2">
                {side === 'long' ? 'Largo' : 'Corto'} en {ticker} · {tf}
              </span>
            </div>
            <p className="pp-gate-summary">{conflict.message}</p>
            {conflict.gate?.summary && !conflict.message.includes(conflict.gate.summary) && (
              <p className="small text-2" style={{ margin: 0 }}>
                {conflict.gate.summary}
              </p>
            )}
            {conflict.gate && <GateChecklist gate={conflict.gate} />}
            <p className="tiny muted" style={{ margin: 0 }}>
              {pending
                ? 'Dejar una orden que el filtro rechaza a ese precio es decisión tuya. Cuando el precio llegue se consultará otra vez: si sigue diciendo NO TOMAR, la orden se cancela sin abrir nada. Es una orden simulada (cuenta demo).'
                : 'Abrir una operación que el filtro rechaza es decisión tuya. Es una operación simulada; el historial de la cuenta demo compara cómo rinden las operaciones abiertas contra el filtro frente a las aprobadas.'}
            </p>
          </div>
        )}
      </Modal>
    </Card>
  )
}

export default memo(OrderCard)
