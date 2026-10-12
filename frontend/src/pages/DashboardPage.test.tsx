import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import {
  adminDashboardFixture,
  billingDashboardFixture,
  doctorDashboardFixture,
  receptionDashboardFixture,
} from '@/test/reportFixtures'
import DashboardPage from './DashboardPage'

const { canMock } = vi.hoisted(() => ({ canMock: vi.fn() }))
const usePatientsMock = vi.fn()
const useDoctorsMock = vi.fn()
const useAppointmentsMock = vi.fn()
const useAdminDashboardMock = vi.fn()
const useBillingDashboardMock = vi.fn()
const useReceptionDashboardMock = vi.fn()
const useDoctorDashboardMock = vi.fn()

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    can: (p: string) => canMock(p),
    canAny: () => true,
    nav: [],
    role: undefined,
  }),
}))
vi.mock('@/api/patients', () => ({
  usePatients: (p: unknown, o: unknown) => usePatientsMock(p, o),
}))
vi.mock('@/api/doctors', () => ({
  useDoctors: (p: unknown, o: unknown) => useDoctorsMock(p, o),
}))
vi.mock('@/api/appointments', () => ({
  useAppointments: (p: unknown, o: unknown) => useAppointmentsMock(p, o),
}))
// Only the four dashboard hooks are replaced; the error helpers stay real.
vi.mock('@/api/reports', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/reports')>()),
  useAdminDashboard: (o: unknown) => useAdminDashboardMock(o),
  useBillingDashboard: (o: unknown) => useBillingDashboardMock(o),
  useReceptionDashboard: (o: unknown) => useReceptionDashboardMock(o),
  useDoctorDashboard: (o: unknown) => useDoctorDashboardMock(o),
}))
// The quick-action dialogs have their own tests and their own data hooks.
vi.mock('@/pages/patients/RegisterPatientDialog', () => ({ RegisterPatientDialog: () => null }))
vi.mock('@/components/appointments/BookAppointmentDialog', () => ({
  BookAppointmentDialog: () => null,
}))
// Covered by its own tests; it has its own data hooks.
vi.mock('@/components/billing/InvoicesAwaitingPayment', () => ({
  InvoicesAwaitingPayment: () => null,
}))

const count = (total: number) => ({
  data: { items: [], pagination: { page: 1, pageSize: 1, total, totalPages: 1 } },
  isPending: false,
  isError: false,
})
/** What react-query returns for a disabled query: pending forever, never an error. */
const disabled = { data: undefined, isPending: true, isError: false, error: null }
const loaded = (data: unknown) => ({ data, isPending: false, isError: false, error: null })

type Options = { enabled: boolean }

const DASHBOARDS = [
  { mock: useAdminDashboardMock, fixture: adminDashboardFixture },
  { mock: useBillingDashboardMock, fixture: billingDashboardFixture },
  { mock: useReceptionDashboardMock, fixture: receptionDashboardFixture },
  { mock: useDoctorDashboardMock, fixture: doctorDashboardFixture },
]

/** Every call a dashboard hook received, by whether it was allowed to request. */
const enabledCalls = (mock: typeof useAdminDashboardMock) =>
  [...new Set(mock.mock.calls.map(([o]) => (o as Options).enabled))]

function renderWith(permissions: string[]) {
  canMock.mockImplementation((p: string) => permissions.includes(p))
  usePatientsMock.mockImplementation((_p: unknown, o: Options) => (o.enabled ? count(137) : disabled))
  useDoctorsMock.mockImplementation((_p: unknown, o: Options) => (o.enabled ? count(5) : disabled))
  useAppointmentsMock.mockImplementation((_p: unknown, o: Options) =>
    o.enabled ? count(0) : disabled,
  )
  for (const { mock, fixture } of DASHBOARDS) {
    mock.mockImplementation((o: Options) => (o.enabled ? loaded(fixture) : disabled))
  }
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  )
}

/**
 * The dashboard is every role's home page, but it used to request patients,
 * doctors and appointments for everyone. A role without those permissions got
 * 403s and a permanent "Couldn't load today's appointments" alert
 * (PR #29 re-review).
 */
