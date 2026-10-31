// Measures what a first visit downloads: builds the UI with the project's vite.config.ts into a scratch folder (never
// frontend/dist) and reports, raw and gzip (per file, as the server sends them), the JavaScript of the first load
// (index.html: entry + modulepreload), of the section opened first, and of each section opened later.
// Not part of `npm test` (it runs a full Vite build). From frontend/:
//   node tests/imp-fe-perf.measure.mjs ../backend/.tmp/fe-perf/dist                 report
//   node tests/imp-fe-perf.measure.mjs ../backend/.tmp/fe-perf/dist --json          machine-readable
//   node tests/imp-fe-perf.measure.mjs <out> --root=<frontend copy>                 another checkout (same node_modules)
//   node tests/imp-fe-perf.measure.mjs <out> --budget-first-gzip=200                exit 1 if #/chart needs more (kB)
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'
import { build } from 'vite'

const here = path.dirname(fileURLToPath(import.meta.url))
const args = process.argv.slice(2)
const opt = (name) => args.find((a) => a.startsWith(`--${name}=`))?.slice(name.length + 3)
const outArg = args.find((a) => !a.startsWith('--'))
if (!outArg) {
  console.error('Uso: node tests/imp-fe-perf.measure.mjs <carpeta de salida> [--root=<frontend>] [--json] [--budget-first-gzip=kB]')
  process.exit(2)
}
const root = path.resolve(opt('root') ?? path.join(here, '..'))
const outDir = path.resolve(outArg)
const realDist = path.resolve(here, '..', 'dist')
const same = (p) => (process.platform === 'win32' ? p.toLowerCase() : p) // Windows paths ignore case (DIST is dist)
for (const dist of [realDist, path.join(root, 'dist')]) {
  const [o, d] = [same(outDir), same(dist)]
  if (o === d || o.startsWith(d + path.sep) || d.startsWith(o + path.sep)) {
    console.error(`La carpeta de salida no puede ser ni contener ${dist}: usa una carpeta temporal (backend/.tmp/...).`)
    process.exit(2)
  }
}
// Vite empties the output folder before writing (emptyOutDir): only a new or empty folder, or the output of an earlier
// run (index.html, assets/, .vite/), is accepted, so a mistyped path (../backend, src, backend/.tmp) never loses files.
const BUILD_OUTPUT = new Set(['index.html', 'assets', '.vite'])
if (existsSync(outDir)) {
  const foreign = statSync(outDir).isDirectory() ? readdirSync(outDir).filter((f) => !BUILD_OUTPUT.has(f)) : [path.basename(outDir)]
  if (foreign.length) {
    const list = `${foreign.slice(0, 5).join(', ')}${foreign.length > 5 ? ', …' : ''}`
    console.error(`La carpeta de salida ${outDir} tiene otros archivos (${list}) y Vite la vaciaría: usa una carpeta nueva o vacía (backend/.tmp/...).`)
    process.exit(2)
  }
}

const modulesByChunk = new Map()
await build({
  root,
  configFile: path.join(root, 'vite.config.ts'),
  logLevel: 'warn',
  build: { outDir, emptyOutDir: true, manifest: true, reportCompressedSize: false },
  plugins: [
    {
      name: 'imp-fe-perf-modules',
      generateBundle(_options, bundle) {
        for (const c of Object.values(bundle)) {
          if (c.type === 'chunk') modulesByChunk.set(c.fileName, Object.entries(c.modules).map(([id, m]) => ({ id, size: m.renderedLength })))
        }
      },
    },
  ],
})

const manifestPath = path.join(outDir, '.vite', 'manifest.json')
if (!existsSync(manifestPath)) throw new Error(`No se generó ${manifestPath}`)
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))

const sizes = new Map()
function fileSize(file) {
  if (!sizes.has(file)) {
    const buf = readFileSync(path.join(outDir, file))
    sizes.set(file, { raw: buf.length, gzip: gzipSync(buf).length })
  }
  return sizes.get(file)
}
function closure(keys, acc = new Set()) {
  for (const k of keys) {
    if (acc.has(k)) continue
    acc.add(k)
    closure(manifest[k].imports ?? [], acc)
  }
  return acc
}
function total(files) {
  const list = [...new Set(files)]
  return list.reduce((t, f) => ({ files: t.files + 1, raw: t.raw + fileSize(f).raw, gzip: t.gzip + fileSize(f).gzip }), { files: 0, raw: 0, gzip: 0 })
}
const jsOf = (keys) => [...keys].map((k) => manifest[k].file)
const cssOf = (keys) => [...keys].flatMap((k) => manifest[k].css ?? [])

