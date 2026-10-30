// "Indicadores" dialog: searchable catalog by category (click adds with default params, the same indicator can
// be added several times) and the "Activos" section to edit parameters or remove indicators.
import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChartLine, Plus, RotateCcw, Search, Settings2, Trash2, TriangleAlert, X } from 'lucide-react'
import type { IndicatorParam, IndicatorSpec } from '../../api/types'
import { Badge, Button, Empty, ErrorBox, Field, IconButton, Loading, Modal, Select, Spinner, Tabs, Toggle } from '../ui'
import { foldText, safeColor, type ParamMap, type ParamValue } from './chartUtils'
import {
  CATEGORY_ORDER,
  MAX_INDICATORS,
  SOURCE_LABEL,
  paramOptions,
  paramsSummary,
  type ResolvedIndicator,
} from './indicatorsModel'

const ALL = '__all__'

interface Props {
  open: boolean
  onClose: () => void
  catalog: IndicatorSpec[] | null
  catalogError: unknown
  onRetryCatalog: () => void
  items: ResolvedIndicator[]
  focusUid: string | null
  onAdd: (spec: IndicatorSpec) => void
  onUpdate: (uid: string, params: ParamMap) => void
  onReset: (uid: string) => void
  onRemove: (uid: string) => void
  onClear: () => void
}

// ----------------------------------------------------------------------------- parameter inputs

function NumberParam(props: { param: IndicatorParam; value: number; onCommit: (v: number) => void }) {
  const { param, value, onCommit } = props
  const [draft, setDraft] = useState(String(value))
  const focused = useRef(false)

  useEffect(() => {
    if (!focused.current) setDraft(String(value))
  }, [value])

  const parse = (raw: string): number | null => {
    if (raw.trim() === '') return null
    const n = Number(raw.replace(',', '.'))
    if (!Number.isFinite(n)) return null
    return param.type === 'int' ? Math.round(n) : n
  }
  const inBounds = (n: number) => (param.min === undefined || n >= param.min) && (param.max === undefined || n <= param.max)
  const clamp = (n: number) => Math.min(param.max ?? Infinity, Math.max(param.min ?? -Infinity, n))

  const bounds =
    param.min !== undefined || param.max !== undefined
      ? `Rango: ${param.min ?? '−∞'} a ${param.max ?? '∞'}${param.type === 'int' ? ' (entero)' : ''}`
      : param.type === 'int'
        ? 'Número entero'
        : undefined

  return (
    <input
      className="input mono"
      type="number"
      value={draft}
      min={param.min}
      max={param.max}
      step={param.step ?? (param.type === 'int' ? 1 : 'any')}
      title={bounds}
      onFocus={() => {
        focused.current = true
      }}
      onChange={(e) => {
        setDraft(e.target.value)
        const n = parse(e.target.value)
        if (n !== null && inBounds(n) && n !== value) onCommit(n)
      }}
      onBlur={() => {
        focused.current = false
        const n = parse(draft)
        if (n !== null) {
          const v = clamp(n)
          if (v !== value) onCommit(v)
          setDraft(String(v))
        } else {
          setDraft(String(value))
        }
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
      }}
    />
  )
}

