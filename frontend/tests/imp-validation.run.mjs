// Runs the validation-card tests (tests/imp-validation.*.test.ts) like the other runners: each file is bundled with
// esbuild (types stripped; `tsc --noEmit` covers src/) into a temporary folder and run with the Node test runner on the
// fake browser of tests/fe-shell.env.ts. CSS imports are ignored. No network, no browser.
// From frontend/:  node tests/imp-validation.run.mjs
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const files = readdirSync(here)
  .filter((f) => /^imp-validation\..+\.test\.ts$/.test(f))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'imp-validation-tests-'))
try {
  for (const f of files) {
    await build({
      entryPoints: [join(here, f)],
      outfile: join(out, f.replace(/\.ts$/, '.mjs')),
      bundle: true,
      platform: 'node',
      format: 'esm',
      jsx: 'automatic',
      logLevel: 'warning',
      loader: { '.css': 'empty' },
      define: { 'import.meta.env': JSON.stringify({ VITE_API_BASE: '' }) },
      // react-dom/server (CommonJS) requires Node built-ins: give the ESM bundles a real `require`.
      banner: { js: "import { createRequire as __ivCreateRequire } from 'node:module'; const require = __ivCreateRequire(import.meta.url);" },
    })
  }
  const bundles = readdirSync(out)
    .filter((f) => f.endsWith('.mjs'))
    .sort()
    .map((f) => join(out, f))
  const run = spawnSync(process.execPath, ['--test', ...bundles], { stdio: 'inherit' })
  process.exitCode = run.status ?? 1
} finally {
  rmSync(out, { recursive: true, force: true })
}
