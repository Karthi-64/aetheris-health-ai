import { AxiosError, type AxiosInstance, type AxiosRequestConfig } from 'axios'
import { ApiError, type ApiResponse, type Paginated } from './types'

/**
 * Envelope handling, copied from the Hospital app's `frontend/src/api/http.ts`
 * and turned into a factory: this package owns no Axios instance, no token and
 * no auth store, so each app passes in the instance it built for its own
 * principal.
 */

/** Turn whatever a failed request threw into a typed {@link ApiError}. */
export function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  if (err instanceof AxiosError) {
    const body = err.response?.data as ApiResponse<unknown> | undefined
    return new ApiError(
      body?.message ?? err.message,
      body?.error_code ?? 'network_error',
      err.response?.status,
      body?.errors,
    )
  }
  return new ApiError(err instanceof Error ? err.message : 'Unexpected error')
}

/** The typed wrappers an app calls; they unwrap `{ success, data }`. */
export interface HttpClient {
  get: <T>(url: string, config?: AxiosRequestConfig) => Promise<T>
  getPaginated: <T>(url: string, config?: AxiosRequestConfig) => Promise<Paginated<T>>
  post: <T>(url: string, body?: unknown, config?: AxiosRequestConfig) => Promise<T>
  patch: <T>(url: string, body?: unknown, config?: AxiosRequestConfig) => Promise<T>
  put: <T>(url: string, body?: unknown, config?: AxiosRequestConfig) => Promise<T>
  delete: <T>(url: string, config?: AxiosRequestConfig) => Promise<T>
}

/**
 * Build the typed wrappers around one Axios instance. They unwrap the envelope
 * and throw a typed {@link ApiError} on failure — components and hooks only
 * ever see domain data.
 */
export function createHttpClient(api: AxiosInstance): HttpClient {
  async function request<T>(config: AxiosRequestConfig): Promise<T> {
    try {
      const res = await api.request<ApiResponse<T> | undefined>(config)
      // A 204 has no envelope at all; its `data` is whatever the caller typed
      // as "nothing".
      return res.data?.data as T
    } catch (err) {
      throw toApiError(err)
    }
  }

  /** GET that returns items + pagination together, reading `metadata.pagination`. */
  async function getPaginated<T>(url: string, config?: AxiosRequestConfig): Promise<Paginated<T>> {
    try {
      const res = await api.get<ApiResponse<T[]>>(url, config)
      const wire = res.data.metadata?.pagination
      if (!wire) throw new ApiError('Response is missing pagination metadata', 'bad_response')
      return {
        items: res.data.data,
        pagination: {
          page: wire.page,
          pageSize: wire.page_size,
          total: wire.total_records,
          totalPages: wire.total_pages,
        },
      }
    } catch (err) {
      throw toApiError(err)
    }
  }

  return {
    get: (url, config) => request({ ...config, url, method: 'get' }),
    getPaginated,
    post: (url, body, config) => request({ ...config, url, method: 'post', data: body }),
    patch: (url, body, config) => request({ ...config, url, method: 'patch', data: body }),
    put: (url, body, config) => request({ ...config, url, method: 'put', data: body }),
    delete: (url, config) => request({ ...config, url, method: 'delete' }),
  }
}
