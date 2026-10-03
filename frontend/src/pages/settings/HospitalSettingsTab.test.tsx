import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { HospitalSettingsTab } from './HospitalSettingsTab'
import type { HospitalSettings } from '@/api/hospitals'

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
})
