// Maps PyChart compile / run errors (1-based user line numbers) onto CodeMirror lint diagnostics.
import type { Text } from '@uiw/react-codemirror'
import type { Diagnostic } from '@codemirror/lint'
import type { ScriptError } from '../../api/types'

export interface Problem extends ScriptError {
  origin: 'compile' | 'run'
}

export const ERROR_KIND_LABEL: Record<ScriptError['kind'], string> = {
  syntax: 'Sintaxis',
  security: 'Seguridad',
  runtime: 'Ejecución',
  timeout: 'Tiempo agotado',
}

export function firstNonSpace(text: string): number {
  const m = /\S/.exec(text)
  return m ? m.index : 0
}

/** "Sintaxis", "Ejecución", "Seguridad · al ejecutar"… (the origin is only added when it is not obvious). */
export function problemLabel(p: Problem): string {
  const base = ERROR_KIND_LABEL[p.kind] ?? 'Error'
  return p.origin === 'run' && p.kind !== 'runtime' && p.kind !== 'timeout' ? `${base} · al ejecutar` : base
}

/** Errors without a line are skipped (they are still listed in the "Problemas" tab). */
export function toDiagnostics(doc: Text, problems: readonly Problem[]): Diagnostic[] {
  const out: Diagnostic[] = []
  for (const p of problems) {
    if (typeof p.line !== 'number' || !Number.isFinite(p.line) || doc.lines < 1) continue
    const lineNo = Math.min(Math.max(1, Math.round(p.line)), doc.lines)
    const line = doc.line(lineNo)
    let from = line.from + firstNonSpace(line.text)
    if (typeof p.col === 'number' && Number.isFinite(p.col) && p.col > 0) from = Math.min(line.to, line.from + Math.round(p.col) - 1)
    const to = Math.max(from, line.to)
    out.push({
      from,
      to,
      severity: 'error',
      source: problemLabel(p),
      message: p.message,
    })
  }
  return out
}
