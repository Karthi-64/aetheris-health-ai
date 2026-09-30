import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { Paginated } from '@/api/types'
import type { DoctorSummary } from '@/api/doctors'
import DoctorsPage from './DoctorsPage'

const useDoctorsMock = vi.fn()
vi.mock('@/api/doctors', () => ({
  useDoctors: (p: unknown) => useDoctorsMock(p),
  doctorKeys: { all: ['doctors'] },
}))
vi.mock('@/api/departments', () => ({
  useDepartments: () => ({ data: [] }),
}))

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

const page = (items: DoctorSummary[], total = items.length): Paginated<DoctorSummary> => ({
  items,
  pagination: { page: 1, pageSize: 25, total, totalPages: Math.max(1, Math.ceil(total / 25)) },
})

const chen: DoctorSummary = {
  id: 'd1',
  user_id: 'u1',
  full_name: 'Dr. Anita Chen',
  specialization: 'Cardiology',
  department_id: 'dep1',
  department_name: 'Cardiology',
  consultation_fee: '750.00',
  status: 'active',
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DoctorsPage />
    </MemoryRouter>,
  )
}

describe('DoctorsPage', () => {
  beforeEach(() => useDoctorsMock.mockReset())

  it('renders doctor rows from the API response', () => {
    useDoctorsMock.mockReturnValue(result({ data: page([chen]) }))
    renderPage()
    expect(screen.getByText('Dr. Anita Chen')).toBeInTheDocument()
    expect(screen.getAllByText('Cardiology').length).toBeGreaterThan(0)
  })

  it('shows the empty state when there are no doctors', () => {
    useDoctorsMock.mockReturnValue(result({ data: page([], 0) }))
    renderPage()
    expect(screen.getByText('No doctors yet')).toBeInTheDocument()
  })

  it('shows an error state with a retry action', () => {
    useDoctorsMock.mockReturnValue(result({ isError: true }))
    renderPage()
    expect(screen.getByText("Couldn't load doctors")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })
})
