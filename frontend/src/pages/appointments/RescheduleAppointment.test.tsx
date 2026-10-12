import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { AppointmentStatus, AppointmentSummary } from '@/api/appointments'
import type { DoctorSlot } from '@/api/doctors'
import { formatTime } from '@/lib/format'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import AppointmentsPage from './AppointmentsPage'

/**
 * Rescheduling from the queue, against `PATCH /api/v1/appointments/{id}`
 * (`RescheduleAppointmentRequest`). The real hooks, permission check, `http`
 * wrapper and Axios instance run; only the network adapter is replaced by a
 * small in-memory "server" that derives each slot's status from the
 * appointments it holds — so a moved appointment frees its old slot the way
 * the real one does.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → permission sets as seeded in backend/app/seeds/seed.py.
const RECEPTIONIST = [
  'appointment.read',
  'appointment.book',
  'appointment.reschedule',
  'appointment.cancel',
  'appointment.check_in',
  'doctor.read',
  'doctor.availability.read',
]
const DOCTOR = [
  'appointment.read',
  'appointment.check_in',
  'appointment.start',
  'appointment.complete',
  'doctor.read',
  'doctor.availability.read',
]

const DAY = '2030-01-07'
/** The doctor's grid that day, on the hospital's clock (UTC+5:30). */
const GRID = ['09:30', '10:00', '10:30', '11:00'].map((t, i, all) => ({
  start: `${DAY}T${t}:00+05:30`,
  end: `${DAY}T${all[i + 1] ?? '11:30'}:00+05:30`,
}))
const [, TEN, TEN_THIRTY] = GRID

function appt(id: string, patient: string, status: AppointmentStatus = 'booked'): AppointmentSummary {
  return {
    id,
    patient_id: `p-${id}`,
    patient_name: patient,
    doctor_id: 'd1',
    doctor_name: 'Priya Sharma',
    // 09:30–10:00 in Asia/Kolkata, as the API stores it.
    scheduled_start: '2030-01-07T04:00:00Z',
    scheduled_end: '2030-01-07T04:30:00Z',
    status,
    type: 'new',
  }
}

let server: AppointmentSummary[]
/** Slot starts held by appointments that are not in the queue on screen. */
let takenElsewhere: string[]
let onLeave: string[]
let onPatch: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null
let onSlots: (() => Outcome) | null
let fake: FakeApi

const sameInstant = (a: string, b: string) => new Date(a).getTime() === new Date(b).getTime()

function slotsOn(date: string): DoctorSlot[] {
  if (date !== DAY) return []
  return GRID.map((slot) => {
    const holder = server.find(
      (a) => ['booked', 'checked_in'].includes(a.status) && sameInstant(a.scheduled_start, slot.start),
    )
    if (holder) return { ...slot, status: 'booked', appointment_id: holder.id }
    if (takenElsewhere.includes(slot.start)) return { ...slot, status: 'booked', appointment_id: 'other' }
    if (onLeave.includes(slot.start)) return { ...slot, status: 'on_leave', appointment_id: null }
    return { ...slot, status: 'available', appointment_id: null }
  })
}

function applyReschedule(config: InternalAxiosRequestConfig): Outcome {
  const id = (config.url ?? '').split('/').pop()
  const row = server.find((a) => a.id === id)
  if (!row) return fail(404, 'Appointment not found.')
  const body = bodyOf(config) as { scheduled_start: string; scheduled_end: string }
  row.scheduled_start = new Date(body.scheduled_start).toISOString()
  row.scheduled_end = new Date(body.scheduled_end).toISOString()
  return ok({ ...row })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  server = [appt('a1', 'Ravi Menon')]
  takenElsewhere = [TEN_THIRTY.start]
  onLeave = [GRID[3].start]
  onPatch = null
  onSlots = null
  fake = installFakeApi((config) => {
    const url = config.url ?? ''
    if (config.method === 'patch') return (onPatch ?? applyReschedule)(config)
    if (url === '/hospitals/current') return ok({ id: 'h1', name: 'Demo', timezone: 'Asia/Kolkata' })
    if (url === '/doctors/d1/slots') {
      if (onSlots) return onSlots()
      const date = (config.params as { date: string }).date
      return ok({ date, doctor_id: 'd1', timezone: 'Asia/Kolkata', slots: slotsOn(date) })
    }
    if (url === '/appointments') return paged(server.map((a) => ({ ...a })))
    return paged([])
  })
})

