// Left column of PyChart Studio: scripts grouped into "Mis scripts" / "Ejemplos", search, new / duplicate /
// delete actions and an unsaved-changes dot per script.
import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { ChartLine, Copy, FileCode, FlaskConical, Lock, Plus, RefreshCw, Search, Trash2 } from 'lucide-react'
import type { ScriptMeta } from '../../api/types'
import { ErrorBox, IconButton, Spinner } from '../ui'
import type { Draft } from './studioStore'
import { keyOf } from './studioStore'
import type { TemplateKind } from './templates'

interface Item {
  key: string
  name: string
  description: string
  isExample: boolean
  isNew: boolean
  dirty: boolean
}

interface Props {
  list: ScriptMeta[] | null
  drafts: Record<string, Draft>
  active: string | null
  loading: boolean
  error: unknown
  canDelete: boolean
  canDuplicate: boolean
  onOpen: (key: string) => void
  onNew: (kind: TemplateKind) => void
  onDuplicate: () => void
  onDelete: () => void
  onRetry: () => void
}

const fold = (s: string) =>
  s
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()

const byName = (a: Item, b: Item) => a.name.localeCompare(b.name, 'es', { sensitivity: 'base' })

function ScriptList(props: Props) {
  const { list, drafts, active } = props
  const [query, setQuery] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && e.target instanceof Node && !menuRef.current.contains(e.target)) setMenuOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const { mine, examples } = useMemo(() => {
    const items: Item[] = []
    for (const [key, d] of Object.entries(drafts)) {
      if (d.isNew) items.push({ key, name: d.name || 'Sin título', description: d.description, isExample: false, isNew: true, dirty: true })
    }
    for (const m of list ?? []) {
      const key = keyOf(m.id)
      const d = drafts[key]
      items.push({
        key,
        name: d?.name || m.name,
        description: m.description ?? '',
        isExample: !!m.is_example,
        isNew: false,
        dirty: !!d,
      })
    }
    const q = fold(query.trim())
    const filtered = q ? items.filter((i) => fold(`${i.name} ${i.description}`).includes(q)) : items
    return {
      mine: filtered.filter((i) => !i.isExample).sort((a, b) => Number(b.isNew) - Number(a.isNew) || byName(a, b)),
      examples: filtered.filter((i) => i.isExample).sort(byName),
    }
  }, [list, drafts, query])

  const renderItem = (it: Item) => (
    <button
      key={it.key}
      type="button"
      className={`studio-item ${it.key === active ? 'active' : ''} ${it.isNew ? 'is-new' : ''}`}
      onClick={() => props.onOpen(it.key)}
      title={it.description ? `${it.name}\n${it.description}` : it.name}
    >
      {it.isExample ? <Lock size={12} className="studio-item-icon" /> : <FileCode size={12} className="studio-item-icon" />}
      <span className="ellipsis grow">{it.name}</span>
      {it.isNew && <span className="tiny muted">nuevo</span>}
      {it.dirty && <span className="studio-dot" title="Cambios sin guardar" />}
    </button>
  )

  const empty = list !== null && !mine.length && !examples.length

  return (
    <aside className="studio-side">
      <div className="studio-side-head">
        <strong className="grow">Scripts</strong>
        {props.loading && <Spinner />}
        <div className="studio-menu-anchor" ref={menuRef}>
          <IconButton size="sm" icon={<Plus size={15} />} title="Nuevo script" onClick={() => setMenuOpen((o) => !o)} active={menuOpen} />
          {menuOpen && (
            <div className="studio-menu" role="menu">
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false)
                  props.onNew('indicator')
                }}
              >
                <ChartLine size={14} /> Nuevo indicador
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setMenuOpen(false)
                  props.onNew('strategy')
                }}
              >
                <FlaskConical size={14} /> Nueva estrategia
              </button>
            </div>
          )}
        </div>
        <IconButton size="sm" icon={<Copy size={14} />} title="Duplicar script" onClick={props.onDuplicate} disabled={!props.canDuplicate} />
        <IconButton
          size="sm"
          icon={<Trash2 size={14} />}
          title={props.canDelete ? 'Eliminar script' : 'Los ejemplos no se pueden eliminar'}
          onClick={props.onDelete}
          disabled={!props.canDelete}
        />
      </div>
      <div className="studio-search">
        <Search size={13} />
        <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Buscar script…" aria-label="Buscar script" />
      </div>
      <div className="studio-list scroll">
        {props.error !== null && props.error !== undefined && (
          <div style={{ padding: 8 }}>
            <ErrorBox error={props.error} onRetry={props.onRetry} />
          </div>
        )}
        {list === null && !props.error && (
          <div className="studio-list-empty">
            <Spinner /> Cargando scripts…
          </div>
        )}
        <div className="studio-group">
          <div className="studio-group-title">
            Mis scripts <span className="muted">{mine.length}</span>
          </div>
          {mine.length ? (
            mine.map(renderItem)
          ) : (
            <div className="studio-list-empty tiny">{query ? 'Sin coincidencias' : 'Aún no tienes scripts. Crea uno con “+” o guarda una copia de un ejemplo.'}</div>
          )}
        </div>
        {(examples.length > 0 || list === null) && (
          <div className="studio-group">
            <div className="studio-group-title">
              Ejemplos <span className="muted">{examples.length}</span>
            </div>
            {examples.map(renderItem)}
          </div>
        )}
        {empty && query && <div className="studio-list-empty tiny">Ningún script coincide con “{query}”.</div>}
      </div>
      <div className="studio-side-foot">
        <IconButton size="sm" icon={<RefreshCw size={13} />} title="Recargar lista" onClick={props.onRetry} />
        <span className="tiny muted grow">Los cambios sin guardar se conservan en este navegador.</span>
      </div>
    </aside>
  )
}

export default memo(ScriptList)
