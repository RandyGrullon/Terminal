// Small numeric helpers (quantiles, "nice" axis steps, histogram binning) for the Simulation page.

export function finiteValues(xs: readonly (number | null | undefined)[] | null | undefined): number[] {
  const out: number[] = []
  if (!xs) return out
  for (const x of xs) if (typeof x === 'number' && Number.isFinite(x)) out.push(x)
  return out
}

/** Linear-interpolated quantile of an ascending array (q in [0, 1]). */
export function quantileSorted(sorted: readonly number[], q: number): number | null {
  const n = sorted.length
  if (n === 0) return null
  if (n === 1) return sorted[0]
  const pos = Math.min(1, Math.max(0, q)) * (n - 1)
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}

export function mean(xs: readonly number[]): number | null {
  if (!xs.length) return null
  let s = 0
  for (const x of xs) s += x
  return s / xs.length
}

export function minMax(xs: readonly number[]): { min: number; max: number } | null {
  if (!xs.length) return null
  let min = Infinity
  let max = -Infinity
  for (const x of xs) {
    if (x < min) min = x
    if (x > max) max = x
  }
  return { min, max }
}

/** Rounds a raw step to 1, 2, 2.5 or 5 x 10^k. */
export function niceStep(raw: number): number {
  if (!(raw > 0) || !Number.isFinite(raw)) return 1
  const exp = Math.floor(Math.log10(raw))
  const base = 10 ** exp
  const f = raw / base
  const nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10
  return nf * base
}

/** Removes floating point noise (0.30000000000000004 -> 0.3). */
export const clean = (v: number): number => Number(v.toFixed(10))

/** Tick values that are multiples of a nice step inside [lo, hi]. */
export function niceTicks(lo: number, hi: number, maxTicks: number): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return Number.isFinite(lo) ? [clean(lo)] : []
  const step = niceStep((hi - lo) / Math.max(1, maxTicks))
  const out: number[] = []
  const first = Math.ceil(lo / step - 1e-9) * step
  for (let v = first; v <= hi + step * 1e-9 && out.length < 50; v += step) out.push(clean(v))
  return out
}

export interface Bins {
  /** Left edge of the first bin. */
  start: number
  step: number
  counts: number[]
  maxCount: number
  total: number
  min: number
  max: number
}

/**
 * Histogram bins aligned to multiples of a nice step (so 0 is always a bin edge, which keeps sign colouring exact).
 * `target` is the desired number of bins.
 */
export function buildBins(values: readonly number[], target: number): Bins | null {
  const vals = finiteValues(values)
  const mm = minMax(vals)
  if (!mm) return null
  let lo = mm.min
  let hi = mm.max
  if (hi - lo < 1e-12) {
    const pad = Math.max(Math.abs(lo) * 0.05, 0.5)
    lo -= pad
    hi += pad
  }
  const step = niceStep((hi - lo) / Math.max(1, target))
  const start = clean(Math.floor(lo / step) * step)
  const n = Math.min(400, Math.max(1, Math.ceil((hi - start) / step - 1e-9)))
  const counts = new Array<number>(n).fill(0)
  for (const v of vals) {
    let i = Math.floor((v - start) / step + 1e-9)
    if (i >= n) i = n - 1
    if (i < 0) i = 0
    counts[i] += 1
  }
  let maxCount = 0
  for (const c of counts) if (c > maxCount) maxCount = c
  return { start, step, counts, maxCount, total: vals.length, min: mm.min, max: mm.max }
}
