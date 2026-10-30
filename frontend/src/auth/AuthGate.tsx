// Shows the login screen in cloud mode until there is a valid Supabase session. Local mode renders the app directly.
import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { LockKeyhole, TriangleAlert } from 'lucide-react'
import { API_BASE } from '../lib/config'
import { Button, Spinner } from '../components/ui'
import { currentSession, loadAuthConfig, onAuthChange, signIn } from './session'

type GateState = 'loading' | 'ready' | 'login' | 'error'

export default function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<GateState>('loading')
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let alive = true
    let off: (() => void) | null = null
    setState('loading')
    loadAuthConfig()
      .then((cfg) => {
        if (!alive) return
        if (cfg.mode !== 'supabase') {
          setState('ready')
          return
        }
        setState(currentSession() ? 'ready' : 'login')
        off = onAuthChange((s) => setState(s ? 'ready' : 'login'))
      })
      .catch((err: Error) => {
        if (!alive) return
        setError(err.message)
        setState('error')
      })
    return () => {
      alive = false
      off?.()
    }
  }, [attempt])

  if (state === 'ready') return <>{children}</>
  if (state === 'loading') {
    return (
      <div className="auth-screen">
        <Spinner large />
        <span className="muted">Conectando con NEXUS…</span>
      </div>
    )
  }
  if (state === 'error') {
    return (
      <div className="auth-screen">
        <div className="auth-card">
          <Brand />
          <div className="errorbox">
            <TriangleAlert size={16} />
            <div className="grow">
              {error}
              <div className="tiny" style={{ marginTop: 4 }}>
                Servidor: <span className="mono">{API_BASE || location.origin}</span>
              </div>
            </div>
          </div>
          <Button variant="primary" onClick={() => setAttempt((n) => n + 1)}>
            Reintentar
          </Button>
        </div>
      </div>
    )
  }
  return <LoginScreen />
}

function Brand() {
  return (
    <div className="brand" style={{ justifyContent: 'center', fontSize: 16 }}>
      <span className="brand-mark">N</span>
      NEXUS <small>TERMINAL</small>
    </div>
  )
}

function LoginScreen() {
  const [email, setEmail] = useState(() => {
    try {
      return localStorage.getItem('nexus.auth.email') ?? ''
    } catch {
      return ''
    }
  })
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!email || !password) return
    setBusy(true)
    setError(null)
    try {
      await signIn(email, password)
      try {
        localStorage.setItem('nexus.auth.email', email.trim())
      } catch {
        /* ignore */
      }
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth-screen">
      <form className="auth-card" onSubmit={submit}>
        <Brand />
        <div className="row gap-2 center muted small">
          <LockKeyhole size={14} /> Acceso privado
        </div>
        <div className="field">
          <label htmlFor="nexus-email">Correo</label>
          <input
            id="nexus-email"
            className="input"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus={!email}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="nexus-password">Contraseña</label>
          <input
            id="nexus-password"
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus={!!email}
            required
          />
        </div>
        {error && (
          <div className="errorbox">
            <TriangleAlert size={16} />
            <div className="grow">{error}</div>
          </div>
        )}
        <Button type="submit" variant="primary" size="lg" loading={busy} disabled={!email || !password}>
          Entrar
        </Button>
        <p className="tiny muted" style={{ margin: 0, textAlign: 'center' }}>
          Las cuentas se gestionan en Supabase (Authentication → Users).
        </p>
      </form>
    </div>
  )
}
