// Minimal stand-in for React's hooks, swapped in for 'react' by tests/int-fe-b.run.mjs in *.market-data.test.ts only.
// It runs ONE custom hook outside a browser: renderHook() renders it; a state setter only stores the new value (call
// rerender() to read the hook's new result); effects run after each render when their deps changed (cleanup first)
// and unmount() runs the cleanups. Enough for useMarketData (useState, useRef, useCallback, useEffect) and the
// modules it imports (lib/chart only imports hook names).
/* eslint-disable @typescript-eslint/no-explicit-any */
type Deps = readonly unknown[] | undefined

interface EffectSlot {
  deps: Deps
  cleanup: (() => void) | void
}

interface Instance {
  slots: any[]
  effects: EffectSlot[]
  queue: (() => void)[]
}

let inst: Instance | null = null
let cursor = 0
let effectCursor = 0

const depsChanged = (prev: Deps, next: Deps) =>
  !prev || !next || prev.length !== next.length || prev.some((v, i) => !Object.is(v, next[i]))

function active(): Instance {
  if (!inst) throw new Error('react-shim: hook called outside renderHook()')
  return inst
}

export function useRef<T>(initial: T): { current: T } {
  const it = active()
  const i = cursor++
  if (!(i in it.slots)) it.slots[i] = { current: initial }
  return it.slots[i]
}

export function useState<T>(initial: T | (() => T)): [T, (next: T | ((prev: T) => T)) => void] {
  const it = active()
  const i = cursor++
  if (!(i in it.slots)) {
    const cell = {
      value: typeof initial === 'function' ? (initial as () => T)() : initial,
      set: (next: T | ((prev: T) => T)) => {
        cell.value = typeof next === 'function' ? (next as (prev: T) => T)(cell.value) : next
      },
    }
    it.slots[i] = cell
  }
  return [it.slots[i].value, it.slots[i].set]
}

export function useMemo<T>(factory: () => T, deps: Deps): T {
  const it = active()
  const i = cursor++
  const prev = it.slots[i] as { deps: Deps; value: T } | undefined
  if (!prev || depsChanged(prev.deps, deps)) it.slots[i] = { deps, value: factory() }
  return it.slots[i].value
}

export function useCallback<T>(fn: T, deps: Deps): T {
  return useMemo(() => fn, deps)
}

export function useEffect(effect: () => void | (() => void), deps?: Deps): void {
  const it = active()
  const i = effectCursor++
  const prev = it.effects[i]
  if (prev && !depsChanged(prev.deps, deps)) return
  it.queue.push(() => {
    if (prev?.cleanup) prev.cleanup()
    it.effects[i] = { deps, cleanup: effect() }
  })
}

export function renderHook<R>(hook: () => R): { result: { current: R }; rerender: () => void; unmount: () => void } {
  const it: Instance = { slots: [], effects: [], queue: [] }
  const result = { current: undefined as unknown as R }
  const render = () => {
    inst = it
    cursor = 0
    effectCursor = 0
    try {
      result.current = hook()
    } finally {
      inst = null
    }
    for (const run of it.queue.splice(0)) run()
  }
  render()
  return {
    result,
    rerender: render,
    unmount: () => {
      for (const e of it.effects) if (e?.cleanup) e.cleanup()
      it.effects = []
    },
  }
}

export default { useCallback, useEffect, useMemo, useRef, useState }
