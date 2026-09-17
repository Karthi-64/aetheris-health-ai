import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ChangePasswordDialog } from './ChangePasswordDialog'
import { ApiError } from '@/api/types'

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
})
