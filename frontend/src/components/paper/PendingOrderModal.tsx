// "Dejar orden límite / stop a X": leaves an idea with its own entry as a pending demo order (from "¿Tomo esta
// operación?"). Shows what will happen (limit when touched, stop when crossed, the gate asked again at the fill), the
// validity, and handles the server's 409s: the gate's NO TOMAR at the order price (explicit "Dejar de todos modos")
// and a portfolio-risk limit (cannot be forced).
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Hourglass, ShieldX } from 'lucide-react'
import { ApiError, errorMessage } from '../../api/client'
import type { Direction, GateResult, PaperOrderKind, PaperRiskBlock, Timeframe } from '../../api/types'
import { fmtNum, fmtPrice, splitSymbol } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Modal, VerdictBadge } from '../ui'
import GateChecklist from '../nexus/GateChecklist'
import ExpiryPicker from './ExpiryPicker'
import RiskBlockModal from './RiskBlockModal'
import { paperOrdersApi } from './ordersApi'
import { DEFAULT_EXPIRY, KIND_HELP, expiryRequest, kindLabel, riskBlockDetail, validatePendingOrder, type ExpiryDraft } from './ordersUtils'
import { conflictDetail } from './paperUtils'

interface Props {
  open: boolean
  symbol: string
  tf: Timeframe
  side: Direction
  kind: PaperOrderKind
  price: number
  sl: number | null
  tp: number | null
  /** last known market price (side check before sending; the server checks it again) */
  market: number | null
  /** the idea's verified verdict at this entry with the same levels, if any */
  verified: GateResult | null
  onClose: () => void
  onPlaced?: () => void
}

function PendingOrderModal(props: Props) {
  const { open, symbol, tf, side, kind, price, sl, tp, market, verified, onClose, onPlaced } = props
  const notify = useApp((s) => s.notify)
  const [expiry, setExpiry] = useState<ExpiryDraft>(DEFAULT_EXPIRY)
  const [placing, setPlacing] = useState(false)
  const [conflict, setConflict] = useState<{ message: string; gate: GateResult | null } | null>(null)
  const [riskBlock, setRiskBlock] = useState<PaperRiskBlock | null>(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])
  useEffect(() => {
    if (open) setConflict(null)
  }, [open])

  const check = useMemo(() => expiryRequest(expiry, Math.floor(Date.now() / 1000)), [expiry])
  const problem = validatePendingOrder(side, kind, price, market, sl, tp) ?? (check.ok ? null : check.problem)
  const label = kindLabel(side, kind)
  const ticker = splitSymbol(symbol).ticker

  const place = async (force = false) => {
    if (problem || !check.ok) return
    setPlacing(true)
    try {
      const res = await paperOrdersApi.place({
        symbol,
        side,
        kind,
        price,
        tf,
        sl: sl ?? undefined,
        tp: tp ?? undefined,
        ...check.fields,
        ...(force ? { force: true } : {}),
      })
      if (!alive.current) return
      notify(
        `Orden demo pendiente: ${label.toLowerCase()} ${ticker} a ${fmtPrice(price)} · ${res?.order?.expiry_text ?? 'válida hasta cancelarla'}${
          force ? ' (contra el filtro: se revisa otra vez al ejecutarse)' : ''
        }`,
        'trade',
      )
      setConflict(null)
      onPlaced?.()
      onClose()
    } catch (err) {
      if (!alive.current) return
      const block = err instanceof ApiError && err.status === 409 ? riskBlockDetail(err.data) : null
      if (block) {
        setRiskBlock(block)
      } else if (err instanceof ApiError && err.status === 409 && !force) {
        const d = conflictDetail(err.data)
        setConflict({ message: d.message ?? err.message, gate: d.gate })
      } else {
        notify(`No se pudo dejar la orden demo: ${errorMessage(err)}`, 'error')
      }
    } finally {
      if (alive.current) setPlacing(false)
    }
  }

  return (
    <>
      <Modal
        open={open}
        onClose={() => (placing ? undefined : onClose())}
        width={600}
        title={
          <span className="row gap-2">
            {conflict ? <ShieldX size={16} className="neg" /> : <Hourglass size={16} />}{' '}
            {conflict ? `El filtro dice ${conflict.gate?.verdict_label || 'NO TOMAR'} a ${fmtPrice(price)}` : `Dejar ${label.toLowerCase()} a ${fmtPrice(price)}`}
          </span>
        }
        footer={
          <>
            <Button onClick={onClose} disabled={placing}>
              Cancelar
            </Button>
            {conflict ? (
              <Button variant="danger" loading={placing} onClick={() => void place(true)}>
                Dejar la orden de todos modos
              </Button>
            ) : (
              <Button variant="primary" loading={placing} disabled={!!problem} onClick={() => void place(false)}>
                Dejar orden demo
              </Button>
            )}
          </>
        }
      >
        <div className="col gap-3">
          <div className="chart-tc-compare">
            <span className="head" />
            <span className="head">Orden</span>
            <span className="head">Mercado</span>
            <span>Entrada</span>
            <span className="mono">{fmtPrice(price)}</span>
            <span className="mono">{market !== null ? `≈ ${fmtPrice(market)}` : '—'}</span>
            <span>SL · hasta aquí</span>
            <span className="mono">{sl !== null ? fmtPrice(sl) : 'NEXUS al ejecutarse'}</span>
            <span />
            <span>Objetivo</span>
            <span className="mono">{tp !== null ? fmtPrice(tp) : 'NEXUS al ejecutarse'}</span>
            <span />
            <span>Filtro</span>
            <span>{verified ? `${verified.verdict_label} ${Math.round(verified.score)}/100 (verificada)` : 'se consulta al dejarla'}</span>
            <span className="mono">{verified ? `R:R ${fmtNum(verified.rr, 2)}` : ''}</span>
          </div>
          <div className="tiny muted">{KIND_HELP[kind]}</div>
          {!conflict && <ExpiryPicker value={expiry} onChange={setExpiry} tf={tf} />}
          {problem && <div className="small neg">{problem}</div>}
          {conflict && (
            <>
              <div className="row gap-2 wrap">
                <VerdictBadge verdict={conflict.gate?.verdict ?? 'AVOID'} score={conflict.gate?.score} label={conflict.gate?.verdict_label || undefined} large />
                <span className="small text-2">
                  {label} en {ticker} · {tf}
                </span>
              </div>
              <p className="chart-advice">{conflict.message}</p>
              {conflict.gate && <GateChecklist gate={conflict.gate} />}
            </>
          )}
          <p className="tiny muted" style={{ margin: 0 }}>
            Orden simulada de la cuenta demo: nunca se envía a un bróker. Solo se ejecuta con precios en vivo (no con datos retrasados ni
            recuperados tras un corte del feed). Cuando el precio llegue, se consulta otra vez el filtro al precio de ejecución: si dice NO
            TOMAR o no responde, la orden se cancela y verás el motivo. Los límites de riesgo de cartera también se comprueban entonces.
            {sl === null || tp === null ? ' Sin SL u objetivo propios, se usan los niveles de NEXUS calculados desde el precio de ejecución.' : ''}
          </p>
        </div>
      </Modal>
      <RiskBlockModal block={riskBlock} onClose={() => setRiskBlock(null)} />
    </>
  )
}

export default memo(PendingOrderModal)
