// "Copiloto": a chat with the local AI (Ollama on the user's own machine). It reads the app's data with read-only
// tools (NEXUS automática's status, decisions and journal, the comparison with random entries, NEXUS's read of a market,
// the semáforo and news, and web search when the user configured a key) and never changes settings nor trades. The
// conversation lives in this page only (nothing is stored). Off by default: the page explains how to set it up.
// Strategy lab: the copilot writes PyChart scripts and tests them in the sandbox; its code blocks can be copied or
// opened in PyChart as an unsaved draft (the user saves it there).
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Bot, Check, Copy, FileCode, RefreshCw, Send, Settings as SettingsIcon, Sparkles } from 'lucide-react'
import { api, errorMessage } from '../api/client'
import type { AIChatMessage, AIChatReply, AIStatus } from '../api/types'
import { newKey, useStudio } from '../components/studio/studioStore'
import { Badge, Button, Card, Disclaimer, ErrorBox, Loading } from '../components/ui'
import { useApp } from '../store'
import '../components/copilot/copilot.css'

/** Spanish names of the copilot's tools (backend app.ai.tools). */
export const TOOL_LABEL: Record<string, string> = {
  estado_nexus_automatica: 'estado de NEXUS automática',
  decisiones_recientes: 'decisiones recientes',
  diario_de_operaciones: 'diario de operaciones',
  comparacion_con_el_azar: 'comparación con el azar',
  analisis_nexus: 'análisis de NEXUS',
  semaforo_y_noticias: 'semáforo y noticias',
  referencia_pychart: 'referencia de PyChart',
  probar_estrategia: 'prueba de estrategia',
  buscar_en_internet: 'búsqueda en internet',
}

export const SUGGESTIONS = [
  '¿Cómo va NEXUS automática?',
  '¿Por qué no ha operado hoy?',
  '¿Qué dice su diario de operaciones?',
  '¿Qué ve NEXUS ahora en BTC?',
  '¿Qué noticias importantes hay esta semana?',
  'Crea y prueba una estrategia de cruce de medias en BTC 1h',
]

const MAX_HISTORY = 20

export interface ChatTurn {
  role: 'user' | 'assistant' | 'error'
  content: string
  tools?: { name: string; ok: boolean }[]
  model?: string
  ms?: number
}

const isStr = (v: unknown): v is string => typeof v === 'string'

/** Defensive parse of an AIStatus: null when it is not one. */
export function normalizeAIStatus(raw: unknown): AIStatus | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.enabled !== 'boolean') return null
  return {
    enabled: o.enabled,
    url: isStr(o.url) ? o.url : '',
    model: isStr(o.model) ? o.model : '',
    models: Array.isArray(o.models) ? o.models.filter(isStr) : [],
    reachable: o.reachable === true,
    error: isStr(o.error) && o.error ? o.error : null,
    search: isStr(o.search) ? o.search : 'none',
    search_ready: o.search_ready === true,
    note: isStr(o.note) ? o.note : '',
  }
}

/** Defensive parse of an AIChatReply: null without its answer. */
export function normalizeReply(raw: unknown): AIChatReply | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (!isStr(o.reply)) return null
  const tools = Array.isArray(o.tools)
    ? o.tools
        .filter((t): t is Record<string, unknown> => !!t && typeof t === 'object' && isStr((t as Record<string, unknown>).name))
        .map((t) => ({ name: String(t.name), ok: t.ok === true }))
    : []
  return { reply: o.reply, model: isStr(o.model) ? o.model : '', tools, duration_ms: typeof o.duration_ms === 'number' ? o.duration_ms : 0 }
}

/** A piece of an answer: plain text or a fenced code block (```lang … ```). */
export interface Block {
  kind: 'text' | 'code'
  text: string
  lang: string
}

const FENCE_RE = /```([\w+-]*)[^\n]*\n([\s\S]*?)(?:```|$)/g

/** Splits an answer into text and code blocks (a fence left open by the model runs to the end). */
export function splitBlocks(text: string): Block[] {
  const out: Block[] = []
  let last = 0
  for (const m of text.matchAll(FENCE_RE)) {
    const at = m.index ?? 0
    if (at > last) out.push({ kind: 'text', text: text.slice(last, at).replace(/^\n+|\n+$/g, ''), lang: '' })
    out.push({ kind: 'code', text: m[2].replace(/\n+$/, ''), lang: m[1].toLowerCase() })
    last = at + m[0].length
  }
  if (last < text.length) out.push({ kind: 'text', text: text.slice(last).replace(/^\n+|\n+$/g, ''), lang: '' })
  return out.filter((b) => b.kind === 'code' || b.text.trim() !== '')
}

