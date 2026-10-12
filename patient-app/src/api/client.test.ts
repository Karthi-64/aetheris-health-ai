import { describe, expect, it } from 'vitest'
import { ApiError } from '@atheris/api-core'
import { api, http, refreshSession } from '@/api/client'
import { queryClient } from '@/lib/query-client'
import { usePatientAuthStore } from '@/store/patient-auth-store'
import { fail, headerOf, noContent, ok, serve, type Handler, type Outcome } from '@/test/fakeApi'
import { signIn } from '@/test/renderApp'

const REFRESH = 'POST /auth/refresh'

/** A resource that accepts only `token`, as the server does after a rotation. */
const accepting =
  (token: string, data: unknown = { ok: true }): Handler =>
  (config) =>
    headerOf(config, 'Authorization') === `Bearer ${token}` ? ok(data) : fail(401, 'UNAUTHORIZED')

const refreshedTo = (token: string) => ok({ access_token: token, expires_in: 900 })

const session = () => usePatientAuthStore.getState()

describe('patient API client', () => {
  it('calls the patient API with credentials', () => {
    expect(api.defaults.baseURL).toBe('/api/v1/patient')
    expect(api.defaults.withCredentials).toBe(true)
  })

  it('attaches the bearer token to protected calls and never to /auth/ calls', async () => {
    signIn('access-1')
    const fake = serve({
      'GET /me': ok({}),
      'POST /auth/otp/request': ok({}, 202),
      'POST /auth/otp/verify': ok({}),
      'POST /auth/logout': noContent(),
    })

    await http.get('/me')
    await http.post('/auth/otp/request', { phone: '+919876543210' })
    await http.post('/auth/otp/verify', { challenge_id: 'c', code: '123456' })
    await http.post('/auth/logout')

    expect(headerOf(fake.calls('GET /me')[0], 'Authorization')).toBe('Bearer access-1')
    for (const route of ['POST /auth/otp/request', 'POST /auth/otp/verify', 'POST /auth/logout']) {
      expect(headerOf(fake.calls(route)[0], 'Authorization')).toBeUndefined()
    }
  })

  it('unwraps the envelope and throws a typed error carrying the backend code', async () => {
    signIn()
    serve({ 'GET /me': fail(403, 'LINK_UNAVAILABLE', 'Please contact the hospital.') })

    const error = await http.get('/me').catch((err: unknown) => err)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ code: 'LINK_UNAVAILABLE', status: 403 })
  })

  describe('expired access token (401)', () => {
    it('refreshes ONCE for several requests failing together, then retries each with the new token', async () => {
      signIn('access-old')
      let finishRefresh: (outcome: Outcome) => void = () => {}
      const fake = serve({
        'GET /me': accepting('access-new', { which: 'me' }),
        'GET /a': accepting('access-new', { which: 'a' }),
        'GET /b': accepting('access-new', { which: 'b' }),
        [REFRESH]: () => new Promise<Outcome>((resolve) => (finishRefresh = resolve)),
      })

      const all = Promise.all([http.get('/me'), http.get('/a'), http.get('/b')])
      // All three have failed and are waiting on the one refresh in flight.
      await expect.poll(() => fake.calls(REFRESH).length).toBe(1)
      await new Promise((resolve) => setTimeout(resolve, 0))
      expect(fake.calls(REFRESH)).toHaveLength(1)
      finishRefresh(refreshedTo('access-new'))

      expect(await all).toEqual([{ which: 'me' }, { which: 'a' }, { which: 'b' }])
      expect(fake.calls(REFRESH)).toHaveLength(1)
      expect(headerOf(fake.calls(REFRESH)[0], 'X-Atheris-Patient')).toBe('1')
      expect(headerOf(fake.calls(REFRESH)[0], 'Authorization')).toBeUndefined()
      // Each request: one refusal, one retry.
      expect(fake.sent).toHaveLength(7)
      expect(session().accessToken).toBe('access-new')
    })

    it('retries without a second refresh when the token was already replaced while the request was in flight', async () => {
      signIn('access-old')
      const fake = serve({
        'GET /me': (config) => {
          // The refusal arrives after another request's refresh has finished.
          if (headerOf(config, 'Authorization') === 'Bearer access-old') {
            session().setAccessToken('access-new')
            return fail(401, 'UNAUTHORIZED')
          }
          return ok({ which: 'me' })
        },
      })

      expect(await http.get('/me')).toEqual({ which: 'me' })
      expect(fake.calls(REFRESH)).toHaveLength(0)
    })

    it('ends the session when the refresh is refused, once, and says why', async () => {
      signIn('access-old')
      queryClient.setQueryData(['patient', 'me'], { cached: 'for the previous session' })
      const fake = serve({
        'GET /me': fail(401, 'UNAUTHORIZED'),
        'GET /a': fail(401, 'UNAUTHORIZED'),
        [REFRESH]: fail(401, 'UNAUTHORIZED'),
      })

      const results = await Promise.allSettled([http.get('/me'), http.get('/a')])

      expect(results.map((r) => r.status)).toEqual(['rejected', 'rejected'])
      expect(fake.calls(REFRESH)).toHaveLength(1)
      expect(session().accessToken).toBeNull()
      expect(session().signOutReason).toBe('session_ended')
      // Nothing the ended session loaded is left for whoever signs in next.
      expect(queryClient.getQueryData(['patient', 'me'])).toBeUndefined()
    })

    it('keeps the session when the refresh could not be reached', async () => {
      signIn('access-old')
      const fake = serve({ 'GET /me': fail(401, 'UNAUTHORIZED'), [REFRESH]: fail(503, 'SERVICE_UNAVAILABLE') })

      await expect(http.get('/me')).rejects.toMatchObject({ status: 401 })

      expect(fake.calls(REFRESH)).toHaveLength(1)
      expect(session().accessToken).toBe('access-old')
      expect(session().signOutReason).toBeNull()
    })

    it('gives up after one retry instead of looping', async () => {
      signIn('access-old')
      const fake = serve({ 'GET /me': fail(401, 'UNAUTHORIZED'), [REFRESH]: refreshedTo('access-new') })

      await expect(http.get('/me')).rejects.toMatchObject({ status: 401 })

      expect(fake.calls('GET /me')).toHaveLength(2)
      expect(fake.calls(REFRESH)).toHaveLength(1)
    })

    it('does not refresh for a 401 on the sign-in endpoints or for a signed-out visitor', async () => {
      const fake = serve({
        'POST /auth/otp/verify': fail(401, 'OTP_INVALID'),
        'GET /me': fail(401, 'UNAUTHORIZED'),
      })

      await expect(http.post('/auth/otp/verify', {})).rejects.toMatchObject({ code: 'OTP_INVALID' })
      await expect(http.get('/me')).rejects.toMatchObject({ status: 401 })

      expect(fake.calls(REFRESH)).toHaveLength(0)
    })
  })

  it('shares one refresh between callers and allows a new one afterwards', async () => {
    const fake = serve({ [REFRESH]: refreshedTo('access-1') })

    const [first, second] = await Promise.all([refreshSession(), refreshSession()])
    expect(first).toEqual({ token: 'access-1', sessionEnded: false })
    expect(second).toBe(first)
    expect(fake.calls(REFRESH)).toHaveLength(1)

    fake.on({ [REFRESH]: refreshedTo('access-2') })
    expect((await refreshSession()).token).toBe('access-2')
    expect(fake.calls(REFRESH)).toHaveLength(2)
  })
})
