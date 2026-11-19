// Modo simple (only the demo accounts) or Pro (every section). Per viewer: kept in this browser (localStorage, in
// try/catch); the simple mode is the default.
import { useCallback, useState } from 'react'

export type UiMode = 'simple' | 'pro'
export const UI_MODE_KEY = 'nexus.ui.mode'

export function loadUiMode(): UiMode {
  try {
    return localStorage.getItem(UI_MODE_KEY) === 'pro' ? 'pro' : 'simple'
  } catch {
    return 'simple'
  }
}

export function useUiMode(): [UiMode, (m: UiMode) => void] {
  const [mode, setState] = useState<UiMode>(loadUiMode)
  const set = useCallback((m: UiMode) => {
    setState(m)
    try {
      localStorage.setItem(UI_MODE_KEY, m)
    } catch {
      /* private window: the mode lasts for this visit */
    }
  }, [])
  return [mode, set]
}
