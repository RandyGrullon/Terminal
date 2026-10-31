// Runs the paper-db tests (tests/imp-paper-db.*.test.ts): pending limit / stop orders and portfolio risk of the demo
// account. Each file is bundled with esbuild (types stripped, no type-check: `tsc --noEmit` covers src/) into a
// temporary folder and run with the Node test runner. No network, no browser. From frontend/:
//   node tests/imp-paper-db.run.mjs
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const files = readdirSync(here)
  .filter((f) => /^imp-paper-db\..+\.test\.ts$/.test(f))
  .sort()
const out = mkdtempSync(join(tmpdir(), 'imp-paper-db-tests-'))
try {
  await build({
    entryPoints: files.map((f) => join(here, f)),
    outdir: out,
    outExtension: { '.js': '.mjs' },
    bundle: true,
    platform: 'node',
    format: 'esm',
    logLevel: 'warning',
  })
  const bundles = readdirSync(out)
    .filter((f) => f.endsWith('.mjs'))
    .sort()
    .map((f) => join(out, f))
  const run = spawnSync(process.execPath, ['--test', ...bundles], {
    stdio: 'inherit',
    env: { ...process.env, PAPER_DB_REPO: resolve(here, '..', '..') },
  })
  process.exitCode = run.status ?? 1
} finally {
  rmSync(out, { recursive: true, force: true })
}