afterEach(() => {
  fake.restore()
  signOut()
})

function renderQueue(permissions: string[]) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <AppointmentsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const rowOf = (patient: string) => screen.findByRole('row', { name: new RegExp(patient) })
const patches = () => fake.requests('patch')
const slotGets = () => fake.requests('get', '/slots')
const listGets = () => fake.sent.filter((c) => c.method === 'get' && c.url === '/appointments')

/** Open the dialog for a patient and wait for the doctor's slots. */
async function openReschedule(user: ReturnType<typeof userEvent.setup>, patient = 'Ravi Menon') {
  const row = await rowOf(patient)
  await user.click(within(row).getByRole('button', { name: `Reschedule appointment for ${patient}` }))
  const dialog = await screen.findByRole('dialog')
  await within(dialog).findByRole('group', { name: 'Time slots' })
  return dialog
}

const slot = (dialog: HTMLElement, name: RegExp | string) => within(dialog).getByRole('button', { name })

describe('who can reschedule', () => {
  it('offers Reschedule to reception on a booked appointment only', async () => {
    server = [appt('a1', 'Booked Patient'), appt('a2', 'Arrived Patient', 'checked_in')]
    server[1].scheduled_start = '2030-01-07T05:30:00Z'
    renderQueue(RECEPTIONIST)

    const booked = await rowOf('Booked Patient')
    expect(within(booked).getByRole('button', { name: /^Reschedule appointment/ })).toBeInTheDocument()
    const arrived = await rowOf('Arrived Patient')
    expect(within(arrived).queryByRole('button', { name: /^Reschedule/ })).not.toBeInTheDocument()
    // Still cancellable, so the row does have actions.
    expect(within(arrived).getByRole('button', { name: /^Cancel appointment/ })).toBeInTheDocument()
  })

  it('hides Reschedule from a role without appointment.reschedule', async () => {
    renderQueue(DOCTOR)

    const row = await rowOf('Ravi Menon')
    expect(within(row).getByRole('button', { name: 'Check in Ravi Menon' })).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /^Reschedule/ })).not.toBeInTheDocument()
  })

  it('hides Reschedule from a user who cannot read the doctor\'s slots', async () => {
    renderQueue(RECEPTIONIST.filter((p) => p !== 'doctor.availability.read'))

    const row = await rowOf('Ravi Menon')
    expect(within(row).queryByRole('button', { name: /^Reschedule/ })).not.toBeInTheDocument()
  })
})

