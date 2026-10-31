// Regression (daily-bars-shifted-a-day): 1d / 1w bars open at 00:00 UTC (weeks on Monday). Every chart shifted
// them to the local clock, so in a UTC-negative zone the 30/10 daily candle read "29 oct 20:00" on the axis and
// crosshair, and "Vela 29/10/26" in the panels. They must keep their UTC session date; intraday bars stay local.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { IChartApi } from 'lightweight-charts'
import type { ChartMarker } from '../src/api/types'
import { ascendingIndices, cleanBars, cleanCandles } from '../src/components/chart/chartUtils'
import {
  ConeLayer,
  TradeLevelsLayer,
  fromTs,
  isSessionTf,
  timeScaleFor,
  toCandleData,
  toHistogramData,
  toLineData,
  toMarkers,
  toVolumeData,
  ts,
} from '../src/lib/chart'
import { fmtBarTime } from '../src/lib/format'

process.env.TZ = 'America/La_Paz' // UTC-4 all year, like the user's machine

const DAY = Date.UTC(2026, 9, 30) / 1000 // Friday 30/10/2026 00:00 UTC (a 1d bar)
const NEXT_DAY = DAY + 86_400
const WEEK = Date.UTC(2026, 9, 26) / 1000 // Monday 26/10/2026 00:00 UTC (a 1w bar)
const H14 = Date.UTC(2026, 9, 30, 14) / 1000 // a 1h bar, 10:00 local

/** lightweight-charts labels UTCTimestamp chart times in UTC: this is what the axis / crosshair show. */
const label = (t: number) => new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')

function fakeChart(): { chart: IChartApi; data: { time: number }[][] } {
  const data: { time: number }[][] = []
  const series = () => ({ setData: (d: { time: number }[]) => data.push(d) })
  return { chart: { addSeries: series } as unknown as IChartApi, data }
}

test('1d / 1w bars keep their UTC session date on the chart', () => {
  assert.equal(label(ts(DAY, '1d')), '2026-10-30 00:00')
  assert.equal(label(ts(WEEK, '1w')), '2026-10-26 00:00')
  // what every chart drew before: the previous evening
  assert.equal(label(ts(DAY)), '2026-10-29 20:00')
  assert.ok(isSessionTf('1d') && isSessionTf('1w') && !isSessionTf('12h') && !isSessionTf(undefined))
})

test('intraday bars keep the local clock and fromTs inverts ts', () => {
  assert.equal(label(ts(H14, '1h')), '2026-10-30 10:00')
  assert.equal(fromTs(ts(H14, '1h'), '1h'), H14)
  assert.equal(fromTs(ts(DAY, '1d'), '1d'), DAY)
  assert.equal(fromTs(ts(WEEK, '1w'), '1w'), WEEK)
})

test('the time axis shows dates without a clock for 1d / 1w', () => {
  assert.equal(timeScaleFor('1d').timeVisible, false)
  assert.equal(timeScaleFor('1w').timeVisible, false)
  assert.equal(timeScaleFor('4h').timeVisible, true)
})

test('candles, volume, lines, histograms and markers use the timeframe', () => {
  const candle = { time: DAY, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10 }
  const marker: ChartMarker = { time: DAY, position: 'belowBar', shape: 'arrowUp', color: '#16c784', text: 'COMPRA', kind: 'BUY' }
  assert.equal(toCandleData([candle], '1d')[0].time, DAY)
  assert.equal(toVolumeData([candle], '1d')[0].time, DAY)
  assert.equal(toLineData([DAY], [1], '1d')[0].time, DAY)
  assert.equal(toHistogramData([DAY], [1], '1d')[0].time, DAY)
  assert.equal(toMarkers([marker], '1w')[0].time, DAY)
  assert.equal(toCandleData([{ ...candle, time: H14 }], '1h')[0].time, ts(H14))
})

test('prediction cone and SL/TP step lines use the timeframe', () => {
  const cone = fakeChart()
  new ConeLayer(cone.chart).set([{ time: NEXT_DAY, p10: 1, p25: 2, p50: 3, p75: 4, p90: 5 }], { time: DAY, price: 3 }, '1d')
  assert.equal(cone.data.length, 5)
  for (const d of cone.data) assert.deepEqual(d.map((p) => p.time), [DAY, NEXT_DAY])

  const levels = fakeChart()
  const empty: (number | null)[] = []
  new TradeLevelsLayer(levels.chart).set(
    {
      time: [DAY, NEXT_DAY],
      score: empty,
      prob_up: empty,
      action: [],
      regime: [],
      ema_fast: empty,
      ema_slow: empty,
      ema_trend: empty,
      pos_entry: [100, 100],
      pos_sl: [90, 90],
      pos_tp1: [110, 110],
      pos_tp2: [120, 120],
      pos_tp3: [130, 130],
    },
    '1d',
  )
  assert.equal(levels.data.length, 5)
  for (const d of levels.data) assert.deepEqual(d.map((p) => p.time), [DAY, NEXT_DAY])
})

test('bar labels: UTC session date for 1d / 1w, local clock for intraday', () => {
  assert.equal(fmtBarTime(DAY, '1d'), '30/10/26') // was 29/10/26 in UTC-4
  assert.equal(fmtBarTime(WEEK, '1w'), '26/10/26') // was 25/10/26 (Sunday)
  assert.equal(fmtBarTime(H14, '1h'), '30/10, 10:00')
  assert.equal(fmtBarTime(null, '1d'), '—')
})

test('ordering / cleaning helpers keep daily data intact in both time modes', () => {
  const times = [DAY, NEXT_DAY, NEXT_DAY + 86_400]
  assert.equal(ascendingIndices(times, '1d'), null)
  assert.deepEqual(ascendingIndices([DAY, DAY, NEXT_DAY], '1d'), [0, 2])
  const candles = times.map((t) => ({ time: t, open: 1, high: 1, low: 1, close: 1, volume: 1 }))
  assert.equal(cleanCandles(candles, '1d').length, 3)
  const bars = cleanBars(
    {
      time: times,
      score: [0, 0, 0],
      prob_up: [0.5, 0.5, 0.5],
      action: ['WAIT', 'WAIT', 'WAIT'],
      regime: ['range', 'range', 'range'],
      ema_fast: [1, 1, 1],
      ema_slow: [1, 1, 1],
      ema_trend: [1, 1, 1],
      pos_entry: [null, null, null],
      pos_sl: [null, null, null],
      pos_tp1: [null, null, null],
      pos_tp2: [null, null, null],
      pos_tp3: [null, null, null],
    },
    '1d',
  )
  assert.deepEqual(bars?.time, times)
})
