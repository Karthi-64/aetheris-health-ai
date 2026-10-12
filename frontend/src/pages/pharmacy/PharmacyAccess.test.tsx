import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { AppointmentStatus, AppointmentSummary } from '@/api/appointments'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { MOCK_PERMISSIONS_BY_ROLE, navForPermissions, type Permission, type Role } from '@/lib/rbac'
import { AppointmentActions } from '@/pages/appointments/AppointmentActions'
import { signIn, signOut } from '@/test/auth'
import { installFakeApi, paged, type FakeApi } from '@/test/fakeApi'
import PharmacyIndexPage from './PharmacyIndexPage'
import { PharmacyTabs } from './PharmacyTabs'

/**
 * How Pharmacy is reached from the rest of the app: the sidebar entry, where
 * `/pharmacy` lands each role, the section tabs, and the Prescribe action on
 * a visit. Role permission sets mirror `backend/app/seeds/seed.py`.
 */

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const perms = (role: Role): Permission[] => MOCK_PERMISSIONS_BY_ROLE[role]

let fake: FakeApi

beforeEach(() => {
  fake = installFakeApi(() => paged([]))
})

afterEach(() => {
  fake.restore()
  signOut()
})

function Where() {
  return <p>at {useLocation().pathname}</p>
}

function renderIndex(role: Role) {
  signIn(perms(role))
  render(
    <MemoryRouter initialEntries={['/pharmacy']}>
      <Routes>
        <Route
          path="/pharmacy"
          element={
            <RequirePermission group="pharmacy.medicine.read">
              <PharmacyIndexPage />
            </RequirePermission>
          }
        />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('pharmacy in the sidebar', () => {
  it.each<[Role, boolean]>([
    ['hospital_admin', true],
    ['super_admin', true],
    ['doctor', true],
    ['pharmacist', true],
    ['inventory_manager', true],
    ['nurse', false],
    ['receptionist', false],
    ['billing_staff', false],
  ])('%s sees the Pharmacy entry: %s', (role, expected) => {
    const paths = navForPermissions(perms(role)).map((n) => n.to)
    expect(paths.includes('/pharmacy')).toBe(expected)
  })
})

describe('/pharmacy', () => {
  it.each<[Role, string]>([
    ['hospital_admin', '/pharmacy/prescriptions'],
    ['doctor', '/pharmacy/prescriptions'],
    ['pharmacist', '/pharmacy/prescriptions'],
    // No prescription.read: the catalog is the first screen they may open.
    ['inventory_manager', '/pharmacy/medicines'],
    ['nurse', '/dashboard'],
    ['receptionist', '/dashboard'],
  ])('sends a %s to %s, asking the API nothing', async (role, destination) => {
    renderIndex(role)

    expect(await screen.findByText(`at ${destination}`)).toBeInTheDocument()
    expect(fake.sent).toHaveLength(0)
  })
})

describe('pharmacy section tabs', () => {
  function tabsFor(role: Role) {
    signIn(perms(role))
    render(
      <MemoryRouter>
        <PharmacyTabs />
      </MemoryRouter>,
    )
    const nav = screen.queryByRole('navigation', { name: 'Pharmacy sections' })
    return nav ? within(nav).getAllByRole('link').map((a) => a.textContent) : []
  }

  it('lists every section for an admin', () => {
    expect(tabsFor('hospital_admin')).toEqual(['Prescriptions', 'Medicines', 'Purchase orders', 'Vendors'])
  })

  it('lists only prescriptions and the catalog for a doctor', () => {
    expect(tabsFor('doctor')).toEqual(['Prescriptions', 'Medicines'])
  })

  it('leaves prescriptions out for an inventory manager', () => {
    expect(tabsFor('inventory_manager')).toEqual(['Medicines', 'Purchase orders', 'Vendors'])
  })
})

describe('prescribing from a visit', () => {
  const visit = (status: AppointmentStatus): AppointmentSummary => ({
    id: 'a1',
    patient_id: 'p1',
    patient_name: 'Asha Rao',
    doctor_id: 'd1',
    doctor_name: 'Dr. Priya Sharma',
    scheduled_start: '2026-10-05T04:30:00Z',
    scheduled_end: '2026-10-05T05:00:00Z',
    status,
    type: 'new',
  })

  function renderRow(permissions: string[], status: AppointmentStatus) {
    signIn(permissions)
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter>
          <AppointmentActions appointment={visit(status)} />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    return screen.queryByRole('button', { name: 'Write prescription for Asha Rao' })
  }

  it.each<AppointmentStatus>(['booked', 'checked_in', 'in_progress', 'completed'])(
    'is offered to a doctor on a %s visit',
    (status) => {
      expect(renderRow(perms('doctor'), status)).toBeInTheDocument()
    },
  )

  it.each<AppointmentStatus>(['cancelled', 'no_show'])('is not offered on a %s visit', (status) => {
    expect(renderRow(perms('doctor'), status)).not.toBeInTheDocument()
  })

  it.each<Role>(['pharmacist', 'inventory_manager', 'nurse', 'receptionist'])(
    'is not offered to a %s, who cannot write prescriptions',
    (role) => {
      expect(renderRow(perms(role), 'in_progress')).not.toBeInTheDocument()
    },
  )

  it('needs the catalog as well as the right to prescribe', () => {
    expect(renderRow(['pharmacy.prescription.create'], 'in_progress')).not.toBeInTheDocument()
  })

  it('asks the API nothing until the form is opened', () => {
    renderRow(perms('doctor'), 'in_progress')

    expect(fake.sent).toHaveLength(0)
  })
})
