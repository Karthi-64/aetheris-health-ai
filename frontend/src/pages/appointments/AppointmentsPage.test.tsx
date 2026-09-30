import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Paginated } from '@/api/types'
import type { AppointmentSummary } from '@/api/appointments'
import AppointmentsPage from './AppointmentsPage'

const useAppointmentsMock = vi.fn()
vi.mock('@/api/appointments', () => ({
  useAppointments: (p: unknown) => useAppointmentsMock(p),
  useBookAppointment: () => ({ mutateAsync: vi.fn(), isPending: false }),
  appointmentKeys: { all: ['appointments'] },
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
  beforeEach(() => useAppointmentsMock.mockReset())

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
})
