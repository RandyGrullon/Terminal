// Fake browser of the int-fe-b tests: tests/fe-shell.env.ts (window, storage, fetch, WebSocket, manual timers) plus
// the `navigator` that lightweight-charts reads when it is imported with a `window` around (Node 20 has none).
// Import it FIRST in a test file: the modules under test read these globals when they are evaluated.
export * from './fe-shell.env'

const g = globalThis as Record<string, unknown>
if (typeof g.navigator === 'undefined') g.navigator = { userAgent: 'node', language: 'es-ES', languages: ['es-ES'] }
