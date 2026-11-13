// Types of the local AI copilot (backend app.ai, Ollama): GET /api/ai/status and POST /api/ai/chat. Re-exported by
// types.ts. One field per line (tools/contract_check.py parses these interfaces).

/** GET /api/ai/status: its settings (never the search key), whether Ollama answers and the models it has. */
export interface AIStatus {
  enabled: boolean // ai.enabled
  url: string // the Ollama server (ai.ollama_url)
  model: string // ai.model ('' = the first model Ollama has)
  models: string[] // models of the server (only while enabled and reachable)
  reachable: boolean
  error: string | null // Spanish: unreachable, no model, an unknown model
  search: string // none | brave
  search_ready: boolean // web search configured (provider and key)
  note: string
}

/** One turn of the conversation sent to POST /api/ai/chat (the question last). */
export interface AIChatMessage {
  role: 'user' | 'assistant'
  content: string
}

/** A read-only tool the copilot used for an answer. */
export interface AIToolUse {
  name: string
  ok: boolean
}

/** POST /api/ai/chat -> its answer (400 {detail} when it is off, 503 when Ollama cannot be reached). */
export interface AIChatReply {
  reply: string
  model: string
  tools: AIToolUse[]
  duration_ms: number
}
