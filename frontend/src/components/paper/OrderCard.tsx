// New demo order ticket: symbol, side, timeframe, risk % and optional SL / TP. The Trade Gate verdict is shown BEFORE
// submitting; if the server answers 409 (filter says NO TOMAR) the user must explicitly confirm "Abrir de todos modos".
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ChartCandlestick, RefreshCw, ShieldCheck, ShieldX, TrendingDown, TrendingUp } from 'lucide-react'
import { ApiError, api, errorMessage } from '../../api/client'
import type { Direction, GateResult, Timeframe } from '../../api/types'
import { TIMEFRAMES } from '../../api/types'
import { fmtPrice, priceDecimals, splitSymbol } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Card, Field, IconButton, Modal, NumberInput, Select, VerdictBadge } from '../ui'
import SymbolSearch from '../SymbolSearch'
import GateChecklist from '../nexus/GateChecklist'
import { isNum } from '../markets/opsUtils'
import GatePreview from './GatePreview'
import { conflictDetail, isGateResult, validateLevels } from './paperUtils'

interface Preview {
  key: string // symbol|side|tf the verdict belongs to
  gate: GateResult
}

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
  const alive = useRef(true)
  const reqRef = useRef(0)

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

  const key = `${symbol}|${side}|${tf}`
  const gate = preview && preview.key === key ? preview.gate : null
  const refPrice = gate && isNum(gate.entry) ? gate.entry : null
  const riskProblem = riskPct !== null && !(riskPct > 0 && riskPct <= MAX_RISK_PCT) ? `El riesgo debe estar entre 0 y ${MAX_RISK_PCT} %.` : null
  const problem = useMemo(() => validateLevels(side, refPrice, sl, tp), [side, refPrice, sl, tp])

  const check = useCallback(async () => {
    if (!symbol) return
    const id = ++reqRef.current
    const k = `${symbol}|${side}|${tf}`
    setChecking(true)
    setGateError(null)
    try {
      const res = await api.gateCheck({ symbol, tf, direction: side, sl: sl ?? undefined, tp: tp ?? undefined })
      if (!alive.current || id !== reqRef.current) return
      if (!isGateResult(res)) throw new Error('Respuesta del filtro no válida')
      setPreview({ key: k, gate: res })
    } catch (err) {
      if (!alive.current || id !== reqRef.current) return
      setGateError(errorMessage(err))
    } finally {
      if (alive.current && id === reqRef.current) setChecking(false)
    }
  }, [symbol, side, tf, sl, tp])

  // Re-check the gate (debounced) whenever the idea changes and is valid.
  useEffect(() => {
    if (!symbol || problem) return
    const t = window.setTimeout(() => void check(), PREVIEW_DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [symbol, side, tf, sl, tp, problem, check])

  const changeSymbol = (s: string) => {
    const next = s.trim().toUpperCase()
    if (!next || next === symbol) return
    setSymbol(next)
    setSl(null)
    setTp(null)
    setGateError(null)
  }

  const changeSide = (d: Direction) => {
    if (d === side) return
    setSide(d)
    setSl(null)
    setTp(null)
  }

  const order = async (force = false) => {
    if (!symbol || problem || riskProblem) return
    setOrdering(true)
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
      if (err instanceof ApiError && err.status === 409 && !force) {
        const d = conflictDetail(err.data)
        setConflict({ message: d.message ?? err.message, gate: d.gate ?? gate })
      } else {
        notify(`No se pudo abrir la orden demo: ${errorMessage(err)}`, 'error')
      }
    } finally {
      if (alive.current) setOrdering(false)
    }
  }

  const { exchange, ticker } = splitSymbol(symbol)
  const verdict = gate?.verdict ?? null
  const openVariant = verdict === 'TAKE' ? 'success' : verdict === 'AVOID' ? 'danger' : 'primary'
  const sideWord = side === 'long' ? 'largo' : 'corto'
  const openLabel =
    verdict === 'AVOID' ? `Abrir ${sideWord} (filtro: NO TOMAR)` : verdict === 'WAIT' ? `Abrir ${sideWord} (filtro: ESPERAR)` : `Abrir ${sideWord}`

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
        </div>
        {(problem || riskProblem) && <div className="tiny neg">{problem ?? riskProblem}</div>}

        <div className="row between">
          <span className="label">Veredicto del filtro</span>
          <IconButton icon={<RefreshCw size={13} />} title="Volver a consultar el filtro" size="sm" onClick={() => void check()} disabled={checking || !!problem} />
        </div>
        <GatePreview gate={gate} loading={checking} error={gateError} onRetry={() => void check()} tf={tf} />

        <Button
          variant={openVariant}
          size="lg"
          icon={side === 'long' ? <TrendingUp size={15} /> : <TrendingDown size={15} />}
          onClick={() => void order()}
          loading={ordering}
          disabled={!symbol || !!problem || !!riskProblem}
          title="Abre la operación en la cuenta demo (simulada)"
        >
          {openLabel}
        </Button>
        <div className="tiny muted">Orden simulada a mercado en la cuenta demo. Si el filtro dice NO TOMAR te pediremos confirmación.</div>
      </div>

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
              Abrir de todos modos
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
              Abrir una operación que el filtro rechaza es decisión tuya. Es una operación simulada; el historial de la cuenta demo compara cómo rinden
              las operaciones abiertas contra el filtro frente a las aprobadas.
            </p>
          </div>
        )}
      </Modal>
    </Card>
  )
}

export default memo(OrderCard)
