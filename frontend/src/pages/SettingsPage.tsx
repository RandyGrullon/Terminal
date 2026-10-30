// "Ajustes": every NEXUS setting grouped with Spanish labels and help, dirty tracking, per-field revert, saving only
// the changed keys, plus system status, local database and about.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, RotateCcw, Save, Settings as SettingsIcon, TriangleAlert } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { Health, Settings } from '../api/types'
import { useApp } from '../store'
import { Button, Card, ErrorBox, Loading, Modal } from '../components/ui'
import AboutCard from '../components/settings/AboutCard'
import DbCard from '../components/settings/DbCard'
import GateZones from '../components/settings/GateZones'
import SettingField from '../components/settings/SettingField'
import SystemCard from '../components/settings/SystemCard'
import { FALLBACK_MARKETS, FIELD_BY_KEY, GROUPS, type FieldDef } from '../components/settings/schema'
import { crossValidate, keyInMessage, sameValue, validateField } from '../components/settings/validate'
import '../components/settings/settings.css'

const FIELDS: FieldDef[] = GROUPS.flatMap((g) => g.fields)

/** Unsaved edits survive switching pages (the page component is unmounted by the shell). */
let pendingEdits: Settings | null = null

function outgoing(def: FieldDef | undefined, v: unknown): unknown {
  if (!def) return v
  switch (def.kind) {
    case 'secret':
      return typeof v === 'string' ? v.trim() : ''
    case 'symbols':
      return Array.isArray(v) ? Array.from(new Set(v.map((s) => String(s).trim().toUpperCase()).filter(Boolean))) : []
    default:
      return v
  }
}