const entryKey = Object.keys(manifest).find((k) => manifest[k].isEntry)
const shell = closure([entryKey])
const pageKeys = Object.keys(manifest).filter((k) => /^src\/pages\/[^/]+\.tsx$/.test(k) && manifest[k].isDynamicEntry)
const pageName = (k) => k.replace(/^src\/pages\/|Page\.tsx$/g, '').toLowerCase()
const pages = {}
for (const k of pageKeys) {
  const own = [...closure([k])].filter((x) => !shell.has(x))
  pages[pageName(k)] = { js: total(jsOf(own)), css: total(cssOf(own)), chunks: jsOf(own) }
}
const firstPage = pages.chart ?? null
const allJs = Object.values(manifest).filter((m) => m.file.endsWith('.js')).map((m) => m.file)
const pkgOf = (id) => {
  const clean = id.replace(/^\0/, '').replace(/\\/g, '/').replace(/\?.*$/, '')
  const m = /\/node_modules\/((?:@[^/]+\/)?[^/]+)/.exec(clean)
  if (m) return m[1]
  const s = /\/src\/(.+)$/.exec(clean)
  return s ? `src/${s[1].split('/').slice(0, s[1].startsWith('components/') ? 2 : 1).join('/')}` : clean
}
const chunks = [...new Set(allJs)]
  .map((f) => {
    const byPkg = new Map()
    for (const { id, size } of modulesByChunk.get(f) ?? []) byPkg.set(pkgOf(id), (byPkg.get(pkgOf(id)) ?? 0) + size)
    return { file: f, ...fileSize(f), packages: [...byPkg].sort((a, b) => b[1] - a[1]).slice(0, Number(opt('top')) || 6) }
  })
  .sort((a, b) => b.raw - a.raw)

const report = {
  shell: { js: total(jsOf(shell)), css: total(cssOf(shell)), chunks: jsOf(shell) },
  firstPage: firstPage && { page: 'chart', js: firstPage.js, css: firstPage.css },
  firstVisitChart: firstPage && {
    raw: total(jsOf(shell)).raw + firstPage.js.raw,
    gzip: total(jsOf(shell)).gzip + firstPage.js.gzip,
  },
  pages,
  allJs: total(allJs),
  chunks,
}

const kB = (n) => `${(n / 1000).toFixed(2)} kB`
if (args.includes('--json')) {
  console.log(JSON.stringify(report, null, 2))
} else {
  const line = (t) => `${t.files} archivo(s), ${kB(t.raw)} (gzip ${kB(t.gzip)})`
  console.log(`\nPrimera carga (index.html: entrada + modulepreload): JS ${line(report.shell.js)}; CSS ${line(report.shell.css)}`)
  if (firstPage) {
    console.log(`+ sección inicial #/chart: JS ${line(firstPage.js)}`)
    console.log(`= primera visita a #/chart: ${kB(report.firstVisitChart.raw)} (gzip ${kB(report.firstVisitChart.gzip)})`)
    console.log('\nAl abrir cada sección por primera vez (además de la primera carga; los fragmentos compartidos se cuentan en cada una):')
    for (const [name, p] of Object.entries(pages)) console.log(`  ${name.padEnd(10)} JS ${line(p.js)}${p.css.files ? `; CSS ${line(p.css)}` : ''}`)
  } else {
    console.log('Las secciones no se cargan bajo demanda: todo va en la primera carga.')
  }
  console.log(`\nTodo el JS: ${line(report.allJs)}\nFragmentos (paquetes: tamaño aprox. antes de minificar):`)
  for (const c of chunks) {
    console.log(`  ${c.file.padEnd(44)} ${kB(c.raw).padStart(10)}  gzip ${kB(c.gzip).padStart(9)}  ${c.packages.map(([p, s]) => `${p} ${Math.round(s / 1000)}k`).join(', ')}`)
  }
}

const budget = Number(opt('budget-first-gzip'))
if (Number.isFinite(budget) && budget > 0) {
  const used = (report.firstVisitChart?.gzip ?? report.shell.js.gzip) / 1000
  if (used > budget) {
    console.error(`\nLa primera visita a #/chart descarga ${used.toFixed(2)} kB gzip de JS: supera el límite de ${budget} kB.`)
    process.exit(1)
  }
}
