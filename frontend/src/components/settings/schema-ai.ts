import { Sparkles } from 'lucide-react'
import type { GroupDef } from './schema'

/** Settings group of the local AI copilot (appended to GROUPS by schema.ts). Defaults: backend/app/settings_ext/ai.py. */
export const GROUPS_EXT: GroupDef[] = [
  {
    id: 'ai',
    title: 'Copiloto IA (Ollama)',
    icon: Sparkles,
    description:
      'Una IA en tu propio PC (Ollama) que entiende los datos de NEXUS con herramientas de solo lectura: explica qué hace NEXUS automática, resume su diario, mira un mercado o las noticias y, si añades una clave, busca en internet. No cambia ajustes ni opera, y puede equivocarse. Instala Ollama (ollama.com) y descarga un modelo, p. ej. «ollama pull qwen3:8b».',
    fields: [
      {
        key: 'ai.enabled',
        label: 'Activar el copiloto',
        kind: 'bool',
        help: 'Abre la sección Copiloto y el comando «ia» de la terminal. Mientras está desactivado no se llama a Ollama.',
      },
      {
        key: 'ai.ollama_url',
        label: 'Dirección de Ollama',
        kind: 'text',
        placeholder: 'http://127.0.0.1:11434',
        inputMode: 'url',
        link: { href: 'https://ollama.com', label: 'Descargar Ollama' },
        help: 'Por defecto, este mismo PC. Si Ollama corre en otro equipo de tu red, pon su dirección (http://IP:11434).',
      },
      {
        key: 'ai.model',
        label: 'Modelo',
        kind: 'text',
        placeholder: 'qwen3:8b',
        help: 'Vacío = el primero que tenga Ollama. Con una GPU de 12 GB caben modelos de hasta unos 14B (p. ej. qwen3:14b).',
      },
      {
        key: 'ai.search_provider',
        label: 'Búsqueda en internet',
        kind: 'select',
        options: [
          { value: 'none', label: 'Sin búsqueda' },
          { value: 'brave', label: 'Brave Search API (con tu clave)' },
        ],
        help: 'Solo con la API oficial de un buscador y tu clave. Lo que encuentra se trata como información, nunca como instrucciones.',
      },
      {
        key: 'ai.brave_api_key',
        label: 'Clave de Brave Search API',
        kind: 'secret',
        link: { href: 'https://brave.com/search/api/', label: 'Obtener una clave' },
        help: 'Se guarda en tu base de datos y solo se envía a la API de Brave Search.',
      },
      {
        key: 'ai.timeout_s',
        label: 'Tiempo máximo de respuesta',
        kind: 'seconds',
        min: 10,
        max: 600,
        step: 10,
        help: 'Un modelo grande en CPU puede tardar minutos; si se corta, súbelo o usa un modelo más pequeño.',
      },
      {
        key: 'ai.context_length',
        label: 'Memoria de la conversación',
        kind: 'int',
        min: 2048,
        max: 131072,
        step: 1024,
        unit: 'tokens',
        help: 'Lo que Ollama reserva para la conversación (num_ctx). 16 384 basta para el laboratorio de estrategias; súbelo si pierde el hilo, bájalo si el modelo no cabe en tu GPU.',
      },
    ],
  },
]
