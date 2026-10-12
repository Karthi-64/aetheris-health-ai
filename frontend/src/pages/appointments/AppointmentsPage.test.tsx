import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Paginated } from '@/api/types'
import type { AppointmentSummary } from '@/api/appointments'
import AppointmentsPage from './AppointmentsPage'

const { canMock } = vi.hoisted(() => ({ canMock: vi.fn() }))
const useAppointmentsMock = vi.fn()

// Permission gate (PR #29 review finding 10). Defaults to allowing everything
// so the layout tests below are unaffected; the gating test narrows it.
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    can: (p: string) => canMock(p),
    canAny: () => true,
    nav: [],
    role: undefined,
  }),
}))
vi.mock('@/api/appointments', () => ({
  useAppointments: (p: unknown) => useAppointmentsMock(p),
  useBookAppointment: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useAppointmentTransition: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useCancelAppointment: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useMarkNoShow: () => ({ mutateAsync: vi.fn(), isPending: false }),
  appointmentKeys: { all: ['appointments'] },
}))
// Each row's (closed) lab-order dialog holds its mutation.
vi.mock('@/api/lab', () => ({
  useCreateLabOrder: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))
vi.mock('@/api/pharmacy', () => ({
  useCreatePrescription: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useMedicines: () => ({ data: undefined, isPending: false, isError: false }),
}))
// The queue header renders the (closed) BookAppointmentDialog, whose hooks run.
vi.mock('@/api/doctors', () => ({ useDoctors: () => ({ data: { items: [] } }) }))
vi.mock('@/api/patients', () => ({ usePatients: () => ({ data: { items: [] } }) }))

function result(overrides: Record<string, unknown>) {
  return {
    data: undefined,
    isPending: false,
    isError: false,
    isFetching: false,
    refetch: vi.fn(),
    ...overrides,
  }
}

const page = (items: AppointmentSummary[], total = items.length): Paginated<AppointmentSummary> => ({
  items,
  pagination: { page: 1, pageSize: 25, total, totalPages: Math.max(1, Math.ceil(total / 25)) },
})

const appt: AppointmentSummary = {
  id: 'a1',
  patient_id: 'p1',
  patient_name: 'Ravi Menon',
  doctor_id: 'd1',
  doctor_name: 'Dr. Anita Chen',
  scheduled_start: '2026-08-28T09:30:00Z',
  scheduled_end: '2026-08-28T09:45:00Z',
  status: 'booked',
  type: 'new',
}

function renderPage() {
  return render(
    <MemoryRouter>
      <AppointmentsPage />
    </MemoryRouter>,
  )
}

describe('AppointmentsPage', () => {
  beforeEach(() => {
    useAppointmentsMock.mockReset()
    canMock.mockReset()
    canMock.mockReturnValue(true)
  })

  it('renders appointment rows from the API response', () => {
    useAppointmentsMock.mockReturnValue(result({ data: page([appt]) }))
    renderPage()
    expect(screen.getByText('Ravi Menon')).toBeInTheDocument()
    expect(screen.getByText('Dr. Anita Chen')).toBeInTheDocument()
    expect(screen.getByText('Booked')).toBeInTheDocument()
  })

  it('shows the empty state when there are no appointments', () => {
    useAppointmentsMock.mockReturnValue(result({ data: page([], 0) }))
    renderPage()
    expect(screen.getByText('No appointments today')).toBeInTheDocument()
  })

  it('shows an error state with a retry action', () => {
    useAppointmentsMock.mockReturnValue(result({ isError: true }))
    renderPage()
    expect(screen.getByText("Couldn't load appointments")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('hides Book for a role without appointment.book', () => {
    // A Doctor lacks appointment.book; the trigger must not render at all.
    canMock.mockImplementation((p: string) => p !== 'appointment.book')
    useAppointmentsMock.mockReturnValue(result({ data: page([appt]) }))
    renderPage()
    expect(screen.queryByRole('button', { name: /^book$/i })).not.toBeInTheDocument()
  })

  it('shows Book for a role that holds appointment.book', () => {
    useAppointmentsMock.mockReturnValue(result({ data: page([appt]) }))
    renderPage()
    expect(screen.getByRole('button', { name: /^book$/i })).toBeInTheDocument()
  })
})
