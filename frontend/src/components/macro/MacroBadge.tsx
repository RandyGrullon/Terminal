// Inline badge for the chart toolbar: the semáforo scoped to the charted symbol (its currencies / asset class)
// with the short label and a countdown to the next relevant high-impact event. Tooltip: headline and advice.
// REST only (60 s refresh), no WS; renders nothing if it ever fails to render. Pass `state` (useMacro(symbol)) when
// the page also reacts to the level (the chart refetches NEXUS on changes); otherwise the badge loads it itself.
import { splitSymbol } from '../../lib/format'
import { MacroBoundary } from './MacroBoundary'
import { blackoutText, pulseCountdown, scopeLabel } from './macroFormat'
import { LEVEL_META } from './macroModel'
import { useMacro, type MacroState } from './useMacro'
import './macro.css'

export function MacroBadge(props: { symbol: string; state?: MacroState }) {
  return (
    <MacroBoundary fallback={null}>
      {props.state ? <BadgeView symbol={props.symbol} m={props.state} /> : <BadgeContent symbol={props.symbol} />}
    </MacroBoundary>
  )
}

function BadgeContent(props: { symbol: string }) {
  return <BadgeView symbol={props.symbol} m={useMacro(props.symbol)} />
}

function BadgeView(props: { symbol: string; m: MacroState }) {
  const m = props.m
  const c = m.conditions
  const ticker = splitSymbol(props.symbol).ticker || props.symbol

  if (!c) {
    const title = m.loading
      ? 'Cargando condiciones de mercado…'
      : 'Sin datos de condiciones de mercado para este símbolo. Opera con cautela.'
    return (
      <span className="mb-badge lv-none" title={title}>
        <span className="mp-dot" aria-hidden="true" />
        <span className="mb-label">{m.loading ? 'Macro…' : 'Macro: sin datos'}</span>
      </span>
    )
  }

  const meta = LEVEL_META[c.level]
  const count = pulseCountdown(c, m.now)
  const scope = c.scope && c.scope !== 'global' ? ` (${scopeLabel(c.scope)})` : ''
  const lines = [`Semáforo ${ticker}${scope}: ${c.label}`, c.headline]
  if (c.advice) lines.push(c.advice)
  const blackout = blackoutText(c, m.now)
  if (blackout) lines.push(blackout)
  if (m.stale) lines.push('(Datos desactualizados)')

  return (
    <span
      className={`mb-badge lv-${c.level}${m.stale ? ' mp-stale' : ''}`}
      title={lines.join('\n')}
      aria-label={`Semáforo de mercado para ${ticker}: ${meta.short}. ${c.headline}`}
    >
      <span className="mp-dot" aria-hidden="true" />
      <span className="mb-label">{meta.short}</span>
      {count && <span className="mb-count">{count}</span>}
    </span>
  )
}
