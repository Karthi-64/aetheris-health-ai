import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ProfilePage from './ProfilePage'
import { useAuthStore } from '@/store/auth-store'

const { get, patch, post } = vi.hoisted(() => ({
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

vi.mock('@/api/http', () => ({ http: { get, patch, post } }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const PROFILE = {
  id: 'u1',
  email: 'nurse@hospital.test',
  first_name: 'Asha',
  last_name: 'Rao',
  phone: '+919812345678',
  status: 'active',
  hospital_id: 'h1',
  roles: [{ id: 'r1', name: 'Nurse', description: null, is_system: true }],
  mfa_enabled: false,
  last_login_at: null,
  password_changed_at: null,
  created_at: null,
  updated_at: null,
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ProfilePage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProfilePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    get.mockResolvedValue(PROFILE)
    useAuthStore.getState().logout()
  })

  it('shows the signed-in user their own details', async () => {
    renderPage()

    expect(await screen.findByDisplayValue('Asha')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Rao')).toBeInTheDocument()
    expect(screen.getByDisplayValue('+919812345678')).toBeInTheDocument()
    expect(screen.getByText('nurse@hospital.test')).toBeInTheDocument()
    expect(screen.getByText('Nurse')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/users/me')
  })

  it('saves edited fields through PATCH /users/me', async () => {
    const user = userEvent.setup()
    patch.mockResolvedValue({ ...PROFILE, first_name: 'Asha M.' })
    renderPage()

    const firstName = await screen.findByDisplayValue('Asha')
    await user.clear(firstName)
    await user.type(firstName, 'Asha M.')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith('/users/me', {
        first_name: 'Asha M.',
        last_name: 'Rao',
        phone: '+919812345678',
      }),
    )
  })

  it('omits an emptied phone rather than sending "", which the API rejects', async () => {
    const user = userEvent.setup()
    patch.mockResolvedValue({ ...PROFILE, phone: null })
    renderPage()

    const phone = await screen.findByDisplayValue('+919812345678')
    await user.clear(phone)
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(patch).toHaveBeenCalled())
    expect(patch.mock.calls[0][1]).toEqual({
      first_name: 'Asha',
      last_name: 'Rao',
      phone: undefined,
    })
  })

  it('rejects a malformed phone number before it reaches the API', async () => {
    const user = userEvent.setup()
    renderPage()

    const phone = await screen.findByDisplayValue('+919812345678')
    await user.clear(phone)
    await user.type(phone, 'not-a-number')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByText(/E\.164 format/i)).toBeInTheDocument()
    expect(patch).not.toHaveBeenCalled()
  })

  it('updates the display name in the auth store so the sidebar follows', async () => {
    const user = userEvent.setup()
    useAuthStore.getState().setAuth(
      { id: 'u1', name: 'Asha Rao', email: 'nurse@hospital.test', permissions: ['patient.read'] },
      'access-token',
    )
    patch.mockResolvedValue({ ...PROFILE, first_name: 'Asha M.' })
    renderPage()

    const firstName = await screen.findByDisplayValue('Asha')
    await user.clear(firstName)
    await user.type(firstName, 'Asha M.')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(useAuthStore.getState().user?.name).toBe('Asha M. Rao'))
    // The session's permissions must survive a profile edit untouched.
    expect(useAuthStore.getState().user?.permissions).toEqual(['patient.read'])
  })
})
