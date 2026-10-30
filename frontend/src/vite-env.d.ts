/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend URL in cloud mode (e.g. https://nexus.up.railway.app). Empty = same origin (local mode). */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
