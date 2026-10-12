import axios, { type InternalAxiosRequestConfig } from 'axios'
import { tokenStore } from '@/services/tokenStore'
import { useAuthStore } from '@/store/auth-store'

/**
 * Central Axios instance.
 *
 * The access token is attached from the in-memory `tokenStore` on every request.
 * The refresh token is sent in the request body (the backend contract uses
 * `{ refresh_token }` in the body, not an HTTP-only cookie).
 *
 * Every backend route is mounted under `/api/v1` (`docs/06-API_STANDARDS.md`).
 * In dev the Vite proxy forwards `/api` to the backend.
 */
export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? '/api/v1',
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
})

// Attach the in-memory access token to every request.
api.interceptors.request.use((config) => {
  const token = tokenStore.getAccessToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

type RetriableConfig = InternalAxiosRequestConfig & { _retry?: boolean }

// Serialize concurrent refreshes behind a single in-flight promise: the backend
// revokes all sessions on refresh-token reuse, so two parallel refreshes would
// log the user out everywhere (defect F4).
let refreshing: Promise<RefreshOutcome> | null = null

/**
 * What a refresh attempt established. `token` is the new access token;
 * `sessionEnded` is true only when the backend refused the refresh token
 * (or there was none to send) — a refresh that merely could not be reached
 * leaves the session as it was.
 */
interface RefreshOutcome {
  token: string | null
  sessionEnded: boolean
}

async function refreshAccessToken(): Promise<RefreshOutcome> {
  const currentRefresh = tokenStore.getRefreshToken()
  if (!currentRefresh) {
    tokenStore.clear()
    return { token: null, sessionEnded: true }
  }

  try {
    // Backend contract: POST /auth/refresh with { refresh_token } in body.
    const { data } = await api.post('/auth/refresh', {
      refresh_token: currentRefresh,
    })
    const newAccess: string | null = data?.data?.access_token ?? null
    const newRefresh: string | null = data?.data?.refresh_token ?? null
    tokenStore.setTokens(newAccess, newRefresh)
    return { token: newAccess, sessionEnded: newAccess === null }
  } catch (err) {
    // The backend ends a session with a 401 (auth_service.py `refresh_token`).
    // A network failure, a 5xx or a 429 says nothing about the session, so the
    // tokens are kept and the next request tries the refresh again.
    const status = axios.isAxiosError(err) ? err.response?.status : undefined
    if (status !== 401 && status !== 403) return { token: null, sessionEnded: false }
    tokenStore.clear()
    return { token: null, sessionEnded: true }
  }
}

// On 401: try one refresh, retry the original request, and only hard-logout if
// the refresh itself fails (defect F4).
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config as RetriableConfig | undefined
    const isRefreshCall = original?.url?.includes('/auth/refresh')

    if (error.response?.status === 401 && original && !original._retry && !isRefreshCall) {
      original._retry = true
      refreshing = refreshing ?? refreshAccessToken()
      const { token, sessionEnded } = await refreshing
      refreshing = null

      if (token) {
        original.headers.Authorization = `Bearer ${token}`
        return api(original)
      }
      // Refresh refused: clear session so route guards send the user to /login,
      // and record why so the sign-in page can say it. Several requests usually
      // fail together; only the first finds a session to end, so the reason is
      // recorded once and a failed sign-in (also a 401) never reports one.
      const { isAuthenticated, logout } = useAuthStore.getState()
      if (sessionEnded && isAuthenticated) logout('session_ended')
    }
    return Promise.reject(error)
  },
)
