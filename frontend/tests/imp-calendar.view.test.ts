// Economic calendar beyond the current week (semáforo panel): this week + next week view, source tags of the
// official calendars, coverage notes, defensive normalization and pill names of official releases. Times are built
// with local-time Date constructors, so the expectations hold in any machine time zone.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { MacroEvent, MarketConditions } from '../src/api/types'
import { addDays, buildCalendarView, calendarDays, weekStart } from '../src/components/macro/calendarView'
import {
  eventShortName,
  fmtHM,
  fmtIsoDay,
  fmtShortDay,
  isApproxTime,
  isOfficialSource,
  officialNote,
  pulseCountdown,
  sourceLabel,
  sourceTag,
} from '../src/components/macro/macroFormat'
import { normalizeCalendar, normalizeCoverage } from '../src/components/macro/macroModel'

const local = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi).getTime()
const sec = (ms: number) => Math.floor(ms / 1000)
const FRIDAY = local(2026, 10, 30, 12, 0)
const OFFICIAL = 'Fed, BLS, BEA, BCE, BoE y BoJ'

function ev(over: Partial<MacroEvent> & { time: number }): MacroEvent {
  return {
    id: `e-${over.time}-${over.source ?? 'forexfactory'}`,
    title: 'X',
    title_es: 'Evento',
    currency: 'USD',
    impact: 'high',
    forecast: null,
    previous: null,
    source: 'forexfactory',
    countdown_s: 0,
    ...over,
  }
}

test('weeks start on Monday and the request reaches the end of next week', () => {
  assert.equal(weekStart(FRIDAY), local(2026, 10, 26))
  assert.equal(weekStart(local(2026, 11, 1, 23, 0)), local(2026, 10, 26)) // Sunday closes the week
  assert.equal(weekStart(local(2026, 11, 2, 0, 30)), local(2026, 11, 2))
  assert.equal(addDays(local(2026, 10, 26), 14), local(2026, 11, 9))
  assert.equal(calendarDays(FRIDAY), 10)
  assert.equal(calendarDays(local(2026, 11, 1, 23, 0)), 8)
  assert.equal(calendarDays(local(2026, 11, 2, 0, 30)), 14)
})

test('the calendar payload and its coverage are normalized defensively', () => {
  const raw = {
    events: [
      { id: 'a', title: 'Employment Situation', title_es: 'Informe de empleo', currency: 'usd', time: 1793971800, impact: 'high', source: 'bls' },
      'junk',
    ],
    coverage: {
      weekly_until: 1793505600,
      weekly_stale: false,
      verified: '2026-10-31',
      sources: [
        { id: 'BLS', label: 'BLS (IPC, empleo)', live: true, ok: false, updated_at: 1793462400, until: 1798642800000, error: 'HTTP 403' },
        { id: '', label: 'sin id' },
        null,
        { id: 'ecb', live: false, ok: true },
      ],
    },
  }
  const { events, coverage } = normalizeCalendar(raw)
  assert.equal(events.length, 1)
  assert.equal(events[0].source, 'bls')
  assert.equal(events[0].currency, 'USD')
  assert.ok(coverage)
  assert.equal(coverage.weekly_until, 1793505600)
  assert.equal(coverage.verified, '2026-10-31')
  assert.deepEqual(coverage.sources.map((s) => s.id), ['bls', 'ecb'])
  assert.equal(coverage.sources[0].updated_at, 1793462400000) // s -> ms
  assert.equal(coverage.sources[0].until, 1798642800) // ms -> s
  assert.equal(coverage.sources[0].error, 'HTTP 403')
  assert.equal(coverage.sources[1].label, 'ecb')
  assert.equal(coverage.sources[1].updated_at, null)
  assert.equal(normalizeCalendar({ events: [] }).coverage, null) // an older backend without coverage
  assert.equal(normalizeCoverage('x'), null)
  assert.deepEqual(normalizeCoverage({}), { weekly_until: null, weekly_stale: false, verified: null, sources: [] })
  assert.equal(fmtIsoDay('2026-10-31'), '31/10/2026')
  assert.equal(fmtIsoDay('ayer'), 'ayer')
})

