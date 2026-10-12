import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { api } from '@/lib/api'

/**
 * A stand-in for the network, for tests that need the real hooks, `http`
 * wrapper and Axios instance to run. Only the adapter is replaced, so a test
 * can assert on the request that would leave the browser — method, URL, query,
 * body and headers — and decide what the "server" answers.
 */

export interface Outcome {
  status: number
  data: unknown
  /** Response headers, for the few endpoints whose answer is a file. */
  headers?: Record<string, string>
}

export type Handler = (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>

/** A success envelope (`docs/06-API_STANDARDS.md` §5). */
export const ok = (data: unknown, status = 200): Outcome => ({
  status,
  data: { success: true, message: 'ok', data },
})

/** A paginated success envelope carrying every item on one page. */
export const paged = (items: unknown[], totalRecords = items.length): Outcome => ({
  status: 200,
  data: {
    success: true,
    message: 'ok',
    data: items,
    metadata: {
      pagination: { page: 1, page_size: 25, total_records: totalRecords, total_pages: 1 },
    },
  },
})

/** A failure envelope. `extra` carries `error_code` and `errors`. */
export const fail = (status: number, message: string, extra: Record<string, unknown> = {}): Outcome => ({
  status,
  data: { success: false, message, ...extra },
})

export interface FakeApi {
  /** Every request made, in order. */
  sent: InternalAxiosRequestConfig[]
  /** Requests with the given method, optionally narrowed to URLs containing `urlPart`. */
  requests: (method: string, urlPart?: string) => InternalAxiosRequestConfig[]
  restore: () => void
}

/** Route every request made through the app's Axios instance to `handler`. */
export function installFakeApi(handler: Handler): FakeApi {
  const original = api.defaults.adapter
  const sent: InternalAxiosRequestConfig[] = []
  const adapter: AxiosAdapter = async (config) => {
    sent.push(config)
    const outcome = await handler(config)
    const response = { ...outcome, statusText: '', headers: outcome.headers ?? {}, config }
    if (outcome.status >= 400) {
      throw new AxiosError('Request failed', 'ERR_BAD_REQUEST', config, null, response)
    }
    return response
  }
  api.defaults.adapter = adapter
  return {
    sent,
    requests: (method, urlPart) =>
      sent.filter((c) => c.method === method && (!urlPart || (c.url ?? '').includes(urlPart))),
    restore: () => {
      api.defaults.adapter = original
    },
  }
}

/** The JSON body of a request, parsed. */
export const bodyOf = (config: InternalAxiosRequestConfig): unknown =>
  typeof config.data === 'string' ? JSON.parse(config.data) : config.data
