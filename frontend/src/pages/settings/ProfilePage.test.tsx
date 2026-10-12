import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ProfilePage from './ProfilePage'
import { useAuthStore } from '@/store/auth-store'
import { ApiError } from '@/api/types'
import { toast } from 'sonner'

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

  it('puts a 422 under the field the API names and anything else above the form', async () => {
    const user = userEvent.setup()
    patch.mockRejectedValue(
      new ApiError('Validation failed.', 'VALIDATION_ERROR', 422, [
        { field: 'phone', message: "String should match pattern '^\\+?[1-9]\\d{1,14}$'" },
        { field: 'email', message: 'Extra inputs are not permitted' },
      ]),
    )
    renderPage()

    const firstName = await screen.findByDisplayValue('Asha')
    await user.type(firstName, 'x')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByText(/String should match pattern/)).toBeInTheDocument()
    expect(screen.getByDisplayValue('+919812345678')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('Extra inputs are not permitted')).toBeInTheDocument()
    expect(toast.error).toHaveBeenCalledWith('Check the highlighted fields and try again.')
  })

  it('shows a plain sentence, not transport text, when the save cannot reach the server', async () => {
    const user = userEvent.setup()
    patch.mockRejectedValue(new ApiError('Network Error', 'network_error'))
    renderPage()

    await user.type(await screen.findByDisplayValue('Asha'), 'x')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Could not save your profile. Please try again.'),
    )
  })

  it('sends one save when the form is submitted twice in the same tick', async () => {
    const user = userEvent.setup()
    let finish: (value: typeof PROFILE) => void = () => {}
    patch.mockImplementation(() => new Promise((resolve) => (finish = resolve)))
    renderPage()

    const firstName = await screen.findByDisplayValue('Asha')
    await user.type(firstName, 'x')
    const form = firstName.closest('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1))
    finish({ ...PROFILE, first_name: 'Ashax' })
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1))
    expect(patch).toHaveBeenCalledTimes(1)
  })
})
