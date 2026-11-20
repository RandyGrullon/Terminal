import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import AuthGate from './auth/AuthGate'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AuthGate>
      <App />
    </AuthGate>
  </React.StrictMode>,
)

// PWA: installable on the phone (public/sw.js only keeps the last page for when there is no connection). Not in
// development, where Vite serves the files.
if (import.meta.env.PROD && 'serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => undefined)
  })
}

// Behave like a phone app: no pinch / double-tap zoom (iOS ignores user-scalable=no without this), no page bounce.
document.addEventListener('gesturestart', (e) => e.preventDefault(), { passive: false })
document.addEventListener(
  'touchmove',
  (e) => {
    if ((e as TouchEvent).touches.length > 1) e.preventDefault()
  },
  { passive: false },
)
