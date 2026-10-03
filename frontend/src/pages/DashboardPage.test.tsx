import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import DashboardPage from './DashboardPage'

const { canMock } = vi.hoisted(() => ({ canMock: vi.fn() }))
const usePatientsMock = vi.fn()
const useDoctorsMock = vi.fn()
const useAppointmentsMock = vi.fn()

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
// The quick-action dialogs have their own tests and their own data hooks.
vi.mock('@/pages/patients/RegisterPatientDialog', () => ({ RegisterPatientDialog: () => null }))
vi.mock('@/pages/appointments/BookAppointmentDialog', () => ({ BookAppointmentDialog: () => null }))

const count = (total: number) => ({
  data: { items: [], pagination: { page: 1, pageSize: 1, total, totalPages: 1 } },
  isPending: false,
  isError: false,
})
/** What react-query returns for a disabled query: pending forever, never an error. */
const disabled = { data: undefined, isPending: true, isError: false }

type Options = { enabled: boolean }

function renderWith(permissions: string[]) {
  canMock.mockImplementation((p: string) => permissions.includes(p))
  usePatientsMock.mockImplementation((_p: unknown, o: Options) => (o.enabled ? count(137) : disabled))
  useDoctorsMock.mockImplementation((_p: unknown, o: Options) => (o.enabled ? count(5) : disabled))
  useAppointmentsMock.mockImplementation((_p: unknown, o: Options) =>
    o.enabled ? count(0) : disabled,
  )
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
  })

  it('shows every tile and the queue to a user who may read all three', () => {
    renderWith(['patient.read', 'doctor.read', 'appointment.read'])

    expect(screen.getByText('Total patients')).toBeInTheDocument()
    expect(screen.getByText('Doctors')).toBeInTheDocument()
    expect(screen.getAllByText("Today's appointments").length).toBeGreaterThan(0)
    for (const mock of [usePatientsMock, useDoctorsMock, useAppointmentsMock]) {
      expect(mock.mock.calls[0][1]).toEqual({ enabled: true })
    }
  })

  it('does not request or show appointments for billing staff', () => {
    // Seeded Billing Staff: patient.read and doctor.read, no appointment.read.
    renderWith(['patient.read', 'doctor.read', 'invoice.read'])

    expect(useAppointmentsMock.mock.calls[0][1]).toEqual({ enabled: false })
    expect(screen.queryByText("Today's appointments")).not.toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load/)).not.toBeInTheDocument()
    expect(screen.getByText('Total patients')).toBeInTheDocument()
    expect(screen.getByText('Doctors')).toBeInTheDocument()
  })

  it('requests nothing and shows no error for a lab technician', () => {
    // Seeded Lab Technician holds none of the three read permissions.
    renderWith(['lab.read'])

    for (const mock of [usePatientsMock, useDoctorsMock, useAppointmentsMock]) {
      expect(mock.mock.calls[0][1]).toEqual({ enabled: false })
    }
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
    expect(screen.queryByText('Doctors')).not.toBeInTheDocument()
    expect(screen.queryByText("Today's appointments")).not.toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load/)).not.toBeInTheDocument()
    // The greeting is still there: the page is not blank.
    expect(screen.getByText(/Good morning/)).toBeInTheDocument()
  })
})
