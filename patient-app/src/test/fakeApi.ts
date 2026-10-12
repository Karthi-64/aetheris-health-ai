import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { api } from '@/api/client'

/**
 * A stand-in for the network, for tests that need the real pages, hooks,
 * envelope wrapper and Axios instance (interceptors included) to run. Only the
 * adapter is replaced, so a test can assert on the request that would leave
 * the browser — method, URL, body, headers, credentials — and decide what the
 * "server" answers. There is no mock-auth path in the app to lean on instead.
 *
 * Pattern copied from the Hospital app's `frontend/src/test/fakeApi.ts`.
 */

export interface Outcome {
  status: number
  data: unknown
  headers?: Record<string, string>
}

export type Handler = (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>

/** A success envelope (`docs/06-API_STANDARDS.md` §5). */
export const ok = (data: unknown, status = 200): Outcome => ({
  status,
  data: { success: true, message: 'ok', data, metadata: { request_id: 'req-test' } },
})

/** A page of a list, as `paginated_envelope` in `backend/app/core/envelope.py` builds it. */
export const okPage = (items: unknown[], page = 1, pageSize = 20, total = items.length): Outcome => ({
  status: 200,
  data: {
    success: true,
    message: 'ok',
    data: items,
    metadata: {
      request_id: 'req-test',
      pagination: { page, page_size: pageSize, total_records: total, total_pages: Math.ceil(total / pageSize) },
    },
  },
})

/**
 * An answer the test gives later, to look at what is on screen in the
 * meantime. It is safe to give before the request has even been made.
 */
export function deferred(): { handler: Handler; answer: (outcome: Outcome) => void } {
  let answer: (outcome: Outcome) => void = () => {}
  const outcome = new Promise<Outcome>((resolve) => (answer = resolve))
  return { handler: () => outcome, answer }
}

/** A request that never gets an answer: no network, or a server that cannot be reached. */
export const unreachable: Handler = (config) => {
  throw new AxiosError('Network Error', 'ERR_NETWORK', config)
}

/** A 204: no envelope, no body. */
export const noContent = (): Outcome => ({ status: 204, data: '' })

/** A failure envelope, as `backend/app/core/envelope.py` builds it. */
export const fail = (status: number, errorCode: string, message = 'Request failed.', headers?: Record<string, string>): Outcome => ({
  status,
  data: { success: false, message, errors: null, error_code: errorCode, metadata: { request_id: 'req-test' } },
  headers,
})

/** Answers keyed by `"METHOD /path"`, e.g. `"POST /auth/refresh"`. */
export type Routes = Record<string, Outcome | Handler>

export interface FakeApi {
  /** Every request made, in order. */
  sent: InternalAxiosRequestConfig[]
  /** Requests to one route, e.g. `calls('POST /auth/refresh')`. */
  calls: (route: string) => InternalAxiosRequestConfig[]
  /** Requests no route answered. The suite fails if any is left here. */
  unexpected: string[]
  /** Change or add answers mid-test. */
  on: (routes: Routes) => void
}

const routeOf = (config: InternalAxiosRequestConfig) => `${(config.method ?? 'get').toUpperCase()} ${config.url ?? ''}`

let installed: { fake: FakeApi; restore: () => void } | null = null

/** Route every request made through the app's Axios instance to `routes`. */
export function serve(routes: Routes): FakeApi {
  uninstallFakeApi()
  const table: Routes = { ...routes }
  const original = api.defaults.adapter
  const fake: FakeApi = {
    sent: [],
    unexpected: [],
    calls: (route) => fake.sent.filter((config) => routeOf(config) === route),
    on: (more) => Object.assign(table, more),
  }
  const adapter: AxiosAdapter = async (config) => {
    fake.sent.push(config)
    const answer = table[routeOf(config)]
    if (answer === undefined) {
      fake.unexpected.push(routeOf(config))
      throw new AxiosError(`No fake route for ${routeOf(config)}`, 'ERR_NETWORK', config)
    }
    const outcome = typeof answer === 'function' ? await answer(config) : answer
    const response = { ...outcome, statusText: '', headers: outcome.headers ?? {}, config }
    if (outcome.status >= 400) {
      throw new AxiosError('Request failed', 'ERR_BAD_REQUEST', config, null, response)
    }
    return response
  }
  api.defaults.adapter = adapter
  installed = { fake, restore: () => (api.defaults.adapter = original) }
  return fake
}

/** Put the real adapter back; returns the requests the last fake could not answer. */
export function uninstallFakeApi(): string[] {
  const unexpected = installed?.fake.unexpected ?? []
  installed?.restore()
  installed = null
  return unexpected
}

/** The JSON body of a request, parsed. */
export const bodyOf = (config: InternalAxiosRequestConfig): unknown =>
  typeof config.data === 'string' ? JSON.parse(config.data) : config.data

/** One request header, or `undefined` when it was not sent. */
export const headerOf = (config: InternalAxiosRequestConfig, name: string): string | undefined => {
  const value = config.headers.get(name)
  return value === undefined || value === null || value === false ? undefined : String(value)
}
