// "Referencia" tab: searchable PyChart API documentation (from /api/scripts/reference). Clicking a name inserts
// it at the editor cursor.
import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { BookOpen, CornerDownLeft, Search } from 'lucide-react'
import type { ScriptReference } from '../../api/types'
import { Empty, ErrorBox, Loading } from '../ui'
import { cachedReference, loadReference } from './reference'

const fold = (s: string) =>
  s
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()

function ReferencePanel({ onInsert }: { onInsert: (text: string) => void }) {
  const [ref, setRef] = useState<ScriptReference | null>(() => cachedReference())
  const [error, setError] = useState<unknown>(null)
  const [query, setQuery] = useState('')

  const load = useCallback((force: boolean) => {
    setError(null)
    loadReference(force)
      .then((r) => setRef(r))
      .catch((e: unknown) => setError(e))
  }, [])

  useEffect(() => {
    if (!cachedReference()) load(false)
  }, [load])

  const sections = useMemo(() => {
    if (!ref) return []
    const q = fold(query.trim())
    if (!q) return ref.sections
    return ref.sections
      .map((s) => ({
        title: s.title,
        items: s.items.filter((i) => fold(`${i.name} ${i.signature ?? ''} ${i.doc ?? ''}`).includes(q) || fold(s.title).includes(q)),
      }))
      .filter((s) => s.items.length > 0)
  }, [ref, query])

  const total = useMemo(() => sections.reduce((n, s) => n + s.items.length, 0), [sections])

  return (
    <div className="studio-ref">
      <div className="studio-search" style={{ borderBottom: '1px solid var(--border)' }}>
        <Search size={13} />
        <input
          className="input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Buscar en la API: ta.ema, plotshape, strategy…"
          aria-label="Buscar en la referencia"
        />
        {ref && <span className="tiny muted nowrap">{total} elementos</span>}
      </div>
      <div className="studio-ref-body scroll">
        {error !== null ? (
          <div style={{ padding: 10 }}>
            <ErrorBox error={error} onRetry={() => load(true)} />
          </div>
        ) : !ref ? (
          <Loading label="Cargando referencia…" />
        ) : !sections.length ? (
          <Empty icon={<BookOpen size={24} />} title={query ? 'Sin resultados' : 'Referencia vacía'}>
            {query ? `Nada coincide con “${query}”.` : 'El servidor no devolvió documentación.'}
          </Empty>
        ) : (
          sections.map((s) => (
            <section key={s.title} className="studio-ref-section">
              <h4>{s.title}</h4>
              {s.items.map((it) => (
                <div key={`${s.title}-${it.name}`} className="studio-ref-item">
                  <div className="row gap-2">
                    <button type="button" className="studio-ref-name mono" onClick={() => onInsert(it.name)} title="Insertar en el editor">
                      {it.name}
                    </button>
                    <CornerDownLeft size={11} className="muted" />
                  </div>
                  {it.signature && <code className="studio-ref-sig">{it.signature}</code>}
                  {it.doc && <p className="studio-ref-doc">{it.doc}</p>}
                </div>
              ))}
            </section>
          ))
        )}
      </div>
    </div>
  )
}

export default memo(ReferencePanel)