describe('the reschedule dialog', () => {
  it('opens on the appointment\'s own day and time, on the hospital\'s clock', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    expect(within(dialog).getByText(/Ravi Menon with Priya Sharma/)).toBeInTheDocument()
    // 04:00Z is 09:30 in Asia/Kolkata, whatever zone the test runs in.
    expect(within(dialog).getByText(/9:30 AM – 10:00 AM/)).toBeInTheDocument()
    expect(within(dialog).getByLabelText(/New date/)).toHaveValue(DAY)

    const [request] = slotGets()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/doctors/d1/slots')
    expect(request.params).toEqual({ date: DAY })
  })

  it('lets only a free slot be picked, and names why the others cannot', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    expect(slot(dialog, '9:30 AM, current time')).toBeDisabled()
    expect(slot(dialog, '10:30 AM, booked')).toBeDisabled()
    expect(slot(dialog, '11:00 AM, on leave')).toBeDisabled()
    expect(slot(dialog, '10:00 AM')).toBeEnabled()
    expect(within(dialog).getByText(/Asia\/Kolkata time/)).toBeInTheDocument()
  })

  it('will not go on without a new slot', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))

    expect(await within(dialog).findByText('Pick a new time slot')).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Confirm reschedule' })).not.toBeInTheDocument()
    expect(patches()).toHaveLength(0)
  })

  it('shows the current and new times and saves only once confirmed', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    await user.click(slot(dialog, '10:00 AM'))
    expect(slot(dialog, '10:00 AM')).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))

    const confirm = await within(dialog).findByRole('button', { name: 'Confirm reschedule' })
    expect(within(dialog).getByText(/9:30 AM – 10:00 AM/)).toBeInTheDocument()
    expect(within(dialog).getByText(/10:00 AM – 10:30 AM/)).toBeInTheDocument()
    // Reviewing is not saving.
    expect(patches()).toHaveLength(0)

    await user.click(confirm)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(toastSuccess.mock.calls[0][0]).toMatch(/^Appointment moved to .*10:00 AM – 10:30 AM$/)
    expect(patches()).toHaveLength(1)
    const [request] = patches()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/appointments/a1')
    // The slot's own start and end, untouched — and nothing else: the API forbids extra keys.
    expect(bodyOf(request)).toEqual({ scheduled_start: TEN.start, scheduled_end: TEN.end })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('sends the reason when one is given', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    await user.click(slot(dialog, '10:00 AM'))
    await user.type(within(dialog).getByLabelText(/Reason/), '  Patient running late  ')
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))
    await user.click(await within(dialog).findByRole('button', { name: 'Confirm reschedule' }))

    await waitFor(() => expect(patches()).toHaveLength(1))
    expect(bodyOf(patches()[0])).toEqual({
      scheduled_start: TEN.start,
      scheduled_end: TEN.end,
      reason: 'Patient running late',
    })
  })

  it('refreshes the queue and frees the old slot after a move', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)
    const listsBefore = listGets().length
    const slotsBefore = slotGets().length

    await user.click(slot(dialog, '10:00 AM'))
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))
    await user.click(await within(dialog).findByRole('button', { name: 'Confirm reschedule' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    // The row's time came from a refetch, not from a guess made in the browser.
    expect(listGets().length).toBeGreaterThan(listsBefore)
    const row = await rowOf('Ravi Menon')
    expect(within(row).getByText(formatTime(TEN.start))).toBeInTheDocument()

    const reopened = await openReschedule(user)
    expect(slotGets().length).toBeGreaterThan(slotsBefore)
    await waitFor(() => expect(slot(reopened, '9:30 AM')).toBeEnabled())
    expect(slot(reopened, '10:00 AM, current time')).toBeDisabled()
  })

  it('goes back from the review with the choice kept, and closes without saving', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    await user.click(slot(dialog, '10:00 AM'))
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))
    await user.click(await within(dialog).findByRole('button', { name: /Back/ }))

    await waitFor(() => expect(slot(dialog, '10:00 AM')).toHaveAttribute('aria-pressed', 'true'))
    await user.click(within(dialog).getByRole('button', { name: 'Keep current time' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(patches()).toHaveLength(0)
  })

  it('drops the picked slot when the day changes', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    await user.click(slot(dialog, '10:00 AM'))
    fireEvent.change(within(dialog).getByLabelText(/New date/), { target: { value: '2030-01-08' } })

    expect(await within(dialog).findByText(/no time slots on that day/)).toBeInTheDocument()
    expect(slotGets().at(-1)?.params).toEqual({ date: '2030-01-08' })
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))
    expect(await within(dialog).findByText('Pick a new time slot')).toBeInTheDocument()
  })
})