export default function SettingsPage() {
  const notify = useApp((s) => s.notify)
  const [loaded, setLoaded] = useState<Settings | null>(null)
  const [values, setValues] = useState<Settings>({})
  const [loadError, setLoadError] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({})
  const [markets, setMarkets] = useState<{ id: string; name: string }[]>(FALLBACK_MARKETS)
  const [version, setVersion] = useState<string | null>(null)
  const [confirmReload, setConfirmReload] = useState(false)

  const load = useCallback(async (restorePending: boolean) => {
    setLoading(true)
    try {
      const r = await api.settings.get()
      const s = r?.settings
      if (!s || typeof s !== 'object') throw new Error('Respuesta de ajustes no válida')
      setLoaded(s)
      let next: Settings = s
      if (restorePending && pendingEdits) {
        const restored = Object.entries(pendingEdits).filter(([k, v]) => k in FIELD_BY_KEY && !sameValue(v, s[k]))
        if (restored.length) {
          next = { ...s, ...Object.fromEntries(restored) }
          notify(`Se recuperaron ${restored.length} cambio(s) sin guardar`, 'info')
        }
      }
      pendingEdits = null
      setValues(next)
      setServerErrors({})
      setSaveError(null)
      setLoadError(null)
    } catch (e) {
      setLoadError(e)
    } finally {
      setLoading(false)
    }
  }, [notify])

  useEffect(() => {
    void load(true)
    let alive = true
    api
      .markets()
      .then((r) => {
        const list = Array.isArray(r?.markets) ? r.markets.filter((m) => m && typeof m.id === 'string') : []
        if (alive && list.length) setMarkets(list.map((m) => ({ id: m.id, name: m.name || m.id })))
      })
      .catch(() => {
        /* fallback list stays */
      })
    return () => {
      alive = false
    }
  }, [load])

  const dirtyKeys = useMemo(() => (loaded ? FIELDS.filter((f) => !sameValue(values[f.key], loaded[f.key])).map((f) => f.key) : []), [values, loaded])
  const dirtySet = useMemo(() => new Set(dirtyKeys), [dirtyKeys])

  const clientErrors = useMemo(() => {
    const out: Record<string, string> = {}
    for (const f of FIELDS) {
      if (!dirtySet.has(f.key)) continue
      const e = validateField(f, values[f.key])
      if (e) out[f.key] = e
    }
    const cross = crossValidate(values)
    for (const [k, msg] of Object.entries(cross)) {
      if (dirtySet.has(k) || dirtySet.has('gate.take_score') || dirtySet.has('gate.wait_score')) out[k] = msg
    }
    return out
  }, [values, dirtySet])

  const blocking = Object.keys(clientErrors)

  // Keep unsaved edits when the page unmounts; warn before closing the tab.
  const stateRef = useRef({ values, dirtyKeys })
  stateRef.current = { values, dirtyKeys }
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (!stateRef.current.dirtyKeys.length) return
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload)
      const { values: v, dirtyKeys: d } = stateRef.current
      pendingEdits = d.length ? Object.fromEntries(d.map((k) => [k, v[k]])) : null
    }
  }, [])

  const onChange = useCallback((key: string, value: unknown) => {
    setValues((v) => ({ ...v, [key]: value }))
    setServerErrors((e) => {
      if (!(key in e)) return e
      const n = { ...e }
      delete n[key]
      return n
    })
    setSaveError(null)
  }, [])

  const loadedRef = useRef(loaded)
  loadedRef.current = loaded
  const onRevert = useCallback((key: string) => {
    const base = loadedRef.current
    if (!base) return
    setValues((v) => ({ ...v, [key]: base[key] }))
    setServerErrors((e) => {
      if (!(key in e)) return e
      const n = { ...e }
      delete n[key]
      return n
    })
  }, [])

  const discard = () => {
    if (!loaded) return
    setValues(loaded)
    setServerErrors({})
    setSaveError(null)
  }

  const scrollToField = (key: string) => {
    document.getElementById(`set-field-${key}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const save = async () => {
    if (!loaded || !dirtyKeys.length || saving) return
    if (blocking.length) {
      notify('Corrige los campos marcados en rojo antes de guardar', 'warn')
      scrollToField(blocking[0])
      return
    }
    const payload: Settings = {}
    for (const k of dirtyKeys) payload[k] = outgoing(FIELD_BY_KEY[k], values[k])
    setSaving(true)
    setSaveError(null)
    try {
      const r = await api.settings.update(payload)
      const s = r?.settings && typeof r.settings === 'object' ? r.settings : { ...loaded, ...payload }
      setLoaded(s)
      setValues(s)
      setServerErrors({})
      notify(`Ajustes guardados (${dirtyKeys.length})`, 'success')
    } catch (e) {
      const msg = errorMessage(e)
      const key = keyInMessage(msg, dirtyKeys)
      if (key) {
        setServerErrors({ [key]: msg })
        scrollToField(key)
      }
      setSaveError(msg)
    } finally {
      setSaving(false)
    }
  }

  const saveRef = useRef(save)
  saveRef.current = save
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 's') {
        if (document.querySelector('.modal-backdrop')) return
        e.preventDefault()
        void saveRef.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const onHealth = useCallback((h: Health | null) => setVersion(h?.version ?? null), [])

  const header = (
    <div className="page-header">
      <SettingsIcon size={20} className="accent" />
      <div className="col gap-0">
        <h1>Ajustes</h1>
        <span className="sub">Se guardan en tu base de datos local y se aplican al momento en todos los módulos.</span>
      </div>
      <div className="grow" />
      <Button
        size="sm"
        icon={<RefreshCw size={14} />}
        loading={loading}
        onClick={() => (dirtyKeys.length ? setConfirmReload(true) : void load(false))}
        title="Volver a leer los ajustes del servidor"
      >
        Recargar
      </Button>
    </div>
  )

  const aside = (
    <div className="col gap-3 set-aside">
      <SystemCard onHealth={onHealth} />
      <DbCard />
      <AboutCard version={version} />
    </div>
  )

  if (!loaded) {
    return (
      <div className="page set-page">
        {header}
        <div className="set-layout">
          <div className="col gap-3">
            {loadError ? <ErrorBox error={loadError} onRetry={() => void load(true)} /> : <Loading label="Cargando ajustes…" />}
          </div>
          {aside}
        </div>
      </div>
    )
  }

  const errorsFor = (key: string) => serverErrors[key] ?? clientErrors[key] ?? null

  return (
    <div className="page set-page">
      {header}

      <div className={`set-savebar ${dirtyKeys.length ? 'dirty' : ''}`}>
        <div className="set-jump">
          {GROUPS.map((g) => {
            const n = g.fields.filter((f) => dirtySet.has(f.key)).length
            const bad = g.fields.some((f) => errorsFor(f.key) && dirtySet.has(f.key))
            return (
              <button
                key={g.id}
                type="button"
                className={`set-jump-pill ${n ? 'dirty' : ''} ${bad ? 'bad' : ''}`}
                onClick={() => document.getElementById(`set-group-${g.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
              >
                <g.icon size={12} />
                {g.title.replace(/«|»/g, '')}
                {n > 0 && <span className="set-jump-count">{n}</span>}
              </button>
            )
          })}
        </div>
        <div className="set-savebar-actions">
          {blocking.length > 0 ? (
            <span className="small neg row gap-1">
              <TriangleAlert size={13} /> {blocking.length} campo(s) con error
            </span>
          ) : (
            <span className={`small ${dirtyKeys.length ? 'warn' : 'muted'}`}>
              {dirtyKeys.length ? `${dirtyKeys.length} cambio(s) sin guardar` : 'Todo guardado'}
            </span>
          )}
          <Button size="sm" variant="ghost" icon={<RotateCcw size={13} />} onClick={discard} disabled={!dirtyKeys.length || saving}>
            Descartar
          </Button>
          <Button
            size="sm"
            variant="primary"
            icon={<Save size={14} />}
            onClick={() => void save()}
            loading={saving}
            disabled={!dirtyKeys.length || blocking.length > 0}
            title="Guardar cambios (Ctrl+S)"
          >
            Guardar cambios
          </Button>
        </div>
      </div>

      {saveError && <ErrorBox error={`No se guardó ningún cambio: ${saveError}`} />}

      <div className="set-layout">
        <div className="col gap-3">
          {GROUPS.map((g) => (
            <div key={g.id} id={`set-group-${g.id}`} className="set-group-anchor">
              <Card
                title={
                  <>
                    <g.icon size={15} className={g.id === 'gate' ? 'pos' : 'accent'} /> {g.title}
                  </>
                }
                sub={g.description}
                className={g.id === 'gate' ? 'set-card-gate' : undefined}
              >
                {g.id === 'gate' && (
                  <GateZones take={values['gate.take_score']} wait={values['gate.wait_score']} enabled={values['gate.enabled'] !== false} />
                )}
                <div className="set-fields">
                  {g.fields.map((f) => (
                    <SettingField
                      key={f.key}
                      def={f}
                      value={values[f.key]}
                      dirty={dirtySet.has(f.key)}
                      error={errorsFor(f.key)}
                      markets={markets}
                      onChange={onChange}
                      onRevert={onRevert}
                    />
                  ))}
                </div>
              </Card>
            </div>
          ))}
        </div>
        {aside}
      </div>

      <Modal
        open={confirmReload}
        onClose={() => setConfirmReload(false)}
        title="Recargar ajustes"
        width={440}
        footer={
          <>
            <Button onClick={() => setConfirmReload(false)}>Cancelar</Button>
            <Button
              variant="danger"
              onClick={() => {
                setConfirmReload(false)
                pendingEdits = null
                void load(false)
              }}
            >
              Descartar y recargar
            </Button>
          </>
        }
      >
        <p style={{ margin: 0 }}>Tienes {dirtyKeys.length} cambio(s) sin guardar. Si recargas, se perderán.</p>
      </Modal>
    </div>
  )
}
