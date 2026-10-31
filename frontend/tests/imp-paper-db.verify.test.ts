// paper-db (verifier): honest wording of the demo account's pending orders. The server fills pending orders with live
// prices only (a bar replayed after a feed stall or delayed data never fills one), and cancels the ones that reach
// their price while the daily loss limit blocks the day: the ticket, the "Dejar orden" modal of "¿Tomo esta
// operación?", the risk modal and the settings help say so. The risk panel never shows a "-0,00 $" loss.
// Run by tests/imp-paper-db.run.mjs (npm test).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import type { PaperRisk } from '../src/api/types'
import { dailyLossMoney } from '../src/components/paper/ordersUtils'

// same formatting as fmtMoney (components/markets/opsUtils, not importable here: it pulls in the API client)
const fmtMoney = (v: number) => `${v.toLocaleString('es-ES', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} $`

const repo = process.env.PAPER_DB_REPO ?? join(process.cwd(), '..')
const source = (rel: string) => readFileSync(join(repo, 'frontend', 'src', ...rel.split('/')), 'utf-8')

test('pending demo orders say they only fill with live prices (never delayed or recovered data)', () => {
  assert.match(source('components/paper/OrderCard.tsx'), /solo se ejecuta con precios en vivo \(no con datos retrasados\)/)
  const modal = source('components/paper/PendingOrderModal.tsx')
  assert.match(modal, /Solo se ejecuta con precios en vivo \(no con datos retrasados ni/)
  assert.match(modal, /nunca se envía a un bróker/)
})

test('the daily loss block says that pending orders reaching their price meanwhile are cancelled (not deferred)', async () => {
  // the server cancels a pending order that triggers while the day is blocked (reason "No se ejecutó al llegar a …")
  const { GROUPS_EXT } = await import('../src/components/settings/schema-paper-risk')
  const daily = GROUPS_EXT.flatMap((g) => g.fields).find((f) => f.key === 'paper.daily_loss_limit_pct')
  assert.match(daily?.help ?? '', /las órdenes pendientes que lleguen a su precio antes se cancelan/)
  assert.match(source('components/paper/RiskBlockModal.tsx'), /hasta las 00:00 UTC: las que lleguen a su precio antes se cancelan/)
  // the backend's block reason (Python string literals joined across lines)
  const py = readFileSync(join(repo, 'backend', 'app', 'sim', 'paper.py'), 'utf-8').replace(/"\s*\n\s*"/g, '')
  assert.match(py, /ni ejecuta órdenes pendientes: las que lleguen a su precio antes se cancelan"/)
})

test('the risk panel shows the day loss as 0,00 $ without a loss (never -0,00 $) and the worst loss otherwise', () => {
  const risk = (daily_pnl: number, daily_low: number) => ({ daily_pnl, daily_low }) as unknown as PaperRisk
  assert.ok(Object.is(dailyLossMoney(risk(0, 0)), 0))
  assert.equal(fmtMoney(dailyLossMoney(risk(0, 0))), '0,00 $') // before the fix the panel showed "-0,00 $"
  assert.equal(fmtMoney(dailyLossMoney(risk(120.5, 0))), '0,00 $') // a winning day consumes nothing
  assert.equal(dailyLossMoney(risk(-50, -310.25)), -310.25) // latched on the worst moment of the day
  assert.equal(dailyLossMoney(risk(-80, 0)), -80)
  const panel = source('components/paper/RiskPanel.tsx')
  assert.match(panel, /fmtMoney\(dailyLossMoney\(risk\)\)/)
  assert.doesNotMatch(panel, /fmtMoney\(-loss\)/)
})
