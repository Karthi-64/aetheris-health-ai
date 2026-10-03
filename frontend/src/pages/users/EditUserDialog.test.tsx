import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { EditUserDialog } from './EditUserDialog'
import type { ManagedUser } from '@/api/users'

// `useUpdateUser` ultimately calls `http.patch` — asserting the body there
// proves what actually goes on the wire (PR #29 review finding 9).
const { patch } = vi.hoisted(() => ({ patch: vi.fn() }))
vi.mock('@/api/http', () => ({
  http: { getPaginated: vi.fn(), get: vi.fn(), post: vi.fn(), patch, delete: vi.fn() },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const staff: ManagedUser = {
  id: 'u1',
  email: 'staff@hospital.test',
  first_name: 'Asha',
  last_name: 'Rao',
  phone: '+919812345678',
  status: 'active',
  hospital_id: 'h1',
  roles: [],
  mfa_enabled: false,
  last_login_at: null,
  password_changed_at: null,
  created_at: null,
  updated_at: null,
}

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <EditUserDialog user={staff} onClose={vi.fn()} />
    </QueryClientProvider>,
  )
}

describe('EditUserDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    patch.mockResolvedValue({})
  })

  it('sends null when the phone is emptied so the number is actually cleared', async () => {
    const actor = userEvent.setup()
    renderDialog()

    const phone = screen.getByLabelText(/phone/i)
    expect(phone).toHaveValue('+919812345678')

    await actor.clear(phone)
    await actor.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith(
        '/users/u1',
        expect.objectContaining({ phone: null }),
      ),
    )
  })

  it('keeps a changed phone number as-is', async () => {
    const actor = userEvent.setup()
    renderDialog()

    const phone = screen.getByLabelText(/phone/i)
    await actor.clear(phone)
    await actor.type(phone, '+919800000001')
    await actor.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith(
        '/users/u1',
        expect.objectContaining({ phone: '+919800000001' }),
      ),
    )
  })
})
