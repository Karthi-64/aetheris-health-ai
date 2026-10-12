import axios, { type InternalAxiosRequestConfig } from 'axios'
import { createHttpClient, type ApiResponse } from '@atheris/api-core'
import { usePatientAuthStore } from '@/store/patient-auth-store'

/**
 * The Patient App's Axios instance. Every patient route is mounted under
 * `/api/v1/patient`; in dev the Vite proxy makes it same-origin.
 *
 * - The access token is attached from memory to every call outside `/auth/`.
 * - The refresh token is an HttpOnly cookie scoped to `/auth/`: this code can
 *   neither read it nor send it anywhere else.
 */
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? '/api/v1/patient',
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
})

/** Typed wrappers that unwrap the envelope and throw `ApiError`. */
export const http = createHttpClient(api)

/**
 * The cookie-authenticated endpoints (refresh, logout) require this header.
 * A cross-site form or image cannot set it, which is the CSRF defence.
 */
export const CSRF_HEADER = { 'X-Atheris-Patient': '1' } as const

/** `/auth/*` is public (code request, code check) or cookie-authenticated. */
const isAuthCall = (url: string | undefined) => (url ?? '').startsWith('/auth/')

const bearer = (token: string) => `Bearer ${token}`

api.interceptors.request.use((config) => {
  const token = usePatientAuthStore.getState().accessToken
  if (token && !isAuthCall(config.url)) config.headers.Authorization = bearer(token)
  return config
})

/**
 * What a refresh attempt established. `sessionEnded` is true only when the
 * server refused the cookie; a refresh that could not be reached leaves the
 * session as it was.
 */
export interface RefreshOutcome {
  token: string | null
  sessionEnded: boolean
}

async function requestRefresh(): Promise<RefreshOutcome> {
  const { accessToken: before, setAccessToken, endSession } = usePatientAuthStore.getState()
  try {
    const { data } = await api.post<ApiResponse<{ access_token?: unknown }>>('/auth/refresh', undefined, {
      headers: CSRF_HEADER,
      timeout: REFRESH_REQUEST_TIMEOUT_MS,
    })
    const token = data?.data?.access_token
    if (typeof token === 'string' && token !== '') {
      setAccessToken(token)
      return { token, sessionEnded: false }
    }
  } catch (err) {
    // A network failure, a 5xx or a 429 says nothing about the session, so it
    // is kept and the next request tries the refresh again.
    const status = axios.isAxiosError(err) ? err.response?.status : undefined
    if (status !== 401 && status !== 403) return { token: null, sessionEnded: false }
  }
  // Refused, or an answer without a token: fail closed. Only a session that
  // existed is reported as ended — a visitor who was never signed in is not.
  if (before !== null) endSession('session_ended')
  return { token: null, sessionEnded: true }
}

/**
 * One name for every tab of this origin. The Web Locks API hands the lock to
 * one holder at a time and releases it when the holder's callback settles, or
 * when its tab goes away.
 */
export const REFRESH_LOCK = 'atheris-patient-refresh'

/** How long the refresh request itself may take, so a lock is never held for ever. */
const REFRESH_REQUEST_TIMEOUT_MS = 10_000

/**
 * How long a tab waits for the others' refreshes before giving up. It gives
 * up WITHOUT refreshing: an unguarded request is exactly the race the lock
 * exists to prevent, so the session is left as it was and tried again later.
 */
export const REFRESH_LOCK_WAIT_MS = 30_000

const NOT_REFRESHED: RefreshOutcome = { token: null, sessionEnded: false }

/** `navigator.locks`, where the browser has it (secure contexts only). */
function lockManager(): LockManager | undefined {
  const nav = (globalThis as { navigator?: { locks?: LockManager } }).navigator
  return typeof nav?.locks?.request === 'function' ? nav.locks : undefined
}

/**
 * One refresh at a time ACROSS TABS. Every tab shares one refresh cookie, and
 * the server rotates it on each use and treats a second use of the old one as
 * theft, signing the patient out everywhere. Two tabs restoring together would
 * do exactly that, so each refresh runs inside an exclusive lock: the next tab
 * sends its own request only after the previous answer — and its rotated
 * cookie — has arrived, and so presents the new cookie, not the spent one.
 *
 * Nothing is shared between tabs but the lock: no token, no message. Each tab
 * gets its own access token from its own request, and learns about its own
 * session only from its own answer.
 */
async function refreshUnderLock(): Promise<RefreshOutcome> {
  const locks = lockManager()
  // No Web Locks: the tab can still keep its own callers to one request.
  if (!locks) return requestRefresh()

  const waiting = new AbortController()
  const giveUp = setTimeout(() => waiting.abort(), REFRESH_LOCK_WAIT_MS)
  let entered = false
  try {
    return await locks.request(REFRESH_LOCK, { signal: waiting.signal }, () => {
      entered = true
      clearTimeout(giveUp)
      return requestRefresh()
    })
  } catch {
    // Timed out behind another tab: do not refresh alongside it.
    if (entered || waiting.signal.aborted) return NOT_REFRESHED
    // The browser refused the lock itself; behave as if it had none.
    return requestRefresh()
  } finally {
    clearTimeout(giveUp)
  }
}

let refreshing: Promise<RefreshOutcome> | null = null

/**
 * Exchange the refresh cookie for a new access token — at most one request at
 * a time, in this tab and across tabs. Callers in this tab (several 401s at
 * once, a double-invoked effect) share one request; tabs take turns through
 * {@link refreshUnderLock}.
 */
export function refreshSession(): Promise<RefreshOutcome> {
  refreshing ??= refreshUnderLock().finally(() => {
    refreshing = null
  })
  return refreshing
}

type RetriableConfig = InternalAxiosRequestConfig & { _retry?: boolean }

// On 401: refresh once, retry the original request once. The session ends only
// when the refresh itself is refused (handled in `requestRefresh`).
api.interceptors.response.use(
  (response) => response,
  async (error: unknown) => {
    if (!axios.isAxiosError(error)) return Promise.reject(error)
    const original = error.config as RetriableConfig | undefined
    const sentWith = original?.headers?.Authorization
    if (
      error.response?.status !== 401 ||
      !original ||
      original._retry ||
      isAuthCall(original.url) ||
      typeof sentWith !== 'string'
    ) {
      return Promise.reject(error)
    }
    original._retry = true

    // If another request already refreshed while this one was in flight, the
    // token in memory is newer than the one that was refused: just retry.
    const current = usePatientAuthStore.getState().accessToken
    const token = current !== null && bearer(current) !== sentWith ? current : (await refreshSession()).token
    if (!token) return Promise.reject(error)

    original.headers.Authorization = bearer(token)
    return api(original)
  },
)
