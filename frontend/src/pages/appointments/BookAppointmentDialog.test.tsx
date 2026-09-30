import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BookAppointmentDialog } from './BookAppointmentDialog'

vi.mock('@/api/appointments', () => ({
  useBookAppointment: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))
vi.mock('@/api/doctors', () => ({ useDoctors: () => ({ data: { items: [] } }) }))
vi.mock('@/api/patients', () => ({ usePatients: () => ({ data: { items: [] } }) }))

describe('BookAppointmentDialog', () => {
  it('opens and blocks submit with validation errors when required fields are empty', async () => {
    const user = userEvent.setup()
    render(<BookAppointmentDialog trigger={<button>Open booking</button>} />)

    await user.click(screen.getByRole('button', { name: 'Open booking' }))
    // Dialog is open with the form.
    expect(screen.getByText('Book appointment')).toBeInTheDocument()

    // Submit empty -> zod validation surfaces required-field errors.
    await user.click(screen.getByRole('button', { name: /^Book$/ }))
    expect(await screen.findByText('Choose a patient')).toBeInTheDocument()
    expect(screen.getByText('Choose a doctor')).toBeInTheDocument()
  })
})
