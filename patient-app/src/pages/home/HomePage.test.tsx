import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { queryClient } from '@/lib/query-client'
import { usePatientAuthStore } from '@/store/patient-auth-store'
import { fail, headerOf, noContent, ok, serve, type Outcome } from '@/test/fakeApi'
import { cityCare, lakeside, me, PHONE_E164, PHONE_MASKED } from '@/test/fixtures'
import { isSignedIn, renderApp, signIn } from '@/test/renderApp'

const ME = 'GET /me'
const LOGOUT = 'POST /auth/logout'

describe('home', () => {
  it('shows a loading state while the details are fetched', async () => {
    signIn()
    let answer: (outcome: Outcome) => void = () => {}
    serve({ [ME]: () => new Promise<Outcome>((resolve) => (answer = resolve)) })
    renderApp('/')

    expect(await screen.findByRole('status', { name: 'Loading your details…' })).toBeInTheDocument()

    answer(ok(me()))
    expect(await screen.findByText('No hospital linked yet')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows the masked phone from the API and an empty state leading to linking', async () => {
    signIn()
    serve({ [ME]: ok(me()) })
    const { user, router } = renderApp('/')

    expect(await screen.findByText(PHONE_MASKED)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(PHONE_E164)
    expect(screen.getByText('No hospital linked yet')).toBeInTheDocument()

    await user.click(screen.getByRole('link', { name: 'Link a hospital record' }))

    expect(await screen.findByRole('heading', { name: 'Link your hospital record' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/link-patient')
  })

  it('lists linked hospitals as cards, marking a paused link and saying what to do', async () => {
    signIn()
    serve({ [ME]: ok(me([cityCare, lakeside])) })
    renderApp('/')

    const list = await screen.findByRole('list', { name: 'Your hospitals' })
    const cards = within(list).getAllByRole('listitem')
    expect(cards).toHaveLength(2)

    expect(within(cards[0]).getByText('City Care Hospital')).toBeInTheDocument()
    expect(within(cards[0]).getByText('Linked on 3 Oct 2026')).toBeInTheDocument()
    expect(within(cards[0]).getByText('Linked')).toBeInTheDocument()

    expect(within(cards[1]).getByText('Lakeside Clinic')).toBeInTheDocument()
    expect(within(cards[1]).getByText('Paused')).toBeInTheDocument()
    expect(within(cards[1]).getByText(/contact the hospital/)).toBeInTheDocument()

    expect(screen.queryByText('No hospital linked yet')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Link another hospital' })).toHaveAttribute('href', '/link-patient')
  })

  it('shows an error with a retry that works, without leaking the server’s text', async () => {
    signIn()
    const api = serve({ [ME]: fail(500, 'INTERNAL_ERROR', 'Traceback…') })
    const { user } = renderApp('/')

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load your details.')
    expect(document.body).not.toHaveTextContent('Traceback')

    api.on({ [ME]: ok(me([cityCare])) })
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('City Care Hospital')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  describe('sign out', () => {
    it('revokes the session on the server, forgets it locally and returns to sign-in', async () => {
      signIn('access-1')
      const api = serve({ [ME]: ok(me([cityCare])), [LOGOUT]: noContent() })
      const { user, router } = renderApp('/')
      await screen.findByText('City Care Hospital')

      await user.click(screen.getByRole('button', { name: 'Sign out' }))

      expect(await screen.findByRole('heading', { name: 'Sign in with your mobile number' })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe('/login')

      const [request] = api.calls(LOGOUT)
      expect(api.calls(LOGOUT)).toHaveLength(1)
      expect(headerOf(request, 'X-Atheris-Patient')).toBe('1')
      expect(request.withCredentials).toBe(true)
      expect(request.data).toBeUndefined()

      expect(isSignedIn()).toBe(false)
      expect(queryClient.getQueryData(['patient', 'me'])).toBeUndefined()
      // Asked-for sign-out: no "you have been signed out" notice.
      expect(screen.queryByText(/You have been signed out/)).not.toBeInTheDocument()
    })

    it('still signs out locally when the server says the session was already gone', async () => {
      signIn()
      const api = serve({ [ME]: ok(me()), [LOGOUT]: fail(401, 'UNAUTHORIZED') })
      const { user } = renderApp('/')
      await screen.findByText(PHONE_MASKED)

      await user.click(screen.getByRole('button', { name: 'Sign out' }))

      expect(await screen.findByRole('heading', { name: 'Sign in with your mobile number' })).toBeInTheDocument()
      expect(isSignedIn()).toBe(false)
      expect(api.calls('POST /auth/refresh')).toHaveLength(0)
    })

    it('does not pretend to have signed out when the server could not be reached', async () => {
      signIn()
      serve({ [ME]: ok(me()), [LOGOUT]: fail(503, 'SERVICE_UNAVAILABLE') })
      const { user, router } = renderApp('/')
      await screen.findByText(PHONE_MASKED)

      await user.click(screen.getByRole('button', { name: 'Sign out' }))

      // The refresh cookie is still valid, so a reload would sign back in:
      // the patient is told, and stays where they are.
      expect(await screen.findByRole('alert')).toHaveTextContent('We could not sign you out.')
      expect(router.state.location.pathname).toBe('/')
      expect(isSignedIn()).toBe(true)
      await waitFor(() => expect(screen.getByRole('button', { name: 'Sign out' })).toBeEnabled())
    })
  })

  it('returns to sign-in with an explanation when the session ends while in use', async () => {
    signIn('access-old')
    const api = serve({ [ME]: fail(401, 'UNAUTHORIZED'), 'POST /auth/refresh': fail(401, 'UNAUTHORIZED') })
    const { router } = renderApp('/')

    expect(await screen.findByText('You have been signed out. Please sign in again.')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(api.calls('POST /auth/refresh')).toHaveLength(1)
    expect(usePatientAuthStore.getState().accessToken).toBeNull()
  })
})
