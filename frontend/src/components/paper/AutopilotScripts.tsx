// "Estrategias de PyChart" in NEXUS automática: saved PyChart strategies that also give it entries when NEXUS's own
// signal has none (autopilot.script_ids, at most 3), with or without the gate's TOMAR (autopilot.script_gate). Their
// positions follow their own stop, target and close rules. Each change is saved at once; the server checks that every
// script is a strategy without errors (400 in Spanish otherwise). Not validated by NEXUS: its journal measures them.
import { useEffect, useRef, useState } from 'react'
import { FileCode, TriangleAlert } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { AutopilotScript, AutopilotStatus, AutopilotUpdate, ScriptMeta } from '../../api/types'
import { fmtAgo } from '../../lib/format'
import { useApp } from '../../store'
import { Button, Card, Toggle } from '../ui'

export const MAX_STRATEGIES = 3

/** Defensive parse of AutopilotStatus.scripts. */
export function normalizeScripts(raw: unknown): AutopilotScript[] {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((x): x is Record<string, unknown> => !!x && typeof x === 'object' && typeof (x as Record<string, unknown>).id === 'number')
    .map((x) => ({
      id: x.id as number,
      name: typeof x.name === 'string' && x.name ? x.name : `Script ${x.id as number}`,
      error: typeof x.error === 'string' && x.error ? x.error : null,
      checked_at: typeof x.checked_at === 'number' ? x.checked_at : null,
    }))
}

/** The ids after ticking / unticking one (never more than MAX_STRATEGIES). */
export function toggleId(ids: number[], id: number): number[] {
  if (ids.includes(id)) return ids.filter((x) => x !== id)
  return ids.length >= MAX_STRATEGIES ? ids : [...ids, id]
}

export interface ScriptsViewProps {
  status: AutopilotStatus
  scripts: ScriptMeta[] | null // the saved scripts (null while loading)
  listError: string | null
  error: string | null // the server's answer to the last refused change
  saving: boolean
  onToggle: (id: number) => void
  onGate: (on: boolean) => void
  onStudio: () => void
}

/** The card without its state (rendered by the tests). */
export function ScriptsView({ status, scripts, listError, error, saving, onToggle, onGate, onStudio }: ScriptsViewProps) {
  const chosen = status.script_ids
  const known = new Map(status.scripts.map((s) => [s.id, s]))
  const full = chosen.length >= MAX_STRATEGIES
  return (
    <Card
      className="ap-scripts"
      title={
        <span className="row gap-2">
          <FileCode size={15} /> Estrategias de PyChart
        </span>
      }
      sub="Opcional: también le dan entradas cuando NEXUS no tiene señal"
      actions={
        <Button size="sm" variant="ghost" onClick={onStudio} title="Abrir el editor de PyChart">
          Abrir PyChart
        </Button>
      }
    >
      <p className="tiny muted" style={{ margin: '0 0 8px' }}>
        NEXUS automática opera también las entradas de las estrategias que marques, con su stop, su objetivo y sus reglas de cierre,
        al cierre de cada vela y en sus mismos mercados. No están validadas: su diario las mide aparte («Fuente de la señal») frente a
        entrar al azar, y «Aprender de sus pérdidas» deja de usar la que pierda con evidencia (30 operaciones o más). Pruébalas antes en
        el laboratorio del copiloto o en PyChart.
      </p>
      {error && <div className="notice ap-brake small">{error}</div>}
      {listError && <div className="notice ap-brake small">{listError}</div>}
      {scripts === null && !listError ? <div className="tiny muted">Cargando tus scripts…</div> : null}
      {scripts !== null && scripts.length === 0 ? (
        <div className="tiny muted">Todavía no tienes scripts de PyChart: crea uno en PyChart o pídeselo al copiloto.</div>
      ) : null}
      {scripts && scripts.length > 0 && (
        <div className="ap-scripts-list">
          {scripts.map((s) => {
            const on = chosen.includes(s.id)
            const info = known.get(s.id)
            return (
              <label key={s.id} className={`ap-script-row small ${on ? 'on' : ''}`}>
                <input type="checkbox" checked={on} disabled={saving || (!on && full)} onChange={() => onToggle(s.id)} />
                <span className="ap-script-name">
                  {s.name}
                  {s.is_example ? <span className="tiny muted"> · ejemplo</span> : null}
                </span>
                {on && info?.error ? (
                  <span className="tiny ap-script-error" title={info.error}>
                    <TriangleAlert size={12} /> {info.error}
                  </span>
                ) : on && info?.checked_at ? (
                  <span className="tiny muted">revisada {fmtAgo(info.checked_at)}</span>
                ) : null}
              </label>
            )
          })}
        </div>
      )}
      {full && <div className="tiny muted">Como mucho {MAX_STRATEGIES} a la vez: cada una se ejecuta en cada vela de cada mercado.</div>}
      <Toggle
        checked={status.script_gate}
        disabled={saving}
        onChange={(v) => onGate(v)}
        label={<span className={status.script_gate ? 'bold' : 'text-2'}>Sus entradas también pasan por el filtro TOMAR</span>}
      />
    </Card>
  )
}

export interface AutopilotScriptsProps {
  status: AutopilotStatus
  saving: boolean
  error: string | null
  onSave: (update: AutopilotUpdate) => Promise<AutopilotStatus | null>
}

export default function AutopilotScripts({ status, saving, error, onSave }: AutopilotScriptsProps) {
  const [scripts, setScripts] = useState<ScriptMeta[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [mine, setMine] = useState(false) // the last refused change came from this card
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    void (async () => {
      try {
        const res = await api.scripts.list()
        if (alive.current) setScripts(Array.isArray(res?.scripts) ? res.scripts : [])
      } catch (err) {
        if (alive.current) setListError(`No se pudieron cargar tus scripts: ${errorMessage(err)}`)
      }
    })()
    return () => {
      alive.current = false
    }
  }, [])

  const save = async (update: AutopilotUpdate) => {
    const res = await onSave(update)
    if (alive.current) setMine(res === null)
  }

  return (
    <ScriptsView
      status={status}
      scripts={scripts}
      listError={listError}
      error={mine ? error : null}
      saving={saving}
      onToggle={(id) => void save({ script_ids: toggleId(status.script_ids, id) })}
      onGate={(on) => void save({ script_gate: on })}
      onStudio={() => useApp.getState().setPage('studio')}
    />
  )
}
