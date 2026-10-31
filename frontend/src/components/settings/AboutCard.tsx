// About NEXUS Terminal: what it is, keyboard shortcuts, data sources and the disclaimer.
import { memo } from 'react'
import { Info, Keyboard } from 'lucide-react'
import { Card, Disclaimer } from '../ui'

const SHORTCUTS: [string, string][] = [
  ['Ctrl + `', 'Mostrar / ocultar el terminal'],
  ['Ctrl + Enter', 'Ejecutar el script (PyChart)'],
  ['Ctrl + Shift + B', 'Compilar el script (PyChart)'],
  ['Ctrl + S', 'Guardar el script (PyChart)'],
  ['Ctrl + Espacio', 'Autocompletar en el editor'],
]

function AboutCard({ version }: { version: string | null }) {
  return (
    <Card
      title={
        <>
          <Info size={14} /> Acerca de
        </>
      }
      sub={version ? `NEXUS Terminal v${version}` : 'NEXUS Terminal'}
    >
      <div className="col gap-3">
        <p className="small text-2 set-p">
          Terminal de investigación local: gráficos de cualquier activo de TradingView, el motor autoadaptativo NEXUS con niveles «hasta
          aquí» y salidas anticipadas, y el filtro <strong>«¿Tomo la operación?»</strong>, que te dice cuándo NO entrar y cuándo sí. Todo se
          guarda en tu propia base de datos: SQLite en tu equipo o, en modo nube, tu Postgres de Supabase.
        </p>
        <div className="col gap-1">
          <h4 className="row gap-1">
            <Keyboard size={13} /> Atajos de teclado
          </h4>
          <div className="set-shortcuts">
            {SHORTCUTS.map(([k, d]) => (
              <div key={k} className="row gap-2">
                <kbd className="set-kbd">{k}</kbd>
                <span className="small text-2">{d}</span>
              </div>
            ))}
          </div>
        </div>
        <p className="tiny muted set-p">
          Datos: endpoints públicos y oficiales (TradingView, APIs públicas de exchanges cripto y, si añades una clave, Twelve Data, Alpha
          Vantage o Polygon). Sin suplantar navegadores ni saltarse protecciones. Las operaciones son siempre simuladas.
        </p>
        <Disclaimer />
      </div>
    </Card>
  )
}

export default memo(AboutCard)
