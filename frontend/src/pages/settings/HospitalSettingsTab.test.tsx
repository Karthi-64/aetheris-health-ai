import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { HospitalSettingsTab } from './HospitalSettingsTab'
import type { HospitalSettings } from '@/api/hospitals'
import { ApiError } from '@/api/types'
import { toast } from 'sonner'

const { mutateAsync } = vi.hoisted(() => ({ mutateAsync: vi.fn() }))

vi.mock('@/api/hospitals', () => ({
  useHospitalSettings: () => ({ data: settings, isLoading: false, isError: false, refetch: vi.fn() }),
  useUpdateHospitalSettings: () => ({ mutateAsync, isPending: false }),
}))
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, canAny: () => true, nav: [], role: undefined }),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

// A seeded demo hospital: the address still uses the legacy `street`/`zip`
// keys, plus an unknown `landmark` the form does not render.
const settings: HospitalSettings = {
  id: 'h1',
  name: 'Demo Hospital & Clinic',
  slug: 'demo-hospital',
  address: {
    street: '123 Healthcare Avenue',
    city: 'Bangalore',
    state: 'Karnataka',
    zip: '560001',
    country: 'India',
    landmark: 'Near the park',
  },
  phone: '+918012345678',
  email: 'info@demohospital.com',
  logo_url: null,
  timezone: 'Asia/Kolkata',
  currency: 'INR',
  locale: 'en-IN',
  is_active: true,
  tax_id: null,
  settings: {},
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
}

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <HospitalSettingsTab />
    </QueryClientProvider>,
  )
}

describe('HospitalSettingsTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mutateAsync.mockResolvedValue({})
  })

  it('reads the legacy address keys into the form', () => {
    renderTab()
    expect(screen.getByLabelText(/address line/i)).toHaveValue('123 Healthcare Avenue')
    expect(screen.getByLabelText(/postal code/i)).toHaveValue('560001')
  })

  it('writes one canonical key set and drops street/zip, keeping unknown keys', async () => {
    const actor = userEvent.setup()
    renderTab()
    await actor.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled())
    const payload = mutateAsync.mock.calls[0][0]
    expect(payload.address).toMatchObject({
      line1: '123 Healthcare Avenue',
      postal_code: '560001',
      city: 'Bangalore',
      landmark: 'Near the park',
    })
    expect(payload.address).not.toHaveProperty('street')
    expect(payload.address).not.toHaveProperty('zip')
  })

  it('sends null to clear a nullable field (email)', async () => {
    const actor = userEvent.setup()
    renderTab()
    await actor.clear(screen.getByLabelText(/contact email/i))
    await actor.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled())
    expect(mutateAsync.mock.calls[0][0].email).toBeNull()
  })

  it('clears an address part when its input is emptied', async () => {
    const actor = userEvent.setup()
    renderTab()
    await actor.clear(screen.getByLabelText(/address line/i))
    await actor.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled())
    expect(mutateAsync.mock.calls[0][0].address).not.toHaveProperty('line1')
  })

  it('puts a 422 under the input the API names and the rest above the form', async () => {
    const user = userEvent.setup()
    mutateAsync.mockRejectedValue(
      new ApiError('Validation failed.', 'VALIDATION_ERROR', 422, [
        { field: 'phone', message: 'String should match pattern' },
        { field: 'address', message: 'Value error, Address values must be text' },
      ]),
    )
    renderTab()

    const phone = screen.getByLabelText('Contact phone')
    await user.type(phone, 'x')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByText('String should match pattern')).toBeInTheDocument()
    expect(phone).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('Address values must be text')).toBeInTheDocument()
    expect(toast.error).toHaveBeenCalledWith("Couldn't save the settings. Check the highlighted fields.")

    // Editing the rejected input clears its message.
    await user.type(phone, '1')
    expect(screen.queryByText('String should match pattern')).not.toBeInTheDocument()
  })

  it("reports the API's refusal, and a plain sentence when the server fails", async () => {
    const user = userEvent.setup()
    renderTab()

    mutateAsync.mockRejectedValueOnce(new ApiError('Permission denied.', 'PERMISSION_DENIED', 403))
    await user.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Permission denied.'))

    mutateAsync.mockRejectedValueOnce(new ApiError('Request failed with status code 500', 'network_error', 500))
    await user.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Could not save the settings. Please try again.'),
    )
  })

  it('sends one save when Save is clicked twice before the button disables', async () => {
    let finish: () => void = () => {}
    mutateAsync.mockImplementation(() => new Promise<void>((resolve) => (finish = resolve)))
    renderTab()

    const save = screen.getByRole('button', { name: /save changes/i })
    save.click()
    save.click()

    expect(mutateAsync).toHaveBeenCalledTimes(1)
    finish()
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1))
  })
})