test('the view splits this week and next week and tags the official dates', () => {
  const events = [
    ev({ id: 'ff-eci', time: sec(local(2026, 10, 30, 8, 30)), title_es: 'Índice de costo laboral de EE. UU.', impact: 'medium' }),
    ev({ id: 'ff-daly', time: sec(local(2026, 10, 30, 16, 0)), title_es: 'Discurso de Daly', impact: 'medium' }),
    ev({ id: 'boe', time: sec(local(2026, 11, 5, 8, 0)), currency: 'GBP', title_es: 'Decisión de tipos del Banco de Inglaterra', source: 'boe' }),
    ev({ id: 'nfp', time: sec(local(2026, 11, 6, 8, 30)), title_es: 'Informe de empleo de EE. UU. (nóminas NFP y desempleo)', source: 'bls' }),
    ev({ id: 'boj', time: sec(local(2026, 11, 8, 23, 0)), currency: 'JPY', title_es: 'Decisión de tipos del Banco de Japón (hora aproximada)', source: 'boj' }),
    ev({ id: 'cpi', time: sec(local(2026, 11, 10, 8, 30)), title_es: 'IPC de EE. UU. (inflación)', source: 'bls' }), // later
  ]
  const cov = normalizeCoverage({ weekly_until: sec(local(2026, 11, 1)), weekly_stale: false, verified: '2026-10-31', sources: [] })
  const view = buildCalendarView([...events].reverse(), cov, FRIDAY)
  const [thisWeek, nextWeek] = view.sections
  assert.equal(thisWeek.title, 'Esta semana')
  assert.equal(nextWeek.title, `Próxima semana · ${fmtShortDay(local(2026, 11, 2))} – ${fmtShortDay(local(2026, 11, 8))}`)
  assert.deepEqual(thisWeek.days.map((d) => d.rows.map((r) => r.id)), [['ff-eci', 'ff-daly']])
  assert.deepEqual(nextWeek.days.map((d) => d.rows.map((r) => r.id)), [['boe'], ['nfp'], ['boj']])
  const eci = thisWeek.days[0].rows[0]
  assert.equal(eci.past, true)
  assert.equal(eci.tag, null) // the weekly feed needs no tag
  assert.equal(eci.time, fmtHM(local(2026, 10, 30, 8, 30)))
  assert.ok(thisWeek.days[0].label.startsWith('Hoy'))
  const nfp = nextWeek.days[1].rows[0]
  assert.equal(nfp.tag, 'BLS')
  assert.equal(nfp.past, false)
  assert.ok(nfp.tagTitle.includes('BLS') && nfp.tagTitle.includes('puede cambiar'))
  const boj = nextWeek.days[2].rows[0]
  assert.equal(boj.time, `≈${fmtHM(local(2026, 11, 8, 23, 0))}`)
  assert.equal(boj.tag, 'BoJ')
  assert.equal(view.truncated, 0)
  assert.equal(
    view.notes[0],
    `Datos semanales (ForexFactory, con previsiones) hasta el ${fmtShortDay(local(2026, 10, 31, 23, 59))}; ` +
      `después, solo las fechas de los calendarios oficiales (${OFFICIAL}), sin previsiones.`,
  )
  assert.ok(view.notes[view.notes.length - 1].startsWith('Las etiquetas marcan fechas de calendarios oficiales'))
})

test('a stale feed, a failing official source and the row limit are said plainly', () => {
  const cov = normalizeCoverage({
    weekly_until: null,
    weekly_stale: true,
    verified: '2026-10-31',
    sources: [
      { id: 'bls', label: 'BLS (IPC, empleo, IPP, JOLTS)', live: true, ok: false, error: 'HTTP 403' },
      { id: 'ecb', label: 'BCE (decisiones de tipos)', live: false, ok: true, error: null },
    ],
  })
  const empty = buildCalendarView([], cov, FRIDAY)
  assert.deepEqual(empty.sections.map((s) => s.days.length), [0, 0])
  assert.equal(empty.sections[0].empty, 'Sin más eventos de impacto alto o medio esta semana.')
  assert.equal(empty.sections[1].empty, 'Sin eventos de impacto alto o medio la próxima semana.')
  assert.deepEqual(empty.notes, [
    `Calendario semanal (ForexFactory) sin actualizar: solo se muestran las fechas oficiales (${OFFICIAL}), sin previsiones.`,
    'BLS (IPC, empleo, IPP, JOLTS): el calendario oficial no respondió (HTTP 403); se usan las fechas guardadas o las verificadas el 31/10/2026.',
  ])
  const many = Array.from({ length: 30 }, (_, i) => ev({ id: `m${i}`, time: sec(local(2026, 11, 2, 9, i)) }))
  const cut = buildCalendarView(many, null, FRIDAY, 25)
  assert.equal(cut.truncated, 5)
  assert.equal(cut.sections[1].days[0].rows.length, 25)
  assert.deepEqual(cut.notes, []) // no coverage and no official rows: nothing to explain
})