describe('DashboardPage', () => {
  beforeEach(() => {
    canMock.mockReset()
    usePatientsMock.mockReset()
    useDoctorsMock.mockReset()
    useAppointmentsMock.mockReset()
    for (const { mock } of DASHBOARDS) mock.mockReset()
  })

  it('shows the three list tiles and the queue to a user with no report code (a nurse)', () => {
    renderWith(['patient.read', 'doctor.read', 'appointment.read'])

    // No dashboard is requested for them, and no role section is shown.
    for (const { mock } of DASHBOARDS) expect(enabledCalls(mock)).toEqual([false])
    for (const heading of ['Hospital overview', 'Billing', 'Front desk', 'My day']) {
      expect(screen.queryByRole('heading', { name: heading })).not.toBeInTheDocument()
    }

    expect(screen.getByText('Total patients')).toBeInTheDocument()
    expect(screen.getByText('Doctors')).toBeInTheDocument()
    expect(screen.getAllByText("Today's appointments").length).toBeGreaterThan(0)
    for (const mock of [usePatientsMock, useDoctorsMock, useAppointmentsMock]) {
      expect(mock.mock.calls[0][1]).toEqual({ enabled: true })
    }
  })

  it('does not request or show appointments for a user with no appointment.read', () => {
    // No appointment.read and no report code: the two list tiles they may read.
    renderWith(['patient.read', 'doctor.read', 'invoice.read'])

    expect(useAppointmentsMock.mock.calls[0][1]).toEqual({ enabled: false })
    expect(screen.queryByText("Today's appointments")).not.toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load/)).not.toBeInTheDocument()
    expect(screen.getByText('Total patients')).toBeInTheDocument()
    expect(screen.getByText('Doctors')).toBeInTheDocument()
  })

  it('requests nothing and shows no error for a lab technician', () => {
    // Seeded Lab Technician holds none of the three read permissions.
    renderWith(['lab.order.read'])

    for (const mock of [usePatientsMock, useDoctorsMock, useAppointmentsMock]) {
      expect(mock.mock.calls[0][1]).toEqual({ enabled: false })
    }
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
    expect(screen.queryByText('Doctors')).not.toBeInTheDocument()
    expect(screen.queryByText("Today's appointments")).not.toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load/)).not.toBeInTheDocument()
    // The greeting is still there: the page is not blank.
    expect(screen.getByText(/Good (morning|afternoon|evening)/)).toBeInTheDocument()
    for (const { mock } of DASHBOARDS) expect(enabledCalls(mock)).toEqual([false])
  })

  it('shows a hospital admin the Admin and Billing sections in place of the list tiles', () => {
    // Seeded Hospital Admin holds every report code, to be able to assign them.
    renderWith([
      'patient.read',
      'doctor.read',
      'appointment.read',
      'report.admin.read',
      'report.billing.read',
      'report.reception.read',
      'report.doctor.read',
      'report.export',
    ])

    expect(enabledCalls(useAdminDashboardMock)).toContain(true)
    expect(enabledCalls(useBillingDashboardMock)).toContain(true)
    // Holding the code is not enough: the personal sections give way to Admin.
    expect(enabledCalls(useReceptionDashboardMock)).toEqual([false])
    expect(enabledCalls(useDoctorDashboardMock)).toEqual([false])
    expect(
      screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent),
    ).toEqual(['Hospital overview', 'Billing', "Today's appointments"])
    expect(screen.getByText('New patients this month')).toBeInTheDocument()
    expect(screen.getByText('Discounts awaiting approval')).toBeInTheDocument()
    // The list counts are replaced, not shown beside the sections, and not requested.
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
    expect(screen.queryByText('Doctors')).not.toBeInTheDocument()
    expect(usePatientsMock.mock.calls[0][1]).toEqual({ enabled: false })
    expect(useDoctorsMock.mock.calls[0][1]).toEqual({ enabled: false })
    // The queue asks for the hospital's day, as the dashboard reported it.
    expect(useAppointmentsMock.mock.calls[0]).toEqual([
      { appointment_date: adminDashboardFixture.meta.today, page: 1, page_size: 25 },
      { enabled: true },
    ])
  })

  it('shows billing staff the Billing section only', () => {
    renderWith(['patient.read', 'doctor.read', 'invoice.read', 'report.billing.read', 'report.export'])

    expect(enabledCalls(useBillingDashboardMock)).toContain(true)
    for (const mock of [useAdminDashboardMock, useReceptionDashboardMock, useDoctorDashboardMock]) {
      expect(enabledCalls(mock)).toEqual([false])
    }
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual(['Billing'])
    for (const label of ['Unpaid invoices', 'Billed today', 'Billed this week', 'Billed this month']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
    expect(useAppointmentsMock.mock.calls[0][1]).toEqual({ enabled: false })
  })

  it('shows a receptionist the Front desk section only', () => {
    renderWith(['patient.read', 'doctor.read', 'appointment.read', 'report.reception.read'])

    expect(enabledCalls(useReceptionDashboardMock)).toContain(true)
    for (const mock of [useAdminDashboardMock, useBillingDashboardMock, useDoctorDashboardMock]) {
      expect(enabledCalls(mock)).toEqual([false])
    }
    expect(
      screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent),
    ).toEqual(['Front desk', "Today's appointments"])
    expect(screen.getByText('Walk-ins waiting')).toBeInTheDocument()
    expect(screen.getByText('Possible no-shows')).toBeInTheDocument()
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
  })

  it('shows a doctor the My day section only', () => {
    renderWith(['patient.read', 'doctor.read', 'appointment.read', 'report.doctor.read'])

    expect(enabledCalls(useDoctorDashboardMock)).toContain(true)
    for (const mock of [useAdminDashboardMock, useBillingDashboardMock, useReceptionDashboardMock]) {
      expect(enabledCalls(mock)).toEqual([false])
    }
    expect(
      screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent),
    ).toEqual(['My day', "Today's appointments"])
    for (const label of ['My appointments today', 'My patients', 'My week']) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
  })

  it('keeps the queue waiting, and unrequested, until the hospital day is known', () => {
    canMock.mockImplementation((p: string) => ['appointment.read', 'report.reception.read'].includes(p))
    useAppointmentsMock.mockReturnValue(disabled)
    usePatientsMock.mockReturnValue(disabled)
    useDoctorsMock.mockReturnValue(disabled)
    for (const { mock } of DASHBOARDS) mock.mockReturnValue(disabled)
    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(useAppointmentsMock.mock.calls[0]).toEqual([
      { appointment_date: undefined, page: 1, page_size: 25 },
      { enabled: false },
    ])
    // Skeleton tiles, not placeholder numbers.
    expect(screen.getByRole('status', { name: "Loading Today's appointments" })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Loading Walk-ins waiting' })).toBeInTheDocument()
    expect(screen.queryByText('0')).not.toBeInTheDocument()
  })
})