/** A PyChart script: it declares strategy(...) or indicator(...). */
export const isPyChart = (code: string): boolean => /^\s*(?:strategy|indicator)\s*\(/m.test(code)

/** The title of its strategy(...) / indicator(...) declaration ("Estrategia" without one). */
export function scriptTitle(code: string): string {
  const m = code.match(/^\s*(?:strategy|indicator)\(\s*(['"])(.+?)\1/m)
  return m ? m[2].trim().slice(0, 80) : 'Estrategia'
}

/** Opens a script of the copilot in PyChart as an unsaved draft (the user reviews and saves it there). */
export function openInStudio(code: string): void {
  const st = useStudio.getState()
  const key = newKey()
  st.putDraft(key, {
    name: `${scriptTitle(code)} (copiloto)`,
    code: code.endsWith('\n') ? code : `${code}\n`,
    description: 'Creado con el copiloto: revísalo y pruébalo antes de usarlo.',
    isNew: true,
    updatedAt: Date.now(),
  })
  st.setActive(key)
  useApp.getState().setPage('studio')
}

function CodeBlock({ code, lang, onOpen }: { code: string; lang: string; onOpen?: (code: string) => void }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    const done = () => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }
    try {
      void navigator.clipboard?.writeText(code).then(done, () => undefined)
    } catch {
      // no clipboard (an insecure context): the code can still be selected by hand
    }
  }
  return (
    <div className="cp-code">
      <div className="cp-code-head">
        <span className="tiny muted">{lang || 'código'}</span>
        <span className="header-spacer" />
        <Button size="sm" variant="ghost" icon={copied ? <Check size={12} /> : <Copy size={12} />} onClick={copy} title="Copiar el código">
          {copied ? 'Copiado' : 'Copiar'}
        </Button>
        {onOpen && isPyChart(code) && (
          <Button size="sm" icon={<FileCode size={12} />} onClick={() => onOpen(code)} title="Abrirlo en PyChart como borrador sin guardar">
            Abrir en PyChart
          </Button>
        )}
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  )
}

/** An answer of the copilot: its text and its code blocks. */
function MessageBody({ text, onOpenCode }: { text: string; onOpenCode?: (code: string) => void }) {
  return (
    <>
      {splitBlocks(text).map((b, i) =>
        b.kind === 'code' ? (
          <CodeBlock key={i} code={b.text} lang={b.lang} onOpen={onOpenCode} />
        ) : (
          <div key={i} className="cp-text">
            {b.text}
          </div>
        ),
      )}
    </>
  )
}

/** What the backend gets: the user / assistant turns (never the error notes), the newest MAX_HISTORY. */
export function historyFor(turns: ChatTurn[]): AIChatMessage[] {
  return turns
    .filter((t): t is ChatTurn & { role: 'user' | 'assistant' } => t.role === 'user' || t.role === 'assistant')
    .map((t) => ({ role: t.role, content: t.content }))
    .slice(-MAX_HISTORY)
}

export interface CopilotViewProps {
  status: AIStatus | null
  statusError: string | null
  turns: ChatTurn[]
  input: string
  sending: boolean
  onInput: (text: string) => void
  onSend: (text: string) => void
  onRetry: () => void
  onSettings: () => void
  /** Opens a PyChart script of an answer in the editor (absent: no button). */
  onOpenCode?: (code: string) => void
}

function SetupNotice({ onSettings }: { onSettings: () => void }) {
  return (
    <Card title="Cómo activarlo" sub="Una IA en tu propio PC: tus datos no salen de él">
      <ol className="cp-steps small">
        <li>
          Instala <b>Ollama</b> (ollama.com) en el PC donde corre NEXUS.
        </li>
        <li>
          Descarga un modelo, por ejemplo <code>ollama pull qwen3:8b</code>. Con una GPU de 12 GB caben modelos de hasta unos 14B.
        </li>
        <li>Actívalo en Ajustes → Copiloto IA (Ollama). Si quieres que busque en internet, añade allí una clave de Brave Search.</li>
      </ol>
      <Button size="sm" variant="primary" icon={<SettingsIcon size={14} />} onClick={onSettings}>
        Abrir Ajustes
      </Button>
    </Card>
  )
}

/** The page without its state (rendered by the tests). */
export function CopilotView(props: CopilotViewProps) {
  const { status, statusError, turns, input, sending, onInput, onSend, onRetry, onSettings, onOpenCode } = props
  const ready = !!status && status.enabled && status.reachable && !status.error
  const model = status ? status.model || status.models[0] || '' : ''
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (input.trim() && !sending) onSend(input)
    }
  }
  return (
    <>
      <div className="page-header">
        <div className="col gap-0">
          <h1 className="row gap-2">
            <Sparkles size={18} /> Copiloto
            {status && (
              <Badge tone={ready ? 'accent' : 'muted'}>{!status.enabled ? 'Desactivado' : ready ? model : 'Sin conexión'}</Badge>
            )}
            {status?.search_ready && <Badge tone="info">Busca en internet</Badge>}
          </h1>
          <span className="sub">IA local con Ollama: entiende tus datos de NEXUS, explica y resume. No opera ni cambia ajustes.</span>
        </div>
        <span className="header-spacer" />
        <Button icon={<RefreshCw size={14} />} variant="ghost" onClick={onRetry} title="Volver a comprobar el copiloto">
          Comprobar
        </Button>
      </div>
      {statusError && <ErrorBox error={statusError} onRetry={onRetry} />}
      {!status && !statusError && <Loading label="Comprobando el copiloto…" />}
      {status && !status.enabled && <SetupNotice onSettings={onSettings} />}
      {status && status.enabled && status.error && (
        <div className="notice ap-brake" role="alert">
          <span>{status.error}</span>
        </div>
      )}
      {ready && (
        <Card className="cp-chat" bodyClassName="cp-body">
          {turns.length === 0 ? (
            <div className="cp-empty">
              <Bot size={22} />
              <p className="small muted">
                Pregúntale por NEXUS automática, sus decisiones y errores, un mercado o las noticias, o pídele que cree y pruebe una
                estrategia de PyChart.
              </p>
              <div className="cp-suggest">
                {SUGGESTIONS.map((s) => (
                  <button key={s} type="button" className="btn btn-sm" onClick={() => onSend(s)} disabled={sending}>
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="cp-list" aria-live="polite">
              {turns.map((t, i) => (
                <div key={i} className={`cp-msg cp-${t.role}`}>
                  {t.role === 'assistant' && t.tools && t.tools.length > 0 && (
                    <div className="tiny muted">Consultó: {t.tools.map((x) => TOOL_LABEL[x.name] ?? x.name).join(', ')}</div>
                  )}
                  {t.role === 'assistant' ? <MessageBody text={t.content} onOpenCode={onOpenCode} /> : <div className="cp-text">{t.content}</div>}
                  {t.role === 'assistant' && t.model && (
                    <div className="tiny muted">
                      {t.model}
                      {typeof t.ms === 'number' ? ` · ${(t.ms / 1000).toLocaleString('es-ES', { maximumFractionDigits: 1 })} s` : ''}
                    </div>
                  )}
                </div>
              ))}
              {sending && <Loading label="Pensando…" />}
            </div>
          )}
          <div className="cp-input">
            <textarea
              value={input}
              onChange={(e) => onInput(e.target.value)}
              onKeyDown={onKey}
              placeholder="Escribe tu pregunta (Intro para enviar, Mayús+Intro para otra línea)"
              rows={2}
              disabled={sending}
              aria-label="Pregunta para el copiloto"
            />
            <Button variant="primary" icon={<Send size={14} />} onClick={() => onSend(input)} disabled={!input.trim() || sending} loading={sending}>
              Enviar
            </Button>
          </div>
          <div className="tiny muted">{status?.note} Puede equivocarse: comprueba los datos en la app.</div>
        </Card>
      )}
      <Disclaimer />
    </>
  )
}

export default function CopilotPage() {
  const [status, setStatus] = useState<AIStatus | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const check = useCallback(async () => {
    setStatusError(null)
    try {
      const s = normalizeAIStatus(await api.ai.status())
      if (!alive.current) return
      if (!s) throw new Error('Respuesta del copiloto no válida')
      setStatus(s)
    } catch (err) {
      if (alive.current) setStatusError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    void check()
  }, [check])

  const send = useCallback(
    async (text: string) => {
      const question = text.trim()
      if (!question || sending) return
      const next: ChatTurn[] = [...turns, { role: 'user', content: question }]
      setTurns(next)
      setInput('')
      setSending(true)
      try {
        const r = normalizeReply(await api.ai.chat(historyFor(next)))
        if (!alive.current) return
        if (!r) throw new Error('Respuesta del copiloto no válida')
        setTurns((t) => [...t, { role: 'assistant', content: r.reply, tools: r.tools, model: r.model, ms: r.duration_ms }])
      } catch (err) {
        if (alive.current) setTurns((t) => [...t, { role: 'error', content: errorMessage(err) }])
      } finally {
        if (alive.current) setSending(false)
      }
    },
    [sending, turns],
  )

  return (
    <CopilotView
      status={status}
      statusError={statusError}
      turns={turns}
      input={input}
      sending={sending}
      onInput={setInput}
      onSend={(t) => void send(t)}
      onRetry={() => void check()}
      onSettings={() => useApp.getState().setPage('settings')}
      onOpenCode={openInStudio}
    />
  )
}
