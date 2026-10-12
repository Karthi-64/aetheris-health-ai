/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API base. Defaults to `/api/v1/patient` (same origin, proxied in dev). */
  readonly VITE_API_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
