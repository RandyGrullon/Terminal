// The "Copiloto" section (local AI with Ollama) in the UI: the contract types, the REST helpers, the setup steps while
// it is off, the error while Ollama cannot be reached, the chat with the tools it used, what is sent back (user and
// assistant turns only) and the honest notes. Same harness as autopilot-fe.test.mjs: the real sources are bundled in
// memory with Vite's esbuild and rendered with react-dom/server; fetch is faked (no network).
// Run: cd frontend && node --test tests/copilot-fe.test.mjs
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../src')

const memory = new Map()
globalThis.window ??= globalThis
globalThis.addEventListener ??= () => {}
globalThis.removeEventListener ??= () => {}
globalThis.navigator ??= { userAgent: 'node', language: 'es-ES' }
globalThis.location ??= { hash: '', origin: 'http://localhost', href: 'http://localhost/' }
globalThis.history ??= { state: null, replaceState() {}, pushState() {} }
globalThis.localStorage ??= {
  getItem: (k) => (memory.has(k) ? memory.get(k) : null),
  setItem: (k, v) => void memory.set(k, String(v)),
  removeItem: (k) => void memory.delete(k),
}

const ENTRY = `
export { api } from './api/client'
export { default as CopilotPage, CopilotView, SUGGESTIONS, TOOL_LABEL, historyFor, isPyChart, normalizeAIStatus, normalizeReply, openInStudio, scriptTitle, splitBlocks } from './pages/CopilotPage'
export { useStudio } from './components/studio/studioStore'
export { useApp } from './store'
export { GROUPS, FIELD_BY_KEY } from './components/settings/schema'
export { createElement } from 'react'
export { renderToStaticMarkup } from 'react-dom/server.browser'
`

