import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { AxiosError } from 'axios'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import ForgotPasswordPage from './ForgotPasswordPage'
import ResetPasswordPage from './ResetPasswordPage'

/**
 * The two public password pages against `POST /auth/password/forgot` and
 * `POST /auth/password/reset` (`backend/app/api/v1/auth.py`). The real Axios
 * instance runs; only the network adapter is replaced.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

let fake: FakeApi
let onPost: () => Outcome | Promise<Outcome>

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  onPost = () => ok(null)
  fake = installFakeApi(() => onPost())
})

afterEach(() => fake.restore())

describe('ForgotPasswordPage', () => {
  function renderPage() {
    render(
      <MemoryRouter>
        <ForgotPasswordPage />
      </MemoryRouter>,
    )
  }

  it('does not claim an email was sent, and says the same whether or not the account exists', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.type(screen.getByLabelText('Email'), 'nobody@hospital.test')
    await user.click(screen.getByRole('button', { name: /send reset link/i }))

    expect(
      await screen.findByText(/If an account exists for that address, a reset link is sent to it/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Email delivery has\s+to be configured for this hospital/)).toBeInTheDocument()
    expect(screen.queryByText(/we.ve sent/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/check your email/i)).not.toBeInTheDocument()
    expect(bodyOf(fake.requests('post', '/auth/password/forgot')[0])).toEqual({
      email: 'nobody@hospital.test',
    })
  })

  it('shows the same message when the request fails', async () => {
    onPost = () => ({ status: 500, data: 'Internal Server Error' })
    const user = userEvent.setup()
    renderPage()
    await user.type(screen.getByLabelText('Email'), 'nurse@hospital.test')
    await user.click(screen.getByRole('button', { name: /send reset link/i }))

    expect(await screen.findByText(/If an account exists for that address/)).toBeInTheDocument()
  })

  it('asks for one reset link when the form is submitted twice in the same tick', async () => {
    const user = userEvent.setup()
    renderPage()
    const email = screen.getByLabelText('Email')
    await user.type(email, 'nurse@hospital.test')

    const form = email.closest('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    await screen.findByText(/If an account exists for that address/)
    expect(fake.requests('post', '/auth/password/forgot')).toHaveLength(1)
  })
})

describe('ResetPasswordPage', () => {
  function renderPage(entry = '/reset-password#token=tok-123') {
    render(
      <MemoryRouter initialEntries={[entry]}>
        <ResetPasswordPage />
      </MemoryRouter>,
    )
  }

  it('takes the token from the URL fragment, which is never sent to a server', async () => {
    const user = userEvent.setup()
    renderPage('/reset-password#token=tok-frag')
    await fill(user)
    await submit(user)

    await waitFor(() => expect(resets()).toHaveLength(1))
    expect(bodyOf(resets()[0])).toEqual({ token: 'tok-frag', new_password: 'correct-horse-battery' })
  })

  it('still accepts a link sent before the token moved to the fragment', async () => {
    const user = userEvent.setup()
    renderPage('/reset-password?token=tok-query')
    await fill(user)
    await submit(user)

    await waitFor(() => expect(resets()).toHaveLength(1))
    expect(bodyOf(resets()[0])).toEqual({ token: 'tok-query', new_password: 'correct-horse-battery' })
  })

  it('removes the token from the address bar and history once it has been read', () => {
    const replaceState = vi.spyOn(window.history, 'replaceState')
    renderPage()

    expect(replaceState).toHaveBeenCalledTimes(1)
    const url = String(replaceState.mock.calls[0][2])
    expect(url).not.toContain('token')
    expect(url).not.toContain('tok-123')
    replaceState.mockRestore()
  })

  async function fill(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText('New password'), 'correct-horse-battery')
    await user.type(screen.getByLabelText('Confirm password'), 'correct-horse-battery')
  }

  const submit = (user: ReturnType<typeof userEvent.setup>) =>
    user.click(screen.getByRole('button', { name: 'Reset password' }))

  const resets = () => fake.requests('post', '/auth/password/reset')

  it('sends the token and the new password, then confirms', async () => {
    const user = userEvent.setup()
    renderPage()
    await fill(user)
    await submit(user)

    expect(await screen.findByText('Password reset!')).toBeInTheDocument()
    expect(bodyOf(resets()[0])).toEqual({ token: 'tok-123', new_password: 'correct-horse-battery' })
  })

  it("shows the API's sentence for a used or expired link (401)", async () => {
    onPost = () => fail(401, 'Invalid or expired password reset token.')
    const user = userEvent.setup()
    renderPage()
    await fill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Invalid or expired password reset token.'),
    )
  })

  it("shows the API's sentence for a password it will not accept (400)", async () => {
    onPost = () => fail(400, 'Password does not meet requirements.')
    const user = userEvent.setup()
    renderPage()
    await fill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Password does not meet requirements.'),
    )
  })

  it('shows a plain sentence, not transport text, for a server failure', async () => {
    onPost = () => ({ status: 500, data: 'Internal Server Error' })
    const user = userEvent.setup()
    renderPage()
    await fill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't reset your password. Please try again."),
    )
  })

  it('shows a plain sentence when the network is down', async () => {
    onPost = () => {
      throw new AxiosError('Network Error', 'ERR_NETWORK')
    }
    const user = userEvent.setup()
    renderPage()
    await fill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't reset your password. Please try again."),
    )
  })

  it('spends the single-use token once when the form is submitted twice in the same tick', async () => {
    const user = userEvent.setup()
    renderPage()
    await fill(user)

    const form = screen.getByLabelText('New password').closest('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await screen.findByText('Password reset!')).toBeInTheDocument()
    expect(resets()).toHaveLength(1)
    expect(toastError).not.toHaveBeenCalled()
  })
})