test('sources, official notes and the pill names of official releases', () => {
  assert.equal(sourceTag('bls'), 'BLS')
  assert.equal(sourceTag('ECB'), 'BCE')
  assert.equal(sourceTag('forexfactory'), 'FF')
  assert.equal(sourceTag('nuevo'), 'NUEVO')
  assert.equal(sourceTag(''), '—')
  assert.ok(sourceLabel('boj').includes('sin hora fija'))
  assert.equal(sourceLabel(''), 'Fuente desconocida')
  assert.equal(isOfficialSource('forexfactory'), false)
  assert.equal(isOfficialSource(''), false)
  assert.equal(isOfficialSource('federalreserve'), true)
  const nfp = ev({ time: 1, title: 'Employment Situation', title_es: 'Informe de empleo de EE. UU. (nóminas NFP y desempleo)', source: 'bls' })
  assert.equal(officialNote(nfp), 'Fecha oficial · BLS')
  assert.equal(officialNote(ev({ time: 1 })), '')
  const boj = ev({ time: 1, currency: 'JPY', title: 'BoJ monetary policy decision', title_es: 'Decisión de tipos del Banco de Japón (hora aproximada)', source: 'boj' })
  assert.equal(isApproxTime(boj), true)
  assert.equal(isApproxTime(nfp), false)
  assert.equal(officialNote(boj), 'Fecha oficial · BoJ · hora aproximada')
  const names: [MacroEvent, string][] = [
    [nfp, 'NFP 🇺🇸'],
    [ev({ time: 1, title: 'Consumer Price Index', title_es: 'IPC de EE. UU. (inflación)', source: 'bls' }), 'IPC 🇺🇸'],
    [ev({ time: 1, title: 'Personal Income and Outlays, October 2026', title_es: 'Ingresos y gasto personal de EE. UU. (inflación PCE)', source: 'bea' }), 'PCE 🇺🇸'],
    [ev({ time: 1, title: 'GDP (Advance Estimate), 4th Quarter and Year 2026', title_es: 'PIB adelantado de EE. UU. (trimestral)', source: 'bea' }), 'PIB 🇺🇸'],
    [ev({ time: 1, currency: 'EUR', title: 'ECB monetary policy decisions', title_es: 'Decisión de tipos del BCE', source: 'ecb' }), 'BCE'],
    [ev({ time: 1, currency: 'GBP', title: 'BoE Bank Rate decision', title_es: 'Decisión de tipos del Banco de Inglaterra', source: 'boe' }), 'BoE'],
    [boj, 'BoJ'],
  ]
  for (const [e, name] of names) assert.equal(eventShortName(e), name, e.title)
})

test('a failed refresh of the weekly feed is not reported as an official calendar', () => {
  const cov = normalizeCoverage({
    weekly_until: sec(local(2026, 11, 1)),
    weekly_stale: false,
    verified: '2026-10-31',
    sources: [
      { id: 'forexfactory', label: 'ForexFactory (semana actual, todas las divisas)', live: true, ok: true, error: 'el calendario económico respondió HTTP 429' },
      { id: 'federalreserve', label: 'Fed (reuniones del FOMC)', live: false, ok: true, error: null },
      { id: 'bea', label: 'BEA (PIB e inflación PCE)', live: true, ok: false, error: 'HTTP 503' },
    ],
  })
  const notes = buildCalendarView([], cov, FRIDAY).notes
  assert.ok(notes.every((n) => !n.startsWith('ForexFactory')), notes.join('\n'))
  assert.deepEqual(
    notes.filter((n) => n.includes('el calendario oficial no respondió')),
    ['BEA (PIB e inflación PCE): el calendario oficial no respondió (HTTP 503); se usan las fechas guardadas o las verificadas el 31/10/2026.'],
  )
})

test('the header pill counts down to an official release of next week', () => {
  const nfpTime = sec(local(2026, 11, 6, 8, 30))
  const nfp = ev({ id: 'bls-us_nfp', time: nfpTime, title: 'Employment Situation', title_es: 'Informe de empleo de EE. UU. (nóminas NFP y desempleo)', source: 'bls' })
  const cond: MarketConditions = {
    level: 'green',
    label: 'Operar con normalidad',
    headline: '',
    advice: '',
    reasons: [],
    scope: 'global',
    symbol: null,
    blackout: { active: false, until: null, event: null },
    next_event: nfp,
    events: [nfp],
    metrics: [],
    updated_at: FRIDAY,
    stale: false,
  }
  assert.equal(pulseCountdown(cond, FRIDAY), `NFP 🇺🇸 · ${Math.round((nfpTime - FRIDAY / 1000) / 86400)} d`)
})
