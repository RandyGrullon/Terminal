// CodeMirror 6 editor for PyChart scripts: Python highlighting, One Dark theme, PyChart autocompletion and
// lint diagnostics (gutter markers + underlines) fed from compile / run errors.
import { forwardRef, memo, useEffect, useImperativeHandle, useState } from 'react'
import CodeMirror, { EditorView, Prec, type BasicSetupOptions } from '@uiw/react-codemirror'
import { python, pythonLanguage } from '@codemirror/lang-python'
import { oneDark } from '@codemirror/theme-one-dark'
import { ifNotIn } from '@codemirror/autocomplete'
import { lintGutter, linter, setDiagnostics } from '@codemirror/lint'
import { firstNonSpace, toDiagnostics, type Problem } from './diagnostics'
import { pychartCompletionSource } from './reference'

export interface CodeEditorHandle {
  /** Moves the cursor to a 1-based line (and optional 1-based column) and focuses the editor. */
  gotoLine: (line: number, col?: number | null) => void
  /** Replaces the selection with `text` and focuses the editor. */
  insert: (text: string) => void
  focus: () => void
}

const studioTheme = EditorView.theme(
  {
    '&': { height: '100%', fontSize: '13px', backgroundColor: 'var(--bg)' },
    '.cm-scroller': { fontFamily: 'var(--mono)', lineHeight: '1.55' },
    '.cm-content': { paddingBottom: '40px' },
    '.cm-gutters': { backgroundColor: 'var(--panel)', borderRight: '1px solid var(--border)', color: '#5c6577' },
    '.cm-activeLineGutter': { backgroundColor: 'rgba(47, 107, 255, 0.12)' },
    '.cm-activeLine': { backgroundColor: 'rgba(47, 107, 255, 0.06)' },
    '.cm-tooltip': { border: '1px solid var(--border-2)', backgroundColor: 'var(--panel-2)' },
    '.cm-tooltip-autocomplete > ul': { fontFamily: 'var(--mono)', fontSize: '12px', maxHeight: '18em' },
    '.cm-completionInfo': { whiteSpace: 'pre-wrap', maxWidth: '380px', fontFamily: 'var(--font)', fontSize: '12px', lineHeight: '1.45' },
    '.cm-completionDetail': { color: 'var(--muted)', fontStyle: 'normal', marginLeft: '8px' },
    '.cm-completionSection': { color: 'var(--accent-2)', fontSize: '11px', fontFamily: 'var(--font)', paddingTop: '4px' },
    '.cm-diagnostic': { fontFamily: 'var(--font)', fontSize: '12px' },
    '.cm-lintRange-error': {
      backgroundImage: 'none',
      textDecoration: 'underline wavy rgba(234, 57, 67, 0.95)',
      textUnderlineOffset: '3px',
    },
  },
  { dark: true },
)

// Module-level constants: react-codemirror reconfigures the editor whenever these identities change.
const EXTENSIONS = [
  python(),
  pythonLanguage.data.of({ autocomplete: ifNotIn(['Comment', 'String', 'FormatString'], pychartCompletionSource) }),
  linter(null), // keeps the lint state in the base config so diagnostics survive reconfiguration
  lintGutter(),
  Prec.highest(studioTheme),
]

const BASIC_SETUP: BasicSetupOptions = {
  tabSize: 4,
  foldGutter: true,
  highlightActiveLine: true,
  highlightActiveLineGutter: true,
  autocompletion: true,
  completionKeymap: true,
  lintKeymap: true,
  searchKeymap: true,
  closeBrackets: true,
  bracketMatching: true,
}

interface Props {
  value: string
  onChange: (value: string) => void
  problems: readonly Problem[]
  readOnly?: boolean
}

const CodeEditor = forwardRef<CodeEditorHandle, Props>(function CodeEditor({ value, onChange, problems, readOnly }, ref) {
  const [view, setView] = useState<EditorView | null>(null)

  useImperativeHandle(
    ref,
    () => ({
      gotoLine(lineNumber, col) {
        if (!view) return
        const doc = view.state.doc
        const n = Math.min(Math.max(1, Math.round(lineNumber) || 1), doc.lines)
        const line = doc.line(n)
        const pos =
          typeof col === 'number' && Number.isFinite(col) && col > 0
            ? Math.min(line.to, line.from + Math.round(col) - 1)
            : line.from + firstNonSpace(line.text)
        view.dispatch({ selection: { anchor: pos }, effects: EditorView.scrollIntoView(pos, { y: 'center' }) })
        view.focus()
      },
      insert(text) {
        if (!view) return
        const { from, to } = view.state.selection.main
        view.dispatch({ changes: { from, to, insert: text }, selection: { anchor: from + text.length }, scrollIntoView: true })
        view.focus()
      },
      focus() {
        view?.focus()
      },
    }),
    [view],
  )

  useEffect(() => {
    if (!view) return
    try {
      view.dispatch(setDiagnostics(view.state, toDiagnostics(view.state.doc, problems)))
    } catch {
      /* view destroyed between render and effect */
    }
  }, [view, problems])

  return (
    <div className="studio-editor">
      <CodeMirror
        className="studio-cm"
        value={value}
        onChange={onChange}
        extensions={EXTENSIONS}
        theme={oneDark}
        height="100%"
        basicSetup={BASIC_SETUP}
        readOnly={readOnly}
        onCreateEditor={(v) => setView(v)}
      />
    </div>
  )
})

export default memo(CodeEditor)
