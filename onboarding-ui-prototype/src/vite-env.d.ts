/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  /** Development identity sent as X-* headers to a backend started with --dev-identity. Not secrets. */
  readonly VITE_DEV_USER_ID?: string
  readonly VITE_DEV_CLIENT_ID?: string
  readonly VITE_DEV_USER_ROLES?: string
  readonly VITE_DEV_IDENTITIES?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
