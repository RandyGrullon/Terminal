/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend URL in cloud mode (e.g. https://nexus.up.railway.app). Empty = same origin (local mode). */
  readonly VITE_API_BASE?: string
  /** Fixed link with a moving backend: the Supabase project URL where the backend publishes its address. */
  readonly VITE_NEXUS_DISCOVERY_URL?: string
  /** Its publishable key (public by design). */
  readonly VITE_NEXUS_DISCOVERY_KEY?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