const bundle = await build({
  stdin: { contents: ENTRY, resolveDir: SRC, loader: 'tsx', sourcefile: 'copilot-fe-entry.tsx' },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  jsx: 'automatic',
  loader: { '.css': 'empty' },
  define: { 'import.meta.env.VITE_API_BASE': 'undefined', 'process.env.NODE_ENV': '"production"' },
  logLevel: 'silent',
})
const BUNDLE_URL = `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
const M = await import(BUNDLE_URL)

const render = (Component, props = {}) => M.renderToStaticMarkup(M.createElement(Component, props))
const noop = () => {}

function status(over = {}) {
  return {
    enabled: true, url: 'http://127.0.0.1:11434', model: '', models: ['qwen3:8b'], reachable: true, error: null,
    search: 'none', search_ready: false,
    note: 'IA local (Ollama) con herramientas de solo lectura: explica, resume e investiga; no cambia ajustes ni opera. No es asesoría financiera.',
    ...over,
  }
}

const view = (over = {}) =>
  render(M.CopilotView, { status: M.normalizeAIStatus(status()), statusError: null, turns: [], input: '', sending: false,
    onInput: noop, onSend: noop, onRetry: noop, onSettings: noop, ...over })

test('contract: types-ai.ts has one field per line and is re-exported by types.ts', () => {
  const src = readFileSync(path.join(SRC, 'api', 'types-ai.ts'), 'utf8')
  const fields = (name) => {
    const body = src.split(`export interface ${name} {`)[1].split('\n}')[0]
    return body.split('\n').map((l) => l.trim().match(/^(\w+)(\?)?\s*:/)).filter(Boolean).map((m) => m[1] + (m[2] ?? ''))
  }
  assert.deepEqual(fields('AIStatus'), ['enabled', 'url', 'model', 'models', 'reachable', 'error', 'search', 'search_ready', 'note'])
  assert.deepEqual(fields('AIChatMessage'), ['role', 'content'])
  assert.deepEqual(fields('AIToolUse'), ['name', 'ok'])
  assert.deepEqual(fields('AIChatReply'), ['reply', 'model', 'tools', 'duration_ms'])
  const types = readFileSync(path.join(SRC, 'api', 'types.ts'), 'utf8')
  assert.ok(types.includes("export * from './types-ai'") && types.includes("'copilot'"))
})

test('client: api.ai.status and api.ai.chat call their routes', async () => {
  const realFetch = globalThis.fetch
  const seen = []
  globalThis.fetch = async (url, init = {}) => {
    seen.push({ url: String(url), method: init.method ?? 'GET', body: init.body ? JSON.parse(init.body) : undefined })
    return new Response(JSON.stringify({ reply: 'Hola', model: 'qwen3:8b', tools: [], duration_ms: 10 }), { status: 200,
      headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await M.api.ai.status()
    await M.api.ai.chat([{ role: 'user', content: 'hola' }])
  } finally {
    globalThis.fetch = realFetch
  }
  const at = (i) => [seen[i].method, new URL(seen[i].url, 'http://localhost').pathname, seen[i].body]
  assert.deepEqual(at(0), ['GET', '/api/ai/status', undefined])
  assert.deepEqual(at(1), ['POST', '/api/ai/chat', { messages: [{ role: 'user', content: 'hola' }] }])
})

test('off: the setup steps (Ollama, a model, Ajustes) and nothing to chat with', () => {
  const html = view({ status: M.normalizeAIStatus(status({ enabled: false, models: [], reachable: false })) })
  assert.ok(html.includes('Cómo activarlo') && html.includes('ollama pull qwen3:8b') && html.includes('Abrir Ajustes'), html)
  assert.ok(html.includes('>Desactivado<') && !html.includes('<textarea'), html)
  assert.ok(html.includes('No es asesoría financiera'), 'the disclaimer is always there')
})

test('unreachable or without a model: the backend error verbatim, no chat', () => {
  const html = view({ status: M.normalizeAIStatus(status({ reachable: false, models: [],
    error: 'No se pudo conectar con Ollama en http://127.0.0.1:11434: ¿está instalado y en marcha? (ConnectError)' })) })
  assert.ok(html.includes('No se pudo conectar con Ollama') && html.includes('>Sin conexión<') && !html.includes('<textarea'), html)
  const loading = view({ status: null })
  assert.ok(loading.includes('Comprobando el copiloto'))
})

test('ready: suggestions, then the conversation with the tools it used, its model and its honest note', () => {
  const empty = view()
  assert.ok(M.SUGGESTIONS.every((s) => empty.includes(s.replace('¿', '¿'))) && empty.includes('<textarea'), empty)
  assert.ok(empty.includes('>qwen3:8b<'), 'the model in use: the first one Ollama has')
  const turns = [
    { role: 'user', content: '¿Cómo va NEXUS automática?' },
    { role: 'assistant', content: 'Está pausada.', tools: [{ name: 'estado_nexus_automatica', ok: true }, { name: 'raro', ok: false }],
      model: 'qwen3:8b', ms: 3200 },
    { role: 'error', content: 'No se pudo conectar con Ollama' },
  ]
  const html = view({ turns, status: M.normalizeAIStatus(status({ search_ready: true })) })
  assert.ok(html.includes('Consultó: estado de NEXUS automática, raro') && html.includes('Está pausada.'), html)
  assert.ok(html.includes('qwen3:8b · 3,2 s') && html.includes('cp-error') && html.includes('Busca en internet'), html)
  assert.ok(html.includes('Puede equivocarse') && html.includes('no cambia ajustes ni opera'), html)
  // what goes back to the backend: no error notes, at most 20 turns
  assert.deepEqual(M.historyFor(turns), [{ role: 'user', content: '¿Cómo va NEXUS automática?' }, { role: 'assistant', content: 'Está pausada.' }])
  const many = Array.from({ length: 30 }, (_, i) => ({ role: 'user', content: String(i) }))
  assert.equal(M.historyFor(many).length, 20)
  assert.equal(M.normalizeReply({ reply: 1 }), null)
  assert.deepEqual(M.normalizeReply({ reply: 'ok', tools: [{ name: 'x', ok: true }, 'bad'], model: 'm', duration_ms: 5 }),
    { reply: 'ok', model: 'm', tools: [{ name: 'x', ok: true }], duration_ms: 5 })
  assert.equal(M.normalizeAIStatus({ enabled: 'si' }), null)
})

test('settings: the copilot group with its secret search key and a select of providers', () => {
  const group = M.GROUPS.find((g) => g.id === 'ai')
  assert.ok(group && group.title === 'Copiloto IA (Ollama)')
  assert.deepEqual(group.fields.map((f) => f.key), ['ai.enabled', 'ai.ollama_url', 'ai.model', 'ai.search_provider', 'ai.brave_api_key', 'ai.timeout_s', 'ai.context_length'])
  const ctx = M.FIELD_BY_KEY['ai.context_length']
  assert.equal(ctx.kind, 'int')
  assert.deepEqual([ctx.min, ctx.max], [2048, 131072])
  assert.equal(M.FIELD_BY_KEY['ai.brave_api_key'].kind, 'secret')
  assert.deepEqual(M.FIELD_BY_KEY['ai.search_provider'].options.map((o) => o.value), ['none', 'brave'])
})

test('lab: code blocks in an answer, with copy and «Abrir en PyChart» for a PyChart script only', () => {
  const code = 'strategy("Cruce rápido", initial_capital=10000)\nstrategy.entry("L", "long", when=ta.crossover(close, ta.ema(close, 20)))'
  const reply = `Aquí está:\n\n\`\`\`python\n${code}\n\`\`\`\nDesarrollo: 12 de 40 (muestra pequeña).\n\`\`\`\nx = 1\n\`\`\``
  assert.deepEqual(M.splitBlocks(reply), [
    { kind: 'text', text: 'Aquí está:', lang: '' },
    { kind: 'code', text: code, lang: 'python' },
    { kind: 'text', text: 'Desarrollo: 12 de 40 (muestra pequeña).', lang: '' },
    { kind: 'code', text: 'x = 1', lang: '' },
  ])
  // a fence the model left open runs to the end; plain text stays one block
  assert.deepEqual(M.splitBlocks('Mira:\n```py\nprint(1)\n'), [{ kind: 'text', text: 'Mira:', lang: '' }, { kind: 'code', text: 'print(1)', lang: 'py' }])
  assert.deepEqual(M.splitBlocks('Sin código'), [{ kind: 'text', text: 'Sin código', lang: '' }])
  assert.ok(M.isPyChart(code) && !M.isPyChart('x = 1') && M.isPyChart('indicator("RSI")\nplot(close)'))
  assert.equal(M.scriptTitle(code), 'Cruce rápido')
  assert.equal(M.scriptTitle('x = 1'), 'Estrategia')
  const turns = [{ role: 'user', content: 'Crea una estrategia' }, { role: 'assistant', content: reply, tools: [{ name: 'probar_estrategia', ok: true }] }]
  const html = view({ turns, onOpenCode: noop })
  assert.ok(html.includes('Consultó: prueba de estrategia') && html.includes('class="cp-code"'), html)
  assert.equal(html.split('Abrir en PyChart').length - 1, 1, 'only the PyChart script gets the button')
  assert.equal(html.split('>Copiar<').length - 1, 2, 'every code block can be copied')
  assert.ok(html.includes('strategy(&quot;Cruce rápido&quot;') && !html.includes('```'), 'the code is shown escaped, without its fences')
  // the user's own messages are never parsed as code
  const mine = view({ turns: [{ role: 'user', content: '```python\nstrategy("x")\n```' }], onOpenCode: noop })
  assert.ok(!mine.includes('cp-code') && mine.includes('```python'), mine)
  // without onOpenCode (e.g. the terminal) there is no button
  assert.ok(!view({ turns }).includes('Abrir en PyChart'))
})

test('lab: «Abrir en PyChart» opens the script as an unsaved draft in the editor', () => {
  const code = 'strategy("Ruptura de Donchian")\nstrategy.entry("L", "long", when=close > ta.highest(high, 20))'
  M.openInStudio(code)
  const st = M.useStudio.getState()
  const draft = st.drafts[st.active]
  assert.ok(st.active.startsWith('new:'), st.active)
  assert.equal(draft.name, 'Ruptura de Donchian (copiloto)')
  assert.equal(draft.code, `${code}\n`)
  assert.equal(draft.isNew, true)
  assert.ok(draft.description.includes('revísalo y pruébalo'))
  assert.equal(M.useApp.getState().page, 'studio')
})
