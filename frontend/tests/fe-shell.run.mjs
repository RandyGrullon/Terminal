// Runs the fe-shell regression tests (tests/fe-shell.*.test.ts): each file is bundled with esbuild (types stripped,
// no type-check: `tsc --noEmit` covers src/) into a temporary folder and run with the Node test runner. Each file is
// its own bundle, so it gets fresh module state. No network, no browser: tests/fe-shell.env.ts fakes window,
// localStorage, document, location / history, fetch, WebSocket and timers.
// - VITE_API_BASE is the bare domain as Railway shows it (no https://, trailing slash): the cloud-mode modules must
//   still talk to https://<domain>.
// - components/ui.tsx additionally exports its internal dialog focus helper (trapDialog) to the tests only.
// From frontend/:  node tests/fe-shell.run.mjs            (one file: node tests/fe-shell.run.mjs session)
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const API_BASE_AS_TYPED = 'nexus-production.up.railway.app/' // tests expect https://nexus-production.up.railway.app

const exposeInternals = {
  name: 'fe-shell-expose-internals',
  setup(b) {
    b.onLoad({ filter: /[\\/]components[\\/]ui\.tsx$/ }, async (args) => ({
      contents: `${await readFile(args.path, 'utf8')}\nexport { trapDialog as __trapDialog }\n`,
      loader: 'tsx',
    }))
  },
}

const here = dirname(fileURLToPath(import.meta.url))
const only = process.argv[2] // optional filter, e.g. "session"
const files = readdirSync(here)
  .filter((f) => /^fe-shell\..+\.test\.ts$/.test(f) && (!only || f.includes(only)))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'fe-shell-tests-'))
try {
  await build({
    entryPoints: files.map((f) => join(here, f)),
    outdir: out,
    outExtension: { '.js': '.mjs' },
    bundle: true,
    platform: 'node',
    format: 'esm',
    jsx: 'automatic',
    logLevel: 'warning',
    define: { 'import.meta.env': JSON.stringify({ VITE_API_BASE: API_BASE_AS_TYPED }) },
    // react-dom/server (CommonJS) requires Node built-ins: give the ESM bundles a real `require`.
    banner: { js: "import { createRequire as __fsCreateRequire } from 'node:module'; const require = __fsCreateRequire(import.meta.url);" },
    plugins: [exposeInternals],
  })
  const bundles = readdirSync(out)
    .filter((f) => f.endsWith('.mjs'))
    .sort()
    .map((f) => join(out, f))
  const run = spawnSync(process.execPath, ['--test', ...bundles], { stdio: 'inherit' })
  process.exitCode = run.status ?? 1
} finally {
  rmSync(out, { recursive: true, force: true })
}
