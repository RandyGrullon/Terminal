// Error boundary for the market-conditions widgets: they render in the app header / chart toolbar, outside the
// page boundaries, so a rendering error must degrade to a fallback instead of taking the whole shell down.
// After a failure it tries the real content again every minute (data may have been fixed meanwhile).
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  fallback: ReactNode
  children: ReactNode
}

interface State {
  failed: boolean
}

const RETRY_MS = 60_000

export class MacroBoundary extends Component<Props, State> {
  state: State = { failed: false }
  private timer: number | null = null

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Semáforo de mercado: error al mostrar el componente', error, info.componentStack)
    if (this.timer !== null) window.clearTimeout(this.timer)
    this.timer = window.setTimeout(() => {
      this.timer = null
      this.setState({ failed: false })
    }, RETRY_MS)
  }

  componentWillUnmount(): void {
    if (this.timer !== null) window.clearTimeout(this.timer)
  }

  render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children
  }
}
