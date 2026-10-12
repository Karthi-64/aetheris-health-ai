import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ok, serve } from '@/test/fakeApi'
import { me } from '@/test/fixtures'
import { renderApp, signIn } from '@/test/renderApp'

const LOGIN_HEADING = 'Sign in with your mobile number'

describe('route guards', () => {
  it.each([
    '/',
    '/link-patient',
    '/hospitals',
    '/hospitals?q=care&city=Bengaluru&page=2',
    '/hospitals/city-care',
    '/hospitals/city-care/doctors',
    '/hospitals/city-care/doctors?q=rao&dept=c0000000-0000-4000-8000-000000000001&page=2',
    '/hospitals/city-care/doctors/d0000000-0000-4000-8000-000000000001',
    '/hospitals/city-care/doctors/d0000000-0000-4000-8000-000000000001/availability',
    '/hospitals/city-care/doctors/d0000000-0000-4000-8000-000000000001/availability?date=2026-10-14&slot=2026-10-14T09%3A00%3A00%2B05%3A30',
    '/hospitals/city-care/doctors/d0000000-0000-4000-8000-000000000001/book?date=2026-10-14&start=2026-10-14T09%3A00%3A00%2B05%3A30&end=2026-10-14T09%3A15%3A00%2B05%3A30',
    '/appointments',
    '/appointments?view=past&page=2',
    '/appointments/e0000000-0000-4000-8000-000000000001',
    '/appointments/e0000000-0000-4000-8000-000000000001?cancel=1',
    '/appointments/not-a-reference',
    '/no-such-page',
  ])('sends a visitor at %s to the sign-in page', async (path) => {
    const api = serve({})
    const { router } = renderApp(path)

    expect(await screen.findByRole('heading', { name: LOGIN_HEADING })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    // Nothing protected is requested for a visitor.
    expect(api.sent).toHaveLength(0)
  })

  it.each(['/login', '/verify-otp', '/no-such-page'])('sends a signed-in patient at %s home', async (path) => {
    signIn()
    serve({ 'GET /me': ok(me()) })
    const { router } = renderApp(path)

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')
  })

  it('offers no staff route', async () => {
    signIn()
    serve({ 'GET /me': ok(me()) })
    const { router } = renderApp('/dashboard/patients')

    expect(await screen.findByRole('heading', { name: 'Welcome' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')
  })
})
