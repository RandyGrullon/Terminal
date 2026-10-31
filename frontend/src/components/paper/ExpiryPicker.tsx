// Validity of a pending demo order: until cancelled, N bars of its timeframe, or until a date (local time).
import { memo } from 'react'
import type { PaperOrderExpiry } from '../../api/types'
import { Field, NumberInput, Select, TextInput } from '../ui'
import { MAX_EXPIRY_BARS, unixToLocalInput, type ExpiryDraft } from './ordersUtils'

const MODES: { value: PaperOrderExpiry; label: string }[] = [
  { value: 'gtc', label: 'Hasta cancelarla' },
  { value: 'bars', label: 'Durante N velas' },
  { value: 'date', label: 'Hasta una fecha' },
]

function ExpiryPicker({ value, onChange, tf }: { value: ExpiryDraft; onChange: (v: ExpiryDraft) => void; tf: string }) {
  const setMode = (mode: PaperOrderExpiry) => {
    // a sensible default date: tomorrow at this time
    const date = mode === 'date' && !value.date ? unixToLocalInput(Math.floor(Date.now() / 1000) + 86400) : value.date
    onChange({ ...value, mode, date })
  }
  return (
    <div className="pp-expiry">
      <Field label="Validez">
        <Select value={value.mode} onChange={setMode} options={MODES} aria-label="Validez de la orden" />
      </Field>
      {value.mode === 'bars' && (
        <Field label={`Velas de ${tf}`}>
          <NumberInput value={value.bars} onChange={(bars) => onChange({ ...value, bars })} min={1} max={MAX_EXPIRY_BARS} step={1} placeholder="3" />
        </Field>
      )}
      {value.mode === 'date' && (
        <Field label="Caduca (hora local)">
          <TextInput type="datetime-local" value={value.date} onChange={(date) => onChange({ ...value, date })} aria-label="Fecha de caducidad" />
        </Field>
      )}
    </div>
  )
}

export default memo(ExpiryPicker)
