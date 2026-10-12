/**
 * @atheris/api-core — the principal-agnostic API contract code, seeded by
 * copying from the Hospital app (docs/modules/15-patient-app.md §25.3):
 * envelope types, `ApiError`, error helpers, the idempotency key and an
 * http-client factory. No auth store, no token store, no Axios singleton.
 */
export {
  ApiError,
  type ApiResponse,
  type ListQueryOptions,
  type Paginated,
  type PaginationMeta,
  type WirePaginationMeta,
} from './types'
export {
  apiErrorMessage,
  fieldErrorsOf,
  hasPath,
  splitFieldErrors,
  type FieldError,
} from './apiErrors'
export { newIdempotencyKey } from './idempotency'
export { createHttpClient, toApiError, type HttpClient } from './http'
