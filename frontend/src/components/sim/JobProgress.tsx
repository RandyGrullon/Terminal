// Spinner banner with elapsed seconds for long-running backtest / Monte Carlo requests.
import { useEffect, useState } from 'react'
import { Spinner } from '../ui'

export function JobProgress(props: { label: string; startedAt: number | null; hint?: string }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])
  const secs = props.startedAt ? Math.max(0, Math.round((now - props.startedAt) / 1000)) : 0
  return (
    <div className="sim-loading" role="status" aria-live="polite">
      <Spinner />
      <span className="grow">{props.label}</span>
      {props.hint && <span className="tiny muted">{props.hint}</span>}
      <span className="mono small muted">{secs} s</span>
    </div>
  )
}