function ParamsEditor(props: { item: ResolvedIndicator; onUpdate: (params: ParamMap) => void; onReset: () => void }) {
  const { item } = props
  const spec = item.spec
  if (!spec) return null
  if (spec.params.length === 0) return <div className="tiny muted">Este indicador no tiene parámetros.</div>
  const set = (name: string, v: ParamValue) => props.onUpdate({ [name]: v })
  return (
    <div className="col gap-2">
      <div className="chart-ind-params">
        {spec.params.map((p) => {
          const v = item.params[p.name]
          let input: ReactNode
          if (p.type === 'int' || p.type === 'float') {
            input = <NumberParam param={p} value={typeof v === 'number' ? v : Number(p.default) || 0} onCommit={(n) => set(p.name, n)} />
          } else if (p.type === 'bool') {
            input = <Toggle checked={v === true} onChange={(b) => set(p.name, b)} label={v === true ? 'Sí' : 'No'} />
          } else {
            const opts = paramOptions(p)
            const value = typeof v === 'string' ? v : String(p.default ?? '')
            const options = (opts.includes(value) || value === '' ? opts : [value, ...opts]).map((o) => ({
              value: o,
              label: p.type === 'source' ? (SOURCE_LABEL[o] ?? o) : o,
            }))
            input = <Select value={value} onChange={(o) => set(p.name, o)} options={options} />
          }
          return (
            <Field key={p.name} label={p.label || p.name}>
              {input}
            </Field>
          )
        })}
      </div>
      <div className="row end">
        <Button size="sm" variant="ghost" icon={<RotateCcw size={13} />} onClick={props.onReset}>
          Valores por defecto
        </Button>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------------------- dialog

function IndicatorsDialog(props: Props) {
  const { open, catalog, items, focusUid } = props
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<string>(ALL)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const itemRefs = useRef(new Map<string, HTMLDivElement>())
  const prevCount = useRef(items.length)

  // Every opening starts clean; from the legend gear, only that indicator is expanded and scrolled into view.
  useEffect(() => {
    if (!open) return
    setQuery('')
    setExpanded(focusUid ? new Set([focusUid]) : new Set())
    if (!focusUid) return
    const id = window.setTimeout(() => itemRefs.current.get(focusUid)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }), 60)
    return () => window.clearTimeout(id)
  }, [open, focusUid])

  // Expand only the indicator that was just added so its parameters can be tuned right away.
  useEffect(() => {
    if (open && items.length > prevCount.current) {
      const last = items[items.length - 1]
      if (last) {
        setExpanded(new Set([last.uid]))
        window.setTimeout(() => itemRefs.current.get(last.uid)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }), 60)
      }
    }
    prevCount.current = items.length
  }, [open, items])

  const categories = useMemo(() => {
    const counts = new Map<string, number>()
    for (const s of catalog ?? []) counts.set(s.category, (counts.get(s.category) ?? 0) + 1)
    const names = Array.from(counts.keys()).sort((a, b) => {
      const ia = CATEGORY_ORDER.indexOf(a)
      const ib = CATEGORY_ORDER.indexOf(b)
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b, 'es')
    })
    return [
      { id: ALL, label: `Todos (${catalog?.length ?? 0})` },
      ...names.map((n) => ({ id: n, label: `${n} (${counts.get(n)})` })),
    ]
  }, [catalog])

  const activeCount = useMemo(() => {
    const m = new Map<string, number>()
    for (const it of items) m.set(it.id, (m.get(it.id) ?? 0) + 1)
    return m
  }, [items])

  const filtered = useMemo(() => {
    const list = catalog ?? []
    const words = foldText(query.trim()).split(/\s+/).filter(Boolean)
    return list.filter((s) => {
      if (category !== ALL && s.category !== category) return false
      if (!words.length) return true
      const hay = foldText(`${s.name} ${s.id} ${s.category} ${s.description}`)
      return words.every((w) => hay.includes(w))
    })
  }, [catalog, query, category])

  const grouped = useMemo(() => {
    if (category !== ALL || query.trim()) return null
    const order = categories.slice(1).map((c) => c.id)
    return order.map((cat) => ({ cat, specs: filtered.filter((s) => s.category === cat) })).filter((g) => g.specs.length)
  }, [category, query, categories, filtered])

  const toggleExpanded = (uid: string) =>
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(uid)) n.delete(uid)
      else n.add(uid)
      return n
    })

  const full = items.length >= MAX_INDICATORS

  const renderSpec = (s: IndicatorSpec) => {
    const count = activeCount.get(s.id) ?? 0
    return (
      <button
        key={s.id}
        type="button"
        className="chart-ind-row"
        onClick={() => props.onAdd(s)}
        disabled={full}
        title={full ? `Máximo ${MAX_INDICATORS} indicadores` : `Añadir ${s.name} con sus valores por defecto`}
      >
        <div className="grow col gap-0">
          <div className="row gap-1">
            <strong className="ellipsis">{s.name}</strong>
            {count > 0 && (
              <Badge tone="accent" title="Ya está en el gráfico (puedes añadirlo otra vez con otros parámetros)">
                ×{count}
              </Badge>
            )}
          </div>
          {s.description && <div className="chart-ind-desc">{s.description}</div>}
        </div>
        <Badge tone={s.overlay ? 'info' : 'purple'} title={s.overlay ? 'Se dibuja sobre el precio' : 'Se dibuja en su propio panel'}>
          {s.overlay ? 'Sobre el precio' : 'Panel propio'}
        </Badge>
        <Plus size={15} className="chart-ind-plus" />
      </button>
    )
  }

  return (
    <Modal
      open={open}
      onClose={props.onClose}
      width={980}
      title={
        <span className="row gap-2">
          <ChartLine size={16} /> Indicadores
          <span className="muted small">
            {items.length}/{MAX_INDICATORS} activos
          </span>
        </span>
      }
      footer={
        <>
          <span className="tiny muted grow">
            Atajo: pulsa <kbd className="chart-kbd">i</kbd> en el gráfico. Los indicadores se guardan automáticamente.
          </span>
          {items.length > 0 && (
            <Button size="sm" variant="ghost" icon={<Trash2 size={13} />} onClick={props.onClear}>
              Quitar todos
            </Button>
          )}
          <Button variant="primary" onClick={props.onClose}>
            Listo
          </Button>
        </>
      }
    >
      <div className="chart-ind-dialog">
        <section className="chart-ind-catalog">
          <div className="chart-ind-search">
            <Search size={14} className="muted" />
            <input
              className="input"
              autoFocus
              value={query}
              placeholder="Buscar: RSI, Bollinger, VWAP, Ichimoku…"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && filtered.length > 0 && !full) props.onAdd(filtered[0])
              }}
            />
            {query && <IconButton size="sm" icon={<X size={13} />} title="Limpiar búsqueda" onClick={() => setQuery('')} />}
          </div>
          {catalog && categories.length > 1 && <Tabs pills tabs={categories} value={category} onChange={setCategory} />}
          <div className="chart-ind-list">
            {!catalog && !props.catalogError && <Loading label="Cargando catálogo de indicadores…" />}
            {!catalog && !!props.catalogError && <ErrorBox error={props.catalogError} onRetry={props.onRetryCatalog} />}
            {catalog && filtered.length === 0 && (
              <Empty icon={<Search size={20} />} title="Sin resultados">
                Prueba con otro nombre o categoría.
              </Empty>
            )}
            {catalog &&
              filtered.length > 0 &&
              (grouped
                ? grouped.map((g) => (
                    <div key={g.cat} className="col gap-1">
                      <h4 className="chart-ind-cat">{g.cat}</h4>
                      {g.specs.map(renderSpec)}
                    </div>
                  ))
                : filtered.map(renderSpec))}
          </div>
        </section>

        <section className="chart-ind-active">
          <h4 className="chart-ind-cat">Activos</h4>
          {items.length === 0 && (
            <Empty icon={<ChartLine size={20} />} title="Sin indicadores">
              Haz clic en cualquier indicador del catálogo para añadirlo al gráfico.
            </Empty>
          )}
          {items.map((it) => {
            const isOpen = expanded.has(it.uid)
            const outputs = it.spec?.outputs ?? []
            return (
              <div
                key={it.uid}
                className={`chart-ind-item ${isOpen ? 'open' : ''}`}
                ref={(el) => {
                  if (el) itemRefs.current.set(it.uid, el)
                  else itemRefs.current.delete(it.uid)
                }}
              >
                <div className="row gap-2">
                  <span className="row" style={{ gap: 2 }}>
                    {outputs.slice(0, 3).map((o) => (
                      <span key={o.key} className="swatch" style={{ background: safeColor(o.color) }} />
                    ))}
                  </span>
                  <button type="button" className="chart-ind-item-name" onClick={() => it.spec && toggleExpanded(it.uid)}>
                    <strong>{it.spec?.name ?? it.id}</strong>
                    <span className="muted mono tiny"> {paramsSummary(it.spec, it.params)}</span>
                  </button>
                  {it.status === 'pending' && <Spinner />}
                  {(it.status === 'error' || it.status === 'unknown') && (
                    <span className="neg" title={it.error ?? ''}>
                      <TriangleAlert size={14} />
                    </span>
                  )}
                  {it.spec && !it.spec.overlay && (
                    <Badge tone="purple" outline>
                      panel
                    </Badge>
                  )}
                  {it.spec && (
                    <IconButton
                      size="sm"
                      icon={<Settings2 size={14} />}
                      title={isOpen ? 'Ocultar parámetros' : 'Editar parámetros'}
                      active={isOpen}
                      onClick={() => toggleExpanded(it.uid)}
                    />
                  )}
                  <IconButton size="sm" icon={<X size={14} />} title="Quitar" onClick={() => props.onRemove(it.uid)} />
                </div>
                {(it.status === 'error' || it.status === 'unknown') && it.error && <div className="tiny neg">{it.error}</div>}
                {isOpen && it.spec && (
                  <ParamsEditor item={it} onUpdate={(p) => props.onUpdate(it.uid, p)} onReset={() => props.onReset(it.uid)} />
                )}
              </div>
            )
          })}
        </section>
      </div>
    </Modal>
  )
}

export default memo(IndicatorsDialog)
