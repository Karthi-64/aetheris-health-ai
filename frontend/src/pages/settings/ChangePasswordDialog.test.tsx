import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ChangePasswordDialog } from './ChangePasswordDialog'
import { ApiError } from '@/api/types'
import { RequireAuth } from '@/components/auth/RequireAuth'
import LoginPage from '@/pages/LoginPage'
import { tokenStore } from '@/services/tokenStore'
import { useAuthStore } from '@/store/auth-store'

const { post } = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('@/api/http', () => ({ http: { post } }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ChangePasswordDialog trigger={<button>Change password</button>} />
    </QueryClientProvider>,
  )
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /change password/i }))
  return screen.findByRole('dialog')
}

describe('ChangePasswordDialog', () => {
  beforeEach(() => vi.clearAllMocks())

  it('posts the current and new password to the auth endpoint', async () => {
    const user = userEvent.setup()
    post.mockResolvedValue({})
    renderDialog()
    await open(user)

    await user.type(screen.getByLabelText(/current password/i), 'Old!Passw0rd123')
    await user.type(screen.getByLabelText(/^new password/i), 'New!Passw0rd123')
    await user.type(screen.getByLabelText(/confirm new password/i), 'New!Passw0rd123')
    await user.click(screen.getByRole('button', { name: /^change password$/i }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/auth/password/change', {
        current_password: 'Old!Passw0rd123',
        new_password: 'New!Passw0rd123',
      }),
    )
  })

  it('refuses a mismatched confirmation without calling the API', async () => {
    const user = userEvent.setup()
    renderDialog()
    await open(user)

    await user.type(screen.getByLabelText(/current password/i), 'Old!Passw0rd123')
    await user.type(screen.getByLabelText(/^new password/i), 'New!Passw0rd123')
    await user.type(screen.getByLabelText(/confirm new password/i), 'Different!123456')
    await user.click(screen.getByRole('button', { name: /^change password$/i }))

    expect(await screen.findByText(/do not match/i)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('enforces the backend 12-character floor client-side', async () => {
    const user = userEvent.setup()
    renderDialog()
    await open(user)

    await user.type(screen.getByLabelText(/current password/i), 'Old!Passw0rd123')
    await user.type(screen.getByLabelText(/^new password/i), 'short')
    await user.type(screen.getByLabelText(/confirm new password/i), 'short')
    await user.click(screen.getByRole('button', { name: /^change password$/i }))

    expect(await screen.findByText(/at least 12 characters/i)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('puts a rejected current password on that field, not in a toast', async () => {
    const user = userEvent.setup()
    post.mockRejectedValue(new ApiError('Current password is incorrect.', 'unauthorized', 401))
    renderDialog()
    await open(user)

    await user.type(screen.getByLabelText(/current password/i), 'Wrong!Passw0rd1')
    await user.type(screen.getByLabelText(/^new password/i), 'New!Passw0rd123')
    await user.type(screen.getByLabelText(/confirm new password/i), 'New!Passw0rd123')
    await user.click(screen.getByRole('button', { name: /^change password$/i }))

    expect(await screen.findByText(/not your current password/i)).toBeInTheDocument()
  })

  /**
   * `AuthService.change_password` revokes every refresh token the user has —
   * this tab's too — and the endpoint returns no new ones
   * (backend/app/services/auth_service.py). The dialog has to say so and act on it.
   */
  describe('this session is revoked too', () => {
    function renderInApp() {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      useAuthStore.getState().setAuth(
        { id: 'u1', name: 'Asha Rao', email: 'asha@hospital.test', permissions: [] },
        'access-1',
        'refresh-1',
      )
      useAuthStore.getState().setRestoring(false)
      return render(
        <QueryClientProvider client={client}>
          <MemoryRouter initialEntries={['/settings/profile']}>
            <Routes>
              <Route
                path="/settings/profile"
                element={
                  <RequireAuth>
                    <ChangePasswordDialog trigger={<button>Change password</button>} />
                  </RequireAuth>
                }
              />
              <Route path="/login" element={<LoginPage />} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>,
      )
    }

    it('says before submitting that this device is signed out as well', async () => {
      const user = userEvent.setup()
      renderInApp()
      const dialog = await open(user)

      expect(dialog).toHaveTextContent(/signs you out on every device, including this one/i)
      expect(dialog).not.toHaveTextContent(/stay signed in/i)
    })

    it('signs out and lands on the sign-in page, which says why', async () => {
      const user = userEvent.setup()
      post.mockResolvedValue({})
      renderInApp()
      await open(user)

      await user.type(screen.getByLabelText(/current password/i), 'Old!Passw0rd123')
      await user.type(screen.getByLabelText(/^new password/i), 'New!Passw0rd123')
      await user.type(screen.getByLabelText(/confirm new password/i), 'New!Passw0rd123')
      await user.click(screen.getByRole('button', { name: /^change password$/i }))

      expect(await screen.findByRole('status')).toHaveTextContent(
        'Your password was changed. Sign in again with your new password.',
      )
      expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

      // The revoked tokens are dropped, so nothing can try to use them.
      expect(useAuthStore.getState().isAuthenticated).toBe(false)
      expect(tokenStore.getAccessToken()).toBeNull()
      expect(tokenStore.getRefreshToken()).toBeNull()
      expect(post).toHaveBeenCalledTimes(1)
      // No toast claims the session carries on, and none reports a failure.
      expect(toast.success).not.toHaveBeenCalled()
      expect(toast.error).not.toHaveBeenCalled()
    })

    it('stays signed in when the change is refused', async () => {
      const user = userEvent.setup()
      post.mockRejectedValue(new ApiError('Current password is incorrect.', 'unauthorized', 401))
      renderInApp()
      await open(user)

      await user.type(screen.getByLabelText(/current password/i), 'Wrong!Passw0rd1')
      await user.type(screen.getByLabelText(/^new password/i), 'New!Passw0rd123')
      await user.type(screen.getByLabelText(/confirm new password/i), 'New!Passw0rd123')
      await user.click(screen.getByRole('button', { name: /^change password$/i }))

      expect(await screen.findByText(/not your current password/i)).toBeInTheDocument()
      expect(useAuthStore.getState().isAuthenticated).toBe(true)
    })
  })
})
