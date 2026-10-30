// Bottom panel of the editor: "Problemas" (click to jump to the line), "Consola" (stdout of the last run) and
// "Alertas" (alertcondition hits of the last run).
import { memo } from 'react'
import { Bell, ChevronDown, ChevronUp, CircleCheck, CircleX, ShieldAlert, Timer, TriangleAlert } from 'lucide-react'
import type { ScriptRunResult } from '../../api/types'
import { fmtBarTime, fmtInt } from '../../lib/format'
import { IconButton } from '../ui'
import { problemLabel, type Problem } from './diagnostics'

export type BottomTab = 'problems' | 'console' | 'alerts'

interface Props {
  tab: BottomTab
  onTab: (t: BottomTab) => void
  collapsed: boolean
  onToggle: () => void
  problems: Problem[]
  warnings: string[]
  run: ScriptRunResult | null
  runInfo: string | null
  compileError: string | null
  onGoto: (line: number, col: number | null) => void
}

function BottomPanel(props: Props) {
  const { tab, problems, warnings, run } = props
  const alerts = run?.alerts ?? []
  const stdout = run?.stdout ?? ''
  const nProblems = problems.length

  const tabs: { id: BottomTab; label: string; count: number | null; tone?: string }[] = [
    { id: 'problems', label: 'Problemas', count: nProblems + warnings.length, tone: nProblems ? 'neg' : warnings.length ? 'warn' : undefined },
    { id: 'console', label: 'Consola', count: null },
    { id: 'alerts', label: 'Alertas', count: alerts.length, tone: alerts.length ? 'accent' : undefined },
  ]

  return (
    <div className={`studio-bottom ${props.collapsed ? 'collapsed' : ''}`}>
      <div className="studio-bottom-tabs" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`studio-btab ${tab === t.id && !props.collapsed ? 'active' : ''}`}
            onClick={() => {
              props.onTab(t.id)
              if (props.collapsed) props.onToggle()
            }}
          >
            {t.label}
            {t.count !== null && t.count > 0 && <span className={`studio-count ${t.tone ?? ''}`}>{t.count}</span>}
          </button>
        ))}
        <div className="grow" />
        {props.runInfo && (
          <span className="tiny muted row gap-1 nowrap" style={{ paddingRight: 6 }}>
            <Timer size={12} /> {props.runInfo}
          </span>
        )}
        <IconButton
          size="sm"
          icon={props.collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          title={props.collapsed ? 'Mostrar panel' : 'Ocultar panel'}
          onClick={props.onToggle}
        />
      </div>
      {!props.collapsed && (
        <div className="studio-bottom-body scroll">
          {tab === 'problems' && (
            <>
              {props.compileError && (
                <div className="studio-problem warn">
                  <TriangleAlert size={13} />
                  <span>No se pudo compilar: {props.compileError}</span>
                </div>
              )}
              {problems.map((p, i) => (
                <button
                  key={`${p.origin}-${i}`}
                  type="button"
                  className="studio-problem neg clickable"
                  onClick={() => typeof p.line === 'number' && props.onGoto(p.line, p.col)}
                  disabled={typeof p.line !== 'number'}
                  title={typeof p.line === 'number' ? 'Ir a la línea' : undefined}
                >
                  {p.kind === 'security' ? <ShieldAlert size={13} /> : <CircleX size={13} />}
                  <span className="studio-problem-loc mono">{typeof p.line === 'number' ? `L${p.line}${typeof p.col === 'number' && p.col > 0 ? `:${p.col}` : ''}` : '—'}</span>
                  <span className="studio-problem-kind">{problemLabel(p)}</span>
                  <span className="studio-problem-msg">{p.message}</span>
                </button>
              ))}
              {warnings.map((w, i) => (
                <div key={`w-${i}`} className="studio-problem warn">
                  <TriangleAlert size={13} />
                  <span className="studio-problem-msg">{w}</span>
                </div>
              ))}
              {!nProblems && !warnings.length && !props.compileError && (
                <div className="studio-problem ok">
                  <CircleCheck size={13} /> Sin problemas detectados.
                </div>
              )}
            </>
          )}
          {tab === 'console' &&
            (stdout.trim() ? (
              <pre className="studio-console">{stdout}</pre>
            ) : (
              <div className="studio-problem muted">
                {run ? 'El script no imprimió nada. Usa print(...) para escribir aquí.' : 'Ejecuta el script (Ctrl+Enter) para ver su salida.'}
              </div>
            ))}
          {tab === 'alerts' &&
            (alerts.length ? (
              <table className="table studio-alerts">
                <thead>
                  <tr>
                    <th>Vela</th>
                    <th>Alerta</th>
                    <th>Mensaje</th>
                  </tr>
                </thead>
                <tbody>
                  {[...alerts]
                    .sort((a, b) => b.time - a.time)
                    .slice(0, 500)
                    .map((a, i) => (
                      <tr key={`${a.time}-${i}`}>
                        <td className="mono">{fmtBarTime(a.time)}</td>
                        <td>
                          <span className="row gap-1">
                            <Bell size={12} className="accent" /> {a.title}
                          </span>
                        </td>
                        <td className="studio-wrap">{a.message}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            ) : (
              <div className="studio-problem muted">
                {run ? 'Ninguna alertcondition(...) se activó en las velas analizadas.' : 'Ejecuta el script para ver las alertas.'}
              </div>
            ))}
          {tab === 'alerts' && alerts.length > 500 && <div className="tiny muted" style={{ padding: 6 }}>Mostrando las 500 más recientes de {fmtInt(alerts.length)}.</div>}
        </div>
      )}
    </div>
  )
}

export default memo(BottomPanel)
