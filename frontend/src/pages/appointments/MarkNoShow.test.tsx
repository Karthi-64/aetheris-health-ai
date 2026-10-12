import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { AppointmentStatus, AppointmentSummary } from '@/api/appointments'
import { signIn, signOut } from '@/test/auth'
import { fail, installFakeApi, ok, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import AppointmentsPage from './AppointmentsPage'

/**
 * Marking a no-show from the queue, against
 * `POST /api/v1/appointments/{id}/no-show` — no body, guarded by
 * `appointment.cancel`, accepted from `booked` or `checked_in`. The real
 * hooks, permission check, `http` wrapper and Axios instance run; only the
 * network adapter is replaced by a small in-memory "server".
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → permission sets as seeded in backend/app/seeds/seed.py.
const RECEPTIONIST = ['appointment.read', 'appointment.book', 'appointment.cancel', 'appointment.check_in']
const DOCTOR = ['appointment.read', 'appointment.check_in', 'appointment.start', 'appointment.complete']
const NURSE = ['appointment.read', 'appointment.check_in']

function appt(
  id: string,
  patient: string,
  status: AppointmentStatus = 'booked',
  start = '2020-01-07T04:00:00Z',
): AppointmentSummary {
  return {
    id,
    patient_id: `p-${id}`,
    patient_name: patient,
    doctor_id: 'd1',
    doctor_name: 'Priya Sharma',
    scheduled_start: start,
    scheduled_end: new Date(new Date(start).getTime() + 30 * 60_000).toISOString(),
    status,
    type: 'new',
  }
}

let server: AppointmentSummary[]
let onPost: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null
let fake: FakeApi

/** The server's state machine for this endpoint (`ALLOWED_TRANSITIONS`). */
function applyNoShow(config: InternalAxiosRequestConfig): Outcome {
  const [, id] = /^\/appointments\/([^/]+)\/no-show$/.exec(config.url ?? '') ?? []
  const row = server.find((a) => a.id === id)
  if (!row) return fail(404, 'Appointment not found.')
  if (row.status !== 'booked' && row.status !== 'checked_in') {
    return fail(400, `Cannot move an appointment from '${row.status}' to 'no_show'.`)
  }
  row.status = 'no_show'
  return ok({ ...row })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  server = [appt('a1', 'Ravi Menon')]
  onPost = null
  fake = installFakeApi((config) => {
    if (config.method === 'post') return (onPost ?? applyNoShow)(config)
    if (config.url === '/appointments') return paged(server.map((a) => ({ ...a })))
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
const posts = () => fake.requests('post')
const listGets = () => fake.sent.filter((c) => c.method === 'get' && c.url === '/appointments')
const noShowButton = (row: HTMLElement) => within(row).queryByRole('button', { name: /as a no-show$/ })

async function actionsIn(patient: string) {
  const row = await rowOf(patient)
  return within(row)
    .queryAllByRole('button')
    .map((b) => b.textContent?.trim())
}

async function openDialog(user: ReturnType<typeof userEvent.setup>, patient = 'Ravi Menon') {
  const row = await rowOf(patient)
  await user.click(within(row).getByRole('button', { name: `Mark ${patient} as a no-show` }))
  return screen.findByRole('dialog')
}

const everyStatus = () => [
  appt('a1', 'Booked Patient', 'booked'),
  appt('a2', 'Arrived Patient', 'checked_in'),
  appt('a3', 'Consulting Patient', 'in_progress'),
  appt('a4', 'Done Patient', 'completed'),
  appt('a5', 'Dropped Patient', 'cancelled'),
  appt('a6', 'Absent Patient', 'no_show'),
]

describe('who can mark a no-show', () => {
  it('offers it to reception while the appointment is booked or checked in, and never after', async () => {
    server = everyStatus()
    renderQueue(RECEPTIONIST)

    expect(noShowButton(await rowOf('Booked Patient'))).toBeInTheDocument()
    expect(noShowButton(await rowOf('Arrived Patient'))).toBeInTheDocument()
    expect(noShowButton(await rowOf('Consulting Patient'))).not.toBeInTheDocument()
    expect(noShowButton(await rowOf('Done Patient'))).not.toBeInTheDocument()
    expect(noShowButton(await rowOf('Dropped Patient'))).not.toBeInTheDocument()
    expect(noShowButton(await rowOf('Absent Patient'))).not.toBeInTheDocument()
    // The whole row, in order.
    expect(await actionsIn('Booked Patient')).toEqual(['Check in', 'No-show', 'Cancel'])
    expect(await actionsIn('Absent Patient')).toEqual([])
  })

  it.each([
    ['a doctor', DOCTOR],
    ['a nurse', NURSE],
  ])('hides it from %s, who lacks appointment.cancel', async (_who, permissions) => {
    server = everyStatus()
    renderQueue(permissions)

    expect(noShowButton(await rowOf('Booked Patient'))).not.toBeInTheDocument()
    expect(noShowButton(await rowOf('Arrived Patient'))).not.toBeInTheDocument()
  })
})

describe('confirming a no-show', () => {
  it('asks first, and sends nothing if the user backs out', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openDialog(user)

    expect(within(dialog).getByRole('heading', { name: 'Mark as a no-show?' })).toBeInTheDocument()
    expect(within(dialog).getByText(/Ravi Menon with Priya Sharma/)).toBeInTheDocument()
    expect(within(dialog).getByText(/can't be undone/)).toBeInTheDocument()
    // The API takes no reason, so none is asked for.
    expect(within(dialog).queryByRole('textbox')).not.toBeInTheDocument()
    expect(posts()).toHaveLength(0)

    await user.click(within(dialog).getByRole('button', { name: 'Keep appointment' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(posts()).toHaveLength(0)
    expect(await actionsIn('Ravi Menon')).toEqual(['Check in', 'No-show', 'Cancel'])
  })

  it('posts to /no-show with no body, then shows the status the server returned', async () => {
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openDialog(user)
    const listsBefore = listGets().length

    await user.click(within(dialog).getByRole('button', { name: 'Mark no-show' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Ravi Menon marked as a no-show'))
    expect(posts()).toHaveLength(1)
    const [request] = posts()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/appointments/a1/no-show')
    expect(request.data).toBeUndefined()

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // The badge came from a refetch, not from a guess made in the browser.
    expect(listGets().length).toBeGreaterThan(listsBefore)
    const row = await rowOf('Ravi Menon')
    expect(within(row).getByText('No show')).toBeInTheDocument()
    // Terminal: nothing more can be done to it.
    expect(await actionsIn('Ravi Menon')).toEqual([])
  })

  it('works for a patient who checked in and then left', async () => {
    server = [appt('a1', 'Ravi Menon', 'checked_in')]
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openDialog(user)

    await user.click(within(dialog).getByRole('button', { name: 'Mark no-show' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(server[0].status).toBe('no_show')
    expect(await actionsIn('Ravi Menon')).toEqual([])
  })

  it('warns when the appointment has not started yet, and only then', async () => {
    server = [
      appt('a1', 'Early Patient', 'booked', '2999-01-07T04:00:00Z'),
      appt('a2', 'Late Patient', 'booked', '2020-01-07T04:00:00Z'),
    ]
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)

    const early = await openDialog(user, 'Early Patient')
    expect(within(early).getByRole('alert')).toHaveTextContent("This appointment hasn't started yet")
    // A caution, not a rule: the API allows it, so the action stays available.
    expect(within(early).getByRole('button', { name: 'Mark no-show' })).toBeEnabled()
    await user.click(within(early).getByRole('button', { name: 'Keep appointment' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    const late = await openDialog(user, 'Late Patient')
    expect(within(late).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('sends one request however many times it is confirmed', async () => {
    let release: () => void = () => {}
    onPost = (config) => new Promise<Outcome>((resolve) => (release = () => resolve(applyNoShow(config))))
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await openDialog(user)
    const form = within(dialog).getByRole('button', { name: 'Mark no-show' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    const busy = await within(dialog).findByRole('button', { name: 'Marking…' })
    expect(busy).toBeDisabled()
    expect(busy).toHaveAttribute('aria-busy', 'true')
    expect(posts()).toHaveLength(1)

    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(posts()).toHaveLength(1)
  })
})

describe('when the server refuses a no-show', () => {
  async function confirm(user: ReturnType<typeof userEvent.setup>) {
    const dialog = await openDialog(user)
    await user.click(within(dialog).getByRole('button', { name: 'Mark no-show' }))
    return dialog
  }

  it('400, the consultation has started: says so and refreshes the row', async () => {
    onPost = (config) => {
      // The doctor started the consultation after this queue was loaded.
      server[0].status = 'in_progress'
      return applyNoShow(config)
    }
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    await confirm(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'This appointment can no longer be marked as a no-show — its status has changed. The queue has been refreshed.',
      ),
    )
    expect(toastSuccess).not.toHaveBeenCalled()
    const row = await rowOf('Ravi Menon')
    await waitFor(() => expect(within(row).getByText('In progress')).toBeInTheDocument())
    // Reception can do nothing to a consultation in progress.
    await waitFor(() => expect(noShowButton(row)).not.toBeInTheDocument())
    expect(posts()).toHaveLength(1)
  })

  it('403: explains it in the dialog and changes nothing', async () => {
    onPost = () => fail(403, 'Permission denied. Required: appointment.cancel.')
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await confirm(user)

    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent("You don't have permission to mark appointments as no-shows.")
    expect(toastError).toHaveBeenCalledWith("You don't have permission to mark appointments as no-shows.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(server[0].status).toBe('booked')
  })

  it('404: says the appointment is gone', async () => {
    onPost = () => fail(404, 'Appointment not found.')
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    await confirm(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'This appointment no longer exists. The queue has been refreshed.',
      ),
    )
  })

  it('422: shows the API\'s own validation message', async () => {
    onPost = () => fail(422, 'appointment_id is not a valid UUID.', { error_code: 'VALIDATION_ERROR' })
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await confirm(user)

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('appointment_id is not a valid UUID.')
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('500: hides the server text, keeps the dialog, and a retry goes through', async () => {
    onPost = () => fail(500, 'Traceback: internal detail')
    const user = userEvent.setup()
    renderQueue(RECEPTIONIST)
    const dialog = await confirm(user)

    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't mark the appointment as a no-show. Please try again.")
    expect(alert).not.toHaveTextContent('Traceback')

    onPost = null
    await user.click(within(dialog).getByRole('button', { name: 'Mark no-show' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(posts()).toHaveLength(2)
    expect(await actionsIn('Ravi Menon')).toEqual([])
  })
})
