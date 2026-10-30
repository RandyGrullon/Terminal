// NEXUS live terminal: every backend event (signals, trades, scans, learning, errors) in real time plus a
// command line ("nexus>") with history, Tab completion and clickable symbols. Fills the .terminal-dock panel.
import {
  useCallback,
  useDeferredValue,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { ArrowDown, Download, Pause, Play, Search, SquareTerminal, Trash2, Volume2, VolumeX, X } from 'lucide-react'
import { api, errorMessage } from '../../api/client'
import type { CommandInfo, CommandResult, NexusEvent } from '../../api/types'
import { socket } from '../../api/ws'
import { runUiAction, useApp } from '../../store'
import { Spinner } from '../ui'
import TermView, { ROW_H } from './TermView'
import { termBuffer } from './termBuffer'
import {
  CATEGORIES,
  HISTORY_MAX,
  MAX_LINES,
  asLevel,
  countCategories,
  eventEntries,
  exportLine,
  inCategory,
  loadCategory,
  loadHistory,
  longestCommonPrefix,
  makeEntries,
  saveCategory,
  saveHistory,
  type Category,
  type TermEntry,
} from './termModel'
import './terminal.css'

const LOCAL_CLEAR = new Set(['clear', 'cls', 'limpiar'])
const TIPS = ['help', 'signal btc', 'check eth long', 'scan crypto', 'sim start']
const FLUSH_MS = 60
const HISTORY_SYNC_LIMIT = 400
const RESYNC_MARGIN_MS = 2000

/** Cached command catalogue (kept while the panel is closed). */
let commandsCache: CommandInfo[] = []

function isCommandInfo(c: unknown): c is CommandInfo {
  return !!c && typeof c === 'object' && typeof (c as CommandInfo).name === 'string'
}

/** Run over the socket when it is open (also rejects if the connection drops meanwhile), else over REST. */
function runCommand(text: string): Promise<CommandResult> {
  if (socket.status !== 'open') return api.terminal.exec(text)
  return new Promise<CommandResult>((resolve, reject) => {
    let settled = false
    let off: (() => void) | null = null
    const finish = (fn: () => void) => {
      if (settled) return
      settled = true
      off?.()
      fn()
    }
    off = socket.onStatus((s) => {
      if (s === 'closed') finish(() => reject(new Error('Se perdió la conexión mientras se ejecutaba el comando')))
    })
    if (settled) off()
    socket.command(text).then(
      (r) => finish(() => resolve(r)),
      (e: unknown) => finish(() => reject(e instanceof Error ? e : new Error(String(e)))),
    )
  })
}

function downloadText(filename: string, text: string): void {
  const blob = new Blob(['﻿', text], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 2000)
}

function fileStamp(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

function matchesFilter(e: TermEntry, category: Category, q: string): boolean {
  if (!inCategory(e, category)) return false
  if (!q) return true
  return e.text.toLowerCase().includes(q) || e.source.toLowerCase().includes(q)
}

export default function Terminal() {
  const wsStatus = useApp((s) => s.wsStatus)
  const notify = useApp((s) => s.notify)

  // ------------------------------------------------------------------ buffer + batching
  const [version, setVersion] = useState(0)
  const bump = useCallback(() => setVersion((v) => v + 1), [])
  const aliveRef = useRef(true)
  const pendingRef = useRef<TermEntry[]>([])
  const flushTimerRef = useRef<number | null>(null)

  const flushNow = useCallback((): boolean => {
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current)
      flushTimerRef.current = null
    }
    if (!pendingRef.current.length) return false
    const batch = pendingRef.current
    pendingRef.current = []
    termBuffer.append(batch)
    return true
  }, [])

  const push = useCallback(
    (entries: TermEntry[]) => {
      if (!entries.length) return
      const pending = pendingRef.current
      for (const e of entries) pending.push(e)
      if (pending.length > MAX_LINES) pending.splice(0, pending.length - MAX_LINES)
      if (flushTimerRef.current === null) {
        flushTimerRef.current = window.setTimeout(() => {
          flushTimerRef.current = null
          if (flushNow() && aliveRef.current) bump()
        }, FLUSH_MS)
      }
    },
    [bump, flushNow],
  )

  // ------------------------------------------------------------------ follow (autoscroll) state
  const [follow, setFollow] = useState(true)
  const followRef = useRef(true)
  const manualPauseRef = useRef(false)
  const [seenId, setSeenId] = useState(0)

  const pauseFollow = useCallback((manual: boolean) => {
    if (manual) manualPauseRef.current = true
    if (!followRef.current) return
    followRef.current = false
    setSeenId(termBuffer.last()?.id ?? 0)
    setFollow(false)
  }, [])

  const resumeFollow = useCallback(() => {
    manualPauseRef.current = false
    if (followRef.current) return
    followRef.current = true
    setFollow(true)
  }, [])

  const onScrollState = useCallback(
    (distance: number) => {
      if (followRef.current) {
        if (distance > ROW_H * 1.5) pauseFollow(false)
      } else if (distance <= 2 && !manualPauseRef.current) {
        resumeFollow()
      }
    },
    [pauseFollow, resumeFollow],
  )
  const onUserScrollUp = useCallback(() => pauseFollow(false), [pauseFollow])

  const clearOutput = useCallback(() => {
    pendingRef.current = []
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current)
      flushTimerRef.current = null
    }
    termBuffer.clear()
    resumeFollow()
    bump()
  }, [bump, resumeFollow])

  // ------------------------------------------------------------------ sound on signals
  const [sound, setSound] = useState(false)
  const soundRef = useRef(false)
  const audioRef = useRef<AudioContext | null>(null)
  const lastBeepRef = useRef(0)

  const beep = useCallback(() => {
    const ctx = audioRef.current
    if (!ctx) return
    const now = performance.now()
    if (now - lastBeepRef.current < 350) return
    lastBeepRef.current = now
    try {
      if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined)
      const t = ctx.currentTime
      const osc = ctx.createOscillator()
      const gain = ctx.createGain()
      osc.type = 'sine'
      osc.frequency.setValueAtTime(880, t)
      osc.frequency.setValueAtTime(1320, t + 0.07)
      gain.gain.setValueAtTime(0.0001, t)
      gain.gain.exponentialRampToValueAtTime(0.12, t + 0.012)
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.16)
      osc.connect(gain)
      gain.connect(ctx.destination)
      osc.start(t)
      osc.stop(t + 0.18)
    } catch {
      /* audio output unavailable */
    }
  }, [])

  const toggleSound = () => {
    const next = !soundRef.current
    if (next) {
      try {
        if (!audioRef.current) {
          const Ctor =
            window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
          if (Ctor) audioRef.current = new Ctor()
        }
        void audioRef.current?.resume().catch(() => undefined)
      } catch {
        audioRef.current = null
      }
      if (!audioRef.current) {
        notify('Este navegador no permite reproducir sonido', 'warn')
        return
      }
    }
    soundRef.current = next
    setSound(next)
    if (next) {
      lastBeepRef.current = 0
      beep()
    }
  }

  useEffect(
    () => () => {
      const ctx = audioRef.current
      audioRef.current = null
      if (ctx) void ctx.close().catch(() => undefined)
    },
    [],
  )

  // ------------------------------------------------------------------ commands catalogue
  const [commands, setCommands] = useState<CommandInfo[]>(commandsCache)
  const loadCommands = useCallback(async () => {
    try {
      const res = await api.terminal.commands()
      const list = (Array.isArray(res?.commands) ? res.commands : []).filter(isCommandInfo)
      commandsCache = list
      if (aliveRef.current) setCommands(list)
    } catch {
      /* server unavailable: retried on the next connection ('hello') */
    }
  }, [])

  const tokens = useMemo(() => {
    const map = new Map<string, CommandInfo>()
    for (const c of commands) {
      map.set(c.name.toLowerCase(), c)
      for (const a of Array.isArray(c.aliases) ? c.aliases : []) {
        const key = String(a).toLowerCase()
        if (key && !map.has(key)) map.set(key, c)
      }
    }
    return map
  }, [commands])

  // ------------------------------------------------------------------ history sync (REST) + live events (WS)
  const syncingRef = useRef(false)
  const syncHistory = useCallback(
    async (mode: 'initial' | 'resume', since: number) => {
      if (syncingRef.current) return
      syncingRef.current = true
      flushNow()
      const marker = mode === 'initial' ? termBuffer.welcome : termBuffer.last()
      try {
        const res = await api.events({ limit: HISTORY_SYNC_LIMIT })
        const floor = Math.max(since, termBuffer.clearedAt)
        const fresh = (Array.isArray(res?.events) ? res.events : [])
          .filter(
            (ev): ev is NexusEvent =>
              !!ev &&
              typeof ev.id === 'number' &&
              !termBuffer.seen.has(ev.id) &&
              (typeof ev.ts !== 'number' || ev.ts >= floor),
          )
          .sort((a, b) => a.id - b.id)
        flushNow()
        const entries: TermEntry[] = []
        for (const ev of fresh) if (!termBuffer.seen.has(ev.id)) entries.push(...eventEntries(ev))
        termBuffer.insertAfter(marker, entries, mode === 'initial' ? 'start' : 'end')
        termBuffer.historyLoaded = true
        if (aliveRef.current) bump()
      } catch {
        /* backend unreachable: the next connection ('hello') retries */
      } finally {
        syncingRef.current = false
      }
    },
    [bump, flushNow],
  )

  useEffect(() => {
    aliveRef.current = true
    if (!termBuffer.initialized) {
      termBuffer.initialized = true
      const banner = makeEntries(
        'sys',
        'NEXUS Terminal · señales, operaciones, escáner, aprendizaje y errores en vivo. Haz clic en un símbolo para abrir su gráfico.',
        'system',
      )
      const tips = makeEntries('sys', 'Escribe ', 'muted', { tips: TIPS })
      const keys = makeEntries('sys', 'Tab autocompleta · ↑/↓ historial · Ctrl+L limpia · Esc borra la línea', 'muted')
      termBuffer.append([...banner, ...tips, ...keys])
      termBuffer.welcome = keys[keys.length - 1]
    }
    if (!termBuffer.historyLoaded) void syncHistory('initial', 0)
    else if (termBuffer.closedAt !== null) void syncHistory('resume', termBuffer.closedAt - RESYNC_MARGIN_MS)
    termBuffer.closedAt = null
    if (!commandsCache.length) void loadCommands()
    bump()

    const offEvent = socket.on('event', (msg) => {
      const ev = msg.event
      if (!ev || typeof ev.id !== 'number' || termBuffer.seen.has(ev.id)) return
      termBuffer.seen.add(ev.id)
      push(eventEntries(ev))
      if (ev.level === 'signal' && soundRef.current) beep()
    })
    const offHello = socket.on('hello', (msg) => {
      push(makeEntries('sys', `Conectado al servidor NEXUS v${msg.version ?? '?'} · eventos en tiempo real activos`, 'success'))
      if (!termBuffer.historyLoaded) void syncHistory('initial', 0)
      else if (termBuffer.disconnectedAt !== null) void syncHistory('resume', termBuffer.disconnectedAt - RESYNC_MARGIN_MS)
      termBuffer.disconnectedAt = null
      void loadCommands()
    })
    const offUi = socket.on('ui', (msg) => {
      if (msg.action === 'clear') clearOutput()
    })
    let wasOpen = socket.status === 'open'
    const offStatus = socket.onStatus((s) => {
      if (s === 'open') {
        wasOpen = true
      } else if (s === 'closed' && wasOpen) {
        wasOpen = false
        if (termBuffer.disconnectedAt === null) termBuffer.disconnectedAt = Date.now()
        push(makeEntries('sys', 'Se perdió la conexión en tiempo real con el servidor · reintentando…', 'warn'))
      }
    })
    return () => {
      offEvent()
      offHello()
      offUi()
      offStatus()
      flushNow()
      termBuffer.closedAt = Date.now()
      aliveRef.current = false
    }
  }, [beep, bump, clearOutput, flushNow, loadCommands, push, syncHistory])

  // ------------------------------------------------------------------ filters
  const [category, setCategory] = useState<Category>(loadCategory)
  const [query, setQuery] = useState('')
  const deferredQuery = useDeferredValue(query)
  const q = deferredQuery.trim().toLowerCase()

  const filtered = useMemo(() => {
    const out: TermEntry[] = []
    for (const e of termBuffer.lines) if (matchesFilter(e, category, q)) out.push(e)
    return out
    // `version` invalidates the mutable buffer snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version, category, q])

  const counts = useMemo(
    () => countCategories(termBuffer.lines),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [version],
  )

  const newCount = useMemo(() => {
    if (follow) return 0
    let n = 0
    for (const e of filtered) if (e.id > seenId) n++
    return n
  }, [filtered, follow, seenId])

  const changeCategory = (c: Category) => {
    setCategory(c)
    saveCategory(c)
  }

  // ------------------------------------------------------------------ command line
  const [input, setInput] = useState('')
  const [focused, setFocused] = useState(false)
  const [running, setRunning] = useState(0)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const historyRef = useRef<string[]>(loadHistory())
  const histIdxRef = useRef(-1)
  const draftRef = useRef('')
  const tabRef = useRef<{ matches: string[]; index: number } | null>(null)

  const focusInput = useCallback(() => inputRef.current?.focus({ preventScroll: true }), [])

  const insertTip = useCallback(
    (cmd: string) => {
      setInput(cmd)
      histIdxRef.current = -1
      tabRef.current = null
      focusInput()
    },
    [focusInput],
  )

  const applyResult = useCallback(
    (res: CommandResult) => {
      const lines = Array.isArray(res?.lines) ? res.lines : []
      const out: TermEntry[] = []
      for (const l of lines) {
        const text = typeof l?.text === 'string' ? l.text : String(l?.text ?? '')
        out.push(...makeEntries('out', text, asLevel(l?.level)))
      }
      if (res && res.ok === false && !lines.length) out.push(...makeEntries('out', '✖ El comando no se pudo completar', 'error'))
      push(out)
      for (const a of Array.isArray(res?.actions) ? res.actions : []) {
        if (!a || typeof a !== 'object') continue
        if (a.action === 'clear') {
          clearOutput()
          continue
        }
        try {
          runUiAction(a)
        } catch (err) {
          push(makeEntries('out', `No se pudo aplicar la acción '${String(a.action)}': ${errorMessage(err)}`, 'warn'))
        }
      }
    },
    [clearOutput, push],
  )

  const execute = useCallback(
    async (raw: string) => {
      const text = raw.trim()
      if (!text) return
      const h = historyRef.current
      if (h[h.length - 1] !== text) {
        h.push(text)
        if (h.length > HISTORY_MAX) h.splice(0, h.length - HISTORY_MAX)
        saveHistory(h)
      }
      resumeFollow()
      if (LOCAL_CLEAR.has(text.toLowerCase())) {
        clearOutput()
        return
      }
      push(makeEntries('cmd', text, 'info'))
      setRunning((n) => n + 1)
      try {
        applyResult(await runCommand(text))
      } catch (err) {
        push(makeEntries('out', `✖ ${errorMessage(err)}`, 'error'))
      } finally {
        if (aliveRef.current) setRunning((n) => Math.max(0, n - 1))
      }
    },
    [applyResult, clearOutput, push, resumeFollow],
  )

  const historyPrev = () => {
    const h = historyRef.current
    if (!h.length) return
    if (histIdxRef.current === -1) {
      draftRef.current = input
      histIdxRef.current = h.length - 1
    } else {
      histIdxRef.current = Math.max(0, histIdxRef.current - 1)
    }
    setInput(h[histIdxRef.current])
  }

  const historyNext = () => {
    const h = historyRef.current
    if (histIdxRef.current === -1) return
    const next = histIdxRef.current + 1
    if (next >= h.length) {
      histIdxRef.current = -1
      setInput(draftRef.current)
    } else {
      histIdxRef.current = next
      setInput(h[next])
    }
  }

  const complete = () => {
    const raw = input.trimStart()
    if (/\s/.test(raw)) return // only the command name is completed
    const tab = tabRef.current
    if (tab && tab.matches.length > 1 && raw === tab.matches[tab.index]) {
      const index = (tab.index + 1) % tab.matches.length
      tabRef.current = { matches: tab.matches, index }
      setInput(tab.matches[index])
      return
    }
    const prefix = raw.toLowerCase()
    const matches = Array.from(tokens.keys())
      .filter((t) => t.startsWith(prefix))
      .sort((a, b) => a.localeCompare(b))
    if (!matches.length) return
    if (matches.length === 1) {
      tabRef.current = null
      setInput(`${matches[0]} `)
      return
    }
    const lcp = longestCommonPrefix(matches)
    if (lcp.length > prefix.length) {
      tabRef.current = null
      setInput(lcp)
      return
    }
    tabRef.current = { matches, index: 0 }
    setInput(matches[0])
  }

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      const text = input
      setInput('')
      histIdxRef.current = -1
      tabRef.current = null
      void execute(text)
    } else if (e.key === 'Tab' && !e.shiftKey && input.trim()) {
      e.preventDefault()
      complete()
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      historyPrev()
    } else if (e.key === 'ArrowDown') {
      e.preventDefault()
      historyNext()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      setInput('')
      histIdxRef.current = -1
      tabRef.current = null
    } else if ((e.key === 'l' || e.key === 'L') && e.ctrlKey && !e.altKey && !e.metaKey) {
      e.preventDefault()
      clearOutput()
    }
  }

  const trimmedStart = input.trimStart()
  const typingArgs = /\S\s/.test(trimmedStart)
  const firstToken = (trimmedStart.split(/\s+/)[0] ?? '').toLowerCase()
  const suggestions = useMemo(() => {
    const prefix = input.trim().toLowerCase()
    if (!prefix || typingArgs) return []
    const seenCmd = new Set<string>()
    const out: { token: string; cmd: CommandInfo }[] = []
    for (const [token, cmd] of tokens) {
      if (token.startsWith(prefix) && !seenCmd.has(cmd.name)) {
        seenCmd.add(cmd.name)
        out.push({ token, cmd })
      }
    }
    out.sort((a, b) => a.token.localeCompare(b.token))
    return out.slice(0, 8)
  }, [input, typingArgs, tokens])
  const usage = typingArgs ? tokens.get(firstToken) ?? null : null
  const showSuggest = focused && (usage !== null || suggestions.length > 0)

  // ------------------------------------------------------------------ export
  const exportTxt = () => {
    if (flushNow()) bump()
    const lines = termBuffer.lines.filter((e) => matchesFilter(e, category, q))
    if (!lines.length) {
      notify('No hay líneas para exportar con el filtro actual', 'warn')
      return
    }
    const header = `NEXUS Terminal · exportado ${new Date().toLocaleString('es-ES')} · ${lines.length} líneas`
    downloadText(`nexus-terminal-${fileStamp()}.txt`, [header, '', ...lines.map(exportLine)].join('\n'))
  }

  const connLabel = wsStatus === 'open' ? 'En vivo' : wsStatus === 'connecting' ? 'Conectando…' : 'Sin conexión'
  const filterActive = category !== 'all' || q !== ''

  return (
    <div className="term">
      <div className="term-toolbar">
        <span className="term-title">
          <SquareTerminal size={14} />
          <span className="term-title-text">Terminal</span>
        </span>
        <div className="term-chips" role="tablist" aria-label="Filtrar por tipo de evento">
          {CATEGORIES.map((c) => (
            <button
              key={c.id}
              type="button"
              role="tab"
              aria-selected={category === c.id}
              className={`term-chip ${category === c.id ? 'active' : ''}`}
              title={c.title}
              onClick={() => changeCategory(c.id)}
            >
              {c.label}
              {c.id !== 'all' && counts[c.id] > 0 && (
                <span className={`term-count ${c.id === 'error' ? 'hot' : ''}`}>{counts[c.id] > 999 ? '999+' : counts[c.id]}</span>
              )}
            </button>
          ))}
        </div>
        <div className="term-filter">
          <Search size={12} className="term-filter-ico" />
          <input
            className="input"
            value={query}
            placeholder="Filtrar texto…"
            aria-label="Filtrar texto"
            spellCheck={false}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setQuery('')
            }}
          />
          {query && (
            <button type="button" className="term-filter-clr" title="Quitar el filtro de texto" onClick={() => setQuery('')}>
              <X size={12} />
            </button>
          )}
        </div>
        <div className="grow" />
        <button
          type="button"
          className={`term-btn ${!follow ? 'on' : ''}`}
          title={follow ? 'Pausar el desplazamiento automático' : 'Reanudar el desplazamiento automático'}
          onClick={() => (follow ? pauseFollow(true) : resumeFollow())}
        >
          {follow ? <Pause size={13} /> : <Play size={13} />}
          <span className="term-btn-label">{follow ? 'Pausar' : 'Seguir'}</span>
        </button>
        <button
          type="button"
          className={`term-btn ${sound ? 'on' : ''}`}
          aria-pressed={sound}
          title={sound ? 'Sonido en cada señal: activado (clic para silenciar)' : 'Sonido en cada señal: desactivado'}
          onClick={toggleSound}
        >
          {sound ? <Volume2 size={13} /> : <VolumeX size={13} />}
          <span className="term-btn-label">Sonido</span>
        </button>
        <button type="button" className="term-btn icon" title="Exportar lo visible a .txt" onClick={exportTxt}>
          <Download size={13} />
        </button>
        <button type="button" className="term-btn icon" title="Limpiar la terminal (Ctrl+L)" onClick={clearOutput}>
          <Trash2 size={13} />
        </button>
        <span className="term-conn" title={`Conexión en tiempo real: ${connLabel}`}>
          <span className={`ws-dot ${wsStatus}`} />
          <span className="term-conn-label">{connLabel}</span>
        </span>
      </div>

      <div className="term-body">
        <TermView
          lines={filtered}
          follow={follow}
          onScrollState={onScrollState}
          onUserScrollUp={onUserScrollUp}
          onBackgroundClick={focusInput}
          onTip={insertTip}
        />
        {filtered.length === 0 && (
          <div className="term-empty">
            {filterActive ? 'Ninguna línea coincide con el filtro actual.' : 'Terminal vacía. Escribe un comando abajo.'}
          </div>
        )}
        {!follow && newCount > 0 && (
          <button type="button" className="term-new" onClick={resumeFollow} title="Ir al final y seguir en vivo">
            <ArrowDown size={12} /> {newCount > 999 ? '999+' : newCount} nuevas
          </button>
        )}
        {showSuggest && (
          <div className="term-suggest">
            {usage ? (
              <div className="term-usage">
                <span className="term-usage-cmd">{usage.usage || usage.name}</span>
                {usage.help && <span className="term-usage-help"> — {usage.help}</span>}
                {usage.aliases?.length > 0 && <span className="term-usage-help"> · alias: {usage.aliases.join(', ')}</span>}
              </div>
            ) : (
              suggestions.map((s) => (
                <button
                  key={s.cmd.name}
                  type="button"
                  className="term-sug"
                  title={s.cmd.usage}
                  onMouseDown={(e) => {
                    e.preventDefault()
                    insertTip(`${s.token} `)
                  }}
                >
                  <span className="term-sug-name">{s.token}</span>
                  {s.cmd.help && <span className="term-sug-help">{s.cmd.help}</span>}
                </button>
              ))
            )}
          </div>
        )}
      </div>

      <div
        className="term-input-row"
        onMouseDown={(e) => {
          if (e.target === e.currentTarget || (e.target as HTMLElement).classList.contains('term-prompt')) {
            e.preventDefault()
            focusInput()
          }
        }}
      >
        <span className="term-prompt">nexus&gt;</span>
        <input
          ref={inputRef}
          className="term-input"
          value={input}
          spellCheck={false}
          autoComplete="off"
          autoCapitalize="off"
          aria-label="Comando de la terminal"
          placeholder="Escribe un comando… (help, signal btc, check eth long, scan crypto, sim start)"
          onChange={(e) => {
            setInput(e.target.value)
            histIdxRef.current = -1
          }}
          onKeyDown={onKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
        />
        {running > 0 && (
          <span className="term-running">
            <Spinner /> ejecutando{running > 1 ? ` (${running})` : ''}…
          </span>
        )}
        <span className="term-hint">Tab · ↑↓ · Ctrl+L</span>
      </div>
    </div>
  )
}
