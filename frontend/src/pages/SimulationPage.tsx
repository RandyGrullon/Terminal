// Simulation page: live market simulator (synthetic / historical replay), Backtest and Monte Carlo.
import type { ReactNode } from 'react'
import { ChartLine, Dices, FlaskConical } from 'lucide-react'
import { Tabs } from '../components/ui'
import { BacktestView } from '../components/sim/BacktestView'
import { LiveSimulator } from '../components/sim/LiveSimulator'
import { MonteCarloView } from '../components/sim/MonteCarloView'
import { useSimStore, type SimTab } from '../components/sim/simStore'
import '../components/sim/sim.css'

const TABS: { id: SimTab; label: ReactNode }[] = [
  {
    id: 'live',
    label: (
      <>
        <FlaskConical size={14} /> Mercado simulado
      </>
    ),
  },
  {
    id: 'backtest',
    label: (
      <>
        <ChartLine size={14} /> Backtest
      </>
    ),
  },
  {
    id: 'montecarlo',
    label: (
      <>
        <Dices size={14} /> Monte Carlo
      </>
    ),
  },
]

export default function SimulationPage() {
  const tab = useSimStore((s) => s.tab)
  const setTab = useSimStore((s) => s.setTab)

  return (
    <div className="page-full sim-page">
      <div className="sim-tabbar">
        <Tabs<SimTab> tabs={TABS} value={tab} onChange={setTab} />
        <span className="sim-tabbar-note">Todo es simulado (papel): nunca se envían órdenes reales.</span>
      </div>
      {tab === 'live' ? (
        <LiveSimulator />
      ) : (
        <div className="sim-scroll">{tab === 'backtest' ? <BacktestView /> : <MonteCarloView />}</div>
      )}
    </div>
  )
}
