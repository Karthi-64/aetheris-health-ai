/**
 * Standard API envelope — every backend response follows this shape
 * (`docs/06-API_STANDARDS.md` §5). The failure fields are flat: the backend
 * emits `message` / `error_code` / `errors` at the top level, not a nested
 * `error` object.
 */
export interface ApiResponse<T> {
  success: boolean
  message: string
  data: T
  metadata?: { request_id?: string | null; pagination?: WirePaginationMeta }
  /** Field-level detail on failure. Shape varies by error code. */
  errors?: unknown
  error_code?: string
}

/** Pagination as the backend sends it, in wire (snake_case) form. */
export interface WirePaginationMeta {
  page: number
  page_size: number
  total_records: number
  total_pages: number
}

/** Pagination as the app consumes it. Mapped from the wire shape in `http.ts`. */
export interface PaginationMeta {
  page: number
  pageSize: number
  total: number
  totalPages: number
}

/** A list payload plus its pagination metadata, unwrapped for consumers. */
export interface Paginated<T> {
  items: T[]
  pagination: PaginationMeta
}

/** Thrown by the HTTP layer on a non-success response. Carries the backend code. */
export class ApiError extends Error {
  readonly code: string
  readonly status?: number
  readonly details?: unknown

  constructor(message: string, code = 'unknown', status?: number, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.details = details
  }
}

/** `enabled: false` skips the request — for callers whose user may not read this. */
export interface ListQueryOptions {
  enabled?: boolean
}