describe('when the server refuses', () => {
  async function confirmTen(user: ReturnType<typeof userEvent.setup>) {
    const dialog = await openReschedule(user)
    await user.click(slot(dialog, '10:00 AM'))
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))
    await user.click(await within(dialog).findByRole('button', { name: 'Confirm reschedule' }))
    return dialog
  }

  it('409: says the time was taken, reloads the slots and asks for another', async () => {
    onPatch = () => {
      // Another desk booked 10:00 between the slots loading and the save.
      takenElsewhere.push(TEN.start)
      return fail(409, 'The doctor already has an appointment in this window.', {
        error_code: 'DOUBLE_BOOKING',
      })
    }
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const slotsBefore = 1
    const dialog = await confirmTen(user)

    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent('Time unavailable')
    expect(alert).toHaveTextContent('Priya Sharma already has an appointment in that window')
    // Back on the picker, with the refetched day showing the slot as gone.
    await waitFor(() => expect(slot(dialog, '10:00 AM, booked')).toBeDisabled())
    expect(slotGets().length).toBeGreaterThan(slotsBefore)
    expect(within(dialog).queryByRole('button', { name: 'Confirm reschedule' })).not.toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    // The appointment itself has not moved.
    expect(server[0].scheduled_start).toBe('2030-01-07T04:00:00Z')
  })

  it('400: shows the API\'s reason and lets the user pick again', async () => {
    onPatch = () => fail(400, "The requested time is outside the doctor's availability.")
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const listsBefore = listGets().length
    const dialog = await confirmTen(user)

    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't reschedule")
    expect(alert).toHaveTextContent("The requested time is outside the doctor's availability.")
    expect(toastError).toHaveBeenCalledWith("The requested time is outside the doctor's availability.")
    expect(slot(dialog, '10:00 AM')).toHaveAttribute('aria-pressed', 'false')
    await waitFor(() => expect(listGets().length).toBeGreaterThan(listsBefore))
    expect(toastSuccess).not.toHaveBeenCalled()

    // The form is still usable: a second attempt is a fresh request.
    onPatch = null
    await user.click(slot(dialog, '10:00 AM'))
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))
    await user.click(await within(dialog).findByRole('button', { name: 'Confirm reschedule' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(patches()).toHaveLength(2)
  })

  it('400 because the patient has since checked in: the action leaves the queue', async () => {
    onPatch = () => {
      server[0].status = 'checked_in'
      return fail(400, 'Cannot move an appointment from checked_in to booked.')
    }
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    await confirmTen(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Cannot move an appointment from checked_in to booked.'),
    )
    const row = await rowOf('Ravi Menon')
    await waitFor(() =>
      expect(within(row).queryByRole('button', { name: /^Reschedule/ })).not.toBeInTheDocument(),
    )
    expect(within(row).getByText('Checked in')).toBeInTheDocument()
  })

  it('422 on the reason: shown under the field', async () => {
    onPatch = () =>
      fail(422, 'Validation failed.', {
        errors: [{ field: 'reason', message: 'String should have at most 500 characters' }],
      })
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await confirmTen(user)

    expect(await within(dialog).findByText('String should have at most 500 characters')).toBeInTheDocument()
    expect(within(dialog).getByLabelText(/Reason/)).toBeInvalid()
  })

  it('403: says so and closes', async () => {
    onPatch = () => fail(403, 'Forbidden.')
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    await confirmTen(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("You don't have permission to reschedule appointments."),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('500: keeps the review open so the same change can be retried', async () => {
    onPatch = () => fail(500, 'Traceback: internal detail')
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await confirmTen(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't reschedule the appointment. Please try again."),
    )
    expect(within(dialog).getByRole('button', { name: 'Confirm reschedule' })).toBeEnabled()
  })
})

describe('loading and duplicate protection', () => {
  it('offers a retry when the slots cannot be loaded', async () => {
    onSlots = () => fail(500, 'boom')
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)

    expect(await within(dialog).findByText(/time slots couldn't be loaded/)).toBeInTheDocument()
    onSlots = null
    await user.click(within(dialog).getByRole('button', { name: /Retry/ }))

    expect(await within(dialog).findByRole('button', { name: '10:00 AM' })).toBeEnabled()
  })

  it('sends one request however many times Confirm is pressed', async () => {
    let release: (outcome: Outcome) => void = () => {}
    onPatch = () => new Promise<Outcome>((resolve) => (release = resolve))
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openReschedule(user)
    await user.click(slot(dialog, '10:00 AM'))
    await user.click(within(dialog).getByRole('button', { name: /Review change/ }))
    const confirm = await within(dialog).findByRole('button', { name: 'Confirm reschedule' })
    const form = confirm.closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    const busy = await within(dialog).findByRole('button', { name: 'Rescheduling…' })
    expect(busy).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: /Back/ })).toBeDisabled()
    expect(patches()).toHaveLength(1)

    release(ok({ ...server[0] }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(patches()).toHaveLength(1)
  })
})
