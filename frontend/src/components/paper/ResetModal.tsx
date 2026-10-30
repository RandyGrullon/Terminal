// Confirmation dialog to reset the demo account with a chosen initial balance (closes positions, clears history).
import { memo, useEffect, useState } from 'react'
import { RotateCcw, TriangleAlert } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { PaperSnapshot } from '../../api/types'
import { Button, ErrorBox, Field, Modal, NumberInput } from '../ui'
import { fmtMoney, isNum } from '../markets/opsUtils'
import { normalizeSnapshot } from './paperUtils'

const MIN_BALANCE = 100
const MAX_BALANCE = 100_000_000
const PRESETS = [1_000, 10_000, 50_000, 100_000]

interface Props {
  open: boolean
  currentInitial: number | null
  openPositions: number
  onClose: () => void
  onDone: (snap: PaperSnapshot | null) => void
}

function ResetModal({ open, currentInitial, openPositions, onClose, onDone }: Props) {
  const [balance, setBalance] = useState<number | null>(currentInitial)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open) {
      setBalance(isNum(currentInitial) && currentInitial > 0 ? currentInitial : 10_000)
      setError(null)
    }
  }, [open, currentInitial])

  const invalid = balance === null || !(balance >= MIN_BALANCE && balance <= MAX_BALANCE)

  const confirm = async () => {
    if (invalid || balance === null) return
    setBusy(true)
    setError(null)
    try {
      const res = await api.paper.reset(balance)
      onDone(normalizeSnapshot(res))
      onClose()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onClose}
      width={460}
      title={
        <span className="row gap-2">
          <RotateCcw size={16} /> Reiniciar cuenta demo
        </span>
      }
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>
            Cancelar
          </Button>
          <Button variant="danger" onClick={() => void confirm()} loading={busy} disabled={invalid}>
            Reiniciar
          </Button>
        </>
      }
    >
      <div className="col gap-3">
        <div className="notice">
          <TriangleAlert size={15} style={{ flex: 'none', marginTop: 1 }} />
          <span>
            Se cerrarán {openPositions > 0 ? `las ${openPositions} posiciones abiertas` : 'todas las posiciones'} y se borrará el historial de la
            cuenta demo. El aprendizaje de NEXUS y del filtro no se pierde.
          </span>
        </div>
        <Field label="Balance inicial ($)" hint={`Entre ${fmtMoney(MIN_BALANCE)} y ${fmtMoney(MAX_BALANCE)}`}>
          <NumberInput value={balance} onChange={setBalance} min={MIN_BALANCE} max={MAX_BALANCE} step={1000} placeholder="10000" />
        </Field>
        <div className="row gap-1 wrap">
          {PRESETS.map((v) => (
            <Button key={v} size="sm" variant="ghost" active={balance === v} onClick={() => setBalance(v)}>
              {fmtMoney(v)}
            </Button>
          ))}
        </div>
        {error && <ErrorBox error={error} onRetry={() => void confirm()} />}
      </div>
    </Modal>
  )
}

export default memo(ResetModal)
