import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { tokenStore } from '@/services/tokenStore'
import { useAuthStore } from '@/store/auth-store'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Handler } from '@/test/fakeApi'
import LoginPage from './LoginPage'

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const EMAIL = 'asha.rao@hospital.test'
const PASSWORD = 'Correct!Horse9Battery'

/** The token payload both POST /auth/login and POST /auth/mfa/verify answer with. */
const SESSION = {
  access_token: 'access-1',
  refresh_token: 'refresh-1',
  expires_in: 900,
  user: {
    id: 'u-asha',
    email: EMAIL,
    first_name: 'Asha',
    last_name: 'Rao',
    roles: ['Nurse'],
    permissions: ['patient.read'],
  },
}

/** What login answers for an account with MFA on (backend/app/api/v1/auth.py `login`). */
const MFA_REQUIRED = { mfa_ticket: 'ticket-abc', expires_in: 300 }

let server: FakeApi

function serve(handler: Handler) {
  server = installFakeApi(handler)
}

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/dashboard" element={<h1>Dashboard</h1>} />
      </Routes>
    </MemoryRouter>,
  )
}

async function submitPassword(user: ReturnType<typeof userEvent.setup>, password = PASSWORD) {
  await user.type(screen.getByLabelText('Email'), EMAIL)
  await user.type(screen.getByLabelText('Password'), password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

async function submitCode(user: ReturnType<typeof userEvent.setup>, code: string) {
  await user.type(await screen.findByLabelText('Authentication code'), code)
  await user.click(screen.getByRole('button', { name: 'Verify' }))
}

describe('LoginPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useAuthStore.getState().logout()
  })

  afterEach(() => server?.restore())

  it('signs in with email and password when the account has no MFA', async () => {
    const user = userEvent.setup()
    serve(() => ok(SESSION))
    renderLogin()

    await submitPassword(user)

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(server.sent).toHaveLength(1)
    expect(server.sent[0].url).toBe('/auth/login')
    expect(bodyOf(server.sent[0])).toEqual({ email: EMAIL, password: PASSWORD })
    expect(useAuthStore.getState().user).toMatchObject({ id: 'u-asha', name: 'Asha Rao', permissions: ['patient.read'] })
    expect(tokenStore.getAccessToken()).toBe('access-1')
    expect(tokenStore.getRefreshToken()).toBe('refresh-1')
    expect(toastSuccess).toHaveBeenCalledWith('Welcome back')
    expect(screen.queryByLabelText('Authentication code')).not.toBeInTheDocument()
  })

  it('keeps one wording for every failed password step', async () => {
    const user = userEvent.setup()
    serve(() => fail(401, 'Invalid credentials.'))
    renderLogin()

    await submitPassword(user, 'Wrong!Passw0rd99')

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'Invalid credentials, or the server is unavailable.',
        // The same hint for every failure, so it says nothing about the account.
        { description: expect.stringContaining('Forgot password?') },
      ),
    )
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
  })

  it('asks an MFA account for its code, verifies it with the ticket, then signs in', async () => {
    const user = userEvent.setup()
    serve((config) => (config.url === '/auth/login' ? ok(MFA_REQUIRED) : ok(SESSION)))
    renderLogin()

    await submitPassword(user)

    const code = await screen.findByLabelText('Authentication code')
    expect(code).toHaveAttribute('inputmode', 'numeric')
    expect(code).toHaveAttribute('autocomplete', 'one-time-code')
    // Not signed in yet, and nothing claims otherwise.
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(toastError).not.toHaveBeenCalled()

    await user.type(code, '123456')
    await user.click(screen.getByRole('button', { name: 'Verify' }))

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    expect(server.sent.map((c) => c.url)).toEqual(['/auth/login', '/auth/mfa/verify'])
    expect(bodyOf(server.sent[0])).toEqual({ email: EMAIL, password: PASSWORD })
    expect(bodyOf(server.sent[1])).toEqual({ mfa_ticket: 'ticket-abc', code: '123456' })
    expect(useAuthStore.getState().user).toMatchObject({ id: 'u-asha', permissions: ['patient.read'] })
    expect(tokenStore.getAccessToken()).toBe('access-1')
    expect(tokenStore.getRefreshToken()).toBe('refresh-1')
    expect(toastSuccess).toHaveBeenCalledWith('Welcome back')
  })

  it('does not send a code that is not six digits', async () => {
    const user = userEvent.setup()
    serve((config) => (config.url === '/auth/login' ? ok(MFA_REQUIRED) : ok(SESSION)))
    renderLogin()

    await submitPassword(user)
    await submitCode(user, '12ab')

    expect(await screen.findByText('Enter the 6-digit code')).toBeInTheDocument()
    expect(server.requests('post', '/auth/mfa/verify')).toHaveLength(0)
  })

  it('keeps the user on the code step when the code is wrong, and lets them try again', async () => {
    const user = userEvent.setup()
    let attempts = 0
    serve((config) => {
      if (config.url === '/auth/login') return ok(MFA_REQUIRED)
      attempts += 1
      return attempts === 1 ? fail(401, 'Invalid MFA code.', { error_code: 'UNAUTHORIZED' }) : ok(SESSION)
    })
    renderLogin()

    await submitPassword(user)
    await submitCode(user, '000000')

    expect(await screen.findByRole('alert')).toHaveTextContent('That code was not accepted')
    const code = screen.getByLabelText('Authentication code')
    expect(code).toHaveAttribute('aria-invalid', 'true')
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
    expect(useAuthStore.getState().signOutReason).toBeNull()
    expect(toastError).not.toHaveBeenCalled()

    await user.clear(code)
    await user.type(code, '654321')
    await user.click(screen.getByRole('button', { name: 'Verify' }))

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
    // The same ticket is reused; the password is not asked for again.
    expect(server.requests('post', '/auth/login')).toHaveLength(1)
    expect(bodyOf(server.requests('post', '/auth/mfa/verify')[1])).toEqual({
      mfa_ticket: 'ticket-abc',
      code: '654321',
    })
  })

  it('goes back to the password step with an explanation when the ticket has expired', async () => {
    const user = userEvent.setup()
    serve((config) =>
      config.url === '/auth/login'
        ? ok(MFA_REQUIRED)
        : fail(401, 'Invalid or expired MFA ticket.', { error_code: 'UNAUTHORIZED' }),
    )
    renderLogin()

    await submitPassword(user)
    await submitCode(user, '123456')

    expect(await screen.findByRole('status')).toHaveTextContent(
      'That sign-in attempt expired. Enter your password to start again.',
    )
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.queryByLabelText('Authentication code')).not.toBeInTheDocument()
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
    expect(toastError).not.toHaveBeenCalled()
  })

  it('reports a server failure on the code step without leaving it', async () => {
    const user = userEvent.setup()
    serve((config) => (config.url === '/auth/login' ? ok(MFA_REQUIRED) : fail(500, 'Internal error.')))
    renderLogin()

    await submitPassword(user)
    await submitCode(user, '123456')

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Could not verify the code. Please try again.'),
    )
    expect(screen.getByLabelText('Authentication code')).toBeInTheDocument()
  })

  it('can step back from the code to the password form', async () => {
    const user = userEvent.setup()
    serve(() => ok(MFA_REQUIRED))
    renderLogin()

    await submitPassword(user)
    await user.click(await screen.findByRole('button', { name: 'Back to sign in' }))

    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says once, on the page, that the session ended', () => {
    useAuthStore.getState().setAuth(
      { id: 'u-asha', name: 'Asha Rao', email: EMAIL, permissions: [] },
      'access-1',
    )
    useAuthStore.getState().logout('session_ended')
    renderLogin()

    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveTextContent('Your session has ended. Sign in again.')
    expect(toastError).not.toHaveBeenCalled()
  })

  it('says nothing of the kind after an ordinary sign-out', () => {
    useAuthStore.getState().logout()
    renderLogin()

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
