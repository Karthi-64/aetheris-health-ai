import { AxiosError } from 'axios'
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { http } from '@/api/http'
import { api } from '@/lib/api'
import { queryClient } from '@/lib/query-client'
import { tokenStore } from '@/services/tokenStore'
import { useAuthStore, type AuthUser } from '@/store/auth-store'
import { fail, installFakeApi, ok, type FakeApi } from '@/test/fakeApi'

const NURSE: AuthUser = { id: 'nurse-1', name: 'Nurse', email: 'n@h.test', permissions: [] }

/**
 * What happens when the access token has expired and the refresh token is no
 * longer good (backend/app/services/auth_service.py `refresh_token`: 401).
 */
describe('api — a session that cannot be refreshed', () => {
  let server: FakeApi

  beforeEach(() => {
    useAuthStore.getState().logout()
    queryClient.clear()
    // Every call is refused, the refresh included.
    server = installFakeApi(() => fail(401, 'Refresh token has been revoked. All sessions invalidated.'))
  })

  afterEach(() => server.restore())

  it('signs the user out once, with the reason, when several requests fail together', async () => {
    useAuthStore.getState().setAuth(NURSE, 'expired-access', 'revoked-refresh')
    const reasons: (string | null)[] = []
    const unsubscribe = useAuthStore.subscribe((state, previous) => {
      if (previous.isAuthenticated && !state.isAuthenticated) reasons.push(state.signOutReason)
    })

    const results = await Promise.allSettled([
      http.get('/patients'),
      http.get('/appointments'),
      http.get('/notifications'),
    ])
    unsubscribe()

    expect(results.map((r) => r.status)).toEqual(['rejected', 'rejected', 'rejected'])
    expect(reasons).toEqual(['session_ended'])
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
    expect(useAuthStore.getState().signOutReason).toBe('session_ended')
    expect(tokenStore.getAccessToken()).toBeNull()
    // One refresh for the three of them, not three.
    expect(server.requests('post', '/auth/refresh')).toHaveLength(1)
  })

  it('empties the query cache on that forced sign-out', async () => {
    useAuthStore.getState().setAuth(NURSE, 'expired-access', 'revoked-refresh')
    queryClient.setQueryData(['patients', 'list'], ['a patient the next user may not see'])

    await expect(http.get('/patients')).rejects.toMatchObject({ status: 401 })

    expect(queryClient.getQueryData(['patients', 'list'])).toBeUndefined()
  })

  it('does not call a wrong password an ended session', async () => {
    await expect(api.post('/auth/login', { email: 'n@h.test', password: 'wrong-password' })).rejects.toBeDefined()

    expect(useAuthStore.getState().signOutReason).toBeNull()
  })

  it('keeps the reason when a late request fails after the sign-out', async () => {
    useAuthStore.getState().setAuth(NURSE, 'expired-access', 'revoked-refresh')
    await expect(http.get('/patients')).rejects.toBeDefined()
    await expect(http.get('/appointments')).rejects.toBeDefined()

    expect(useAuthStore.getState().signOutReason).toBe('session_ended')
  })

  it('still refreshes and retries when the refresh token is good', async () => {
    server.restore()
    server = installFakeApi((config) => {
      if (config.url === '/auth/refresh') return ok({ access_token: 'new-access', refresh_token: 'new-refresh' })
      return config.headers.Authorization === 'Bearer new-access' ? ok(['patient']) : fail(401, 'Token expired.')
    })
    useAuthStore.getState().setAuth(NURSE, 'expired-access', 'good-refresh')

    await expect(http.get('/patients')).resolves.toEqual(['patient'])
    expect(useAuthStore.getState().isAuthenticated).toBe(true)
    expect(useAuthStore.getState().signOutReason).toBeNull()
  })

  // The backend ends a session only with a 401 (auth_service.py
  // `refresh_token`); anything else from /auth/refresh is a passing fault.
  it.each([
    ['the server is failing', () => fail(503, 'Service unavailable.')],
    ['the refresh is rate limited', () => fail(429, 'Too many requests.')],
    [
      'the network drops',
      () => {
        throw new AxiosError('Network Error', 'ERR_NETWORK')
      },
    ],
  ])('does not end the session when the refresh fails because %s', async (_name, refreshAnswer) => {
    server.restore()
    let refreshWorks = false
    server = installFakeApi((config) => {
      if (config.url === '/auth/refresh') {
        return refreshWorks ? ok({ access_token: 'new-access', refresh_token: 'new-refresh' }) : refreshAnswer()
      }
      return config.headers.Authorization === 'Bearer new-access' ? ok(['patient']) : fail(401, 'Token expired.')
    })
    useAuthStore.getState().setAuth(NURSE, 'expired-access', 'good-refresh')
    queryClient.setQueryData(['patients', 'list'], ['still on screen'])

    await expect(http.get('/patients')).rejects.toBeDefined()

    expect(useAuthStore.getState().isAuthenticated).toBe(true)
    expect(useAuthStore.getState().signOutReason).toBeNull()
    expect(tokenStore.getRefreshToken()).toBe('good-refresh')
    expect(queryClient.getQueryData(['patients', 'list'])).toEqual(['still on screen'])

    // Once the fault passes, the kept refresh token recovers the session.
    refreshWorks = true
    await expect(http.get('/patients')).resolves.toEqual(['patient'])
    expect(useAuthStore.getState().isAuthenticated).toBe(true)
  })

  it('ends the session when the refresh token is refused with a 403', async () => {
    server.restore()
    server = installFakeApi((config) =>
      config.url === '/auth/refresh' ? fail(403, 'Forbidden.') : fail(401, 'Token expired.'),
    )
    useAuthStore.getState().setAuth(NURSE, 'expired-access', 'refused-refresh')

    await expect(http.get('/patients')).rejects.toBeDefined()

    expect(useAuthStore.getState().signOutReason).toBe('session_ended')
    expect(tokenStore.getRefreshToken()).toBeNull()
  })
})
