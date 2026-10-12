import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { usePatientAuthStore } from '@/store/patient-auth-store'
import { fail, headerOf, ok, serve, type Outcome } from '@/test/fakeApi'
import { cityCare, me, PHONE_MASKED } from '@/test/fixtures'
import { isSignedIn, loadApp } from '@/test/renderApp'

const REFRESH = 'POST /auth/refresh'
const ME = 'GET /me'

const refreshed = (accessToken = 'access-restored') => ok({ access_token: accessToken, expires_in: 900 })

describe('session restore on load', () => {
  it('holds every page back until the server has answered', async () => {
    let answer: (outcome: Outcome) => void = () => {}
    serve({ [REFRESH]: () => new Promise<Outcome>((resolve) => (answer = resolve)), [ME]: ok(me()) })
    loadApp('/')

    expect(await screen.findByRole('status')).toHaveTextContent('Getting things ready…')
    expect(screen.queryByRole('heading')).not.toBeInTheDocument()

    answer(refreshed())
    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument()
  })

  it('restores the session from the refresh cookie with exactly one CSRF-protected request', async () => {
    const api = serve({ [REFRESH]: refreshed('access-restored'), [ME]: ok(me([cityCare])) })
    const { router } = loadApp('/')

    expect(await screen.findByText(PHONE_MASKED)).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')

    // One request even though StrictMode runs the effect twice: a second use
    // of the same refresh token would be treated as theft by the server.
    expect(api.calls(REFRESH)).toHaveLength(1)
    const [request] = api.calls(REFRESH)
    expect(headerOf(request, 'X-Atheris-Patient')).toBe('1')
    expect(request.withCredentials).toBe(true)
    // The cookie is the credential: no body, and no bearer token to send.
    expect(request.data).toBeUndefined()
    expect(headerOf(request, 'Authorization')).toBeUndefined()

    expect(usePatientAuthStore.getState().accessToken).toBe('access-restored')
    expect(headerOf(api.calls(ME)[0], 'Authorization')).toBe('Bearer access-restored')
  })

  it('restores a deep link to the page that was asked for', async () => {
    serve({ [REFRESH]: refreshed() })
    const { router } = loadApp('/link-patient')

    expect(await screen.findByRole('heading', { name: 'Link your hospital record' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/link-patient')
  })

  it.each([
    ['the cookie is refused', fail(401, 'UNAUTHORIZED')],
    ['the CSRF check fails', fail(403, 'FORBIDDEN')],
    ['the server is down', fail(503, 'SERVICE_UNAVAILABLE')],
    ['the answer carries no token', ok({})],
  ])('shows the sign-in page when %s', async (_case, outcome) => {
    const api = serve({ [REFRESH]: outcome })
    const { router } = loadApp('/')

    expect(await screen.findByRole('heading', { name: 'Sign in with your mobile number' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(isSignedIn()).toBe(false)
    expect(api.calls(REFRESH)).toHaveLength(1)
    // Nothing protected was asked for, and a first-time visitor is not told
    // they were "signed out".
    expect(api.calls(ME)).toHaveLength(0)
    expect(screen.queryByText(/You have been signed out/)).not.toBeInTheDocument()
  })

  it('does not ask again when a session is already in memory', async () => {
    const api = serve({ [ME]: ok(me()) })
    loadApp('/', 'access-1')

    expect(await screen.findByText(PHONE_MASKED)).toBeInTheDocument()
    await waitFor(() => expect(api.calls(ME)).toHaveLength(1))
    expect(api.calls(REFRESH)).toHaveLength(0)
  })
})
