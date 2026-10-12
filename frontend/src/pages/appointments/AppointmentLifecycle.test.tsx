import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { api } from '@/lib/api'
import { useAuthStore } from '@/store/auth-store'
import type { AppointmentStatus, AppointmentSummary } from '@/api/appointments'
import AppointmentsPage from './AppointmentsPage'

/**
 * The lifecycle on the queue, against the contract in
 * docs/18-API_CONTRACTS.md §5.4. The real hooks, permission check, `http`
 * wrapper and Axios instance run; only the network adapter is replaced by a
 * small in-memory "server", so each test asserts on the request that would
 * leave the browser and on what the queue shows once it refetches.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))
// The (closed) booking dialog in the page header reads these.
vi.mock('@/api/doctors', async (original) => ({
  ...(await original<typeof import('@/api/doctors')>()),
  useDoctors: () => ({ data: { items: [] } }),
}))
vi.mock('@/api/patients', () => ({ usePatients: () => ({ data: { items: [] } }) }))

// Role → permission sets as seeded in backend/app/seeds/seed.py.
const ADMIN = [
  'appointment.read',
  'appointment.book',
  'appointment.cancel',
  'appointment.check_in',
  'appointment.start',
  'appointment.complete',
]
const RECEPTIONIST = ['appointment.read', 'appointment.book', 'appointment.cancel', 'appointment.check_in']
const DOCTOR = ['appointment.read', 'appointment.check_in', 'appointment.start', 'appointment.complete']
const NURSE = ['appointment.read', 'appointment.check_in']

type Outcome = { status: number; data: unknown }

const ok = (data: unknown): Outcome => ({ status: 200, data: { success: true, message: 'ok', data } })
const fail = (status: number, message: string, extra: Record<string, unknown> = {}): Outcome => ({
  status,
  data: { success: false, message, ...extra },
})

function appt(id: string, patient: string, status: AppointmentStatus): AppointmentSummary {
  return {
    id,
    patient_id: `p-${id}`,
    patient_name: patient,
    doctor_id: 'd1',
    doctor_name: 'Priya Sharma',
    scheduled_start: '2030-01-07T04:00:00Z',
    scheduled_end: '2030-01-07T04:30:00Z',
    status,
    type: 'new',
  }
}

/** What each endpoint moves an appointment to, as the server would. */
const RESULT: Record<string, AppointmentStatus> = {
  'check-in': 'checked_in',
  start: 'in_progress',
  complete: 'completed',
  cancel: 'cancelled',
}

const originalAdapter = api.defaults.adapter
let server: AppointmentSummary[]
let sent: InternalAxiosRequestConfig[]
/** Override to make the next lifecycle call fail or hang. */
let onPost: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null

const posts = () => sent.filter((c) => c.method === 'post')
const gets = () => sent.filter((c) => c.method === 'get')

function applyTransition(config: InternalAxiosRequestConfig): Outcome {
  const [, id, action] = /^\/appointments\/([^/]+)\/([^/]+)$/.exec(config.url ?? '') ?? []
  const row = server.find((a) => a.id === id)
  if (!row) return fail(404, 'Appointment not found.')
  row.status = RESULT[action]
  return ok({ ...row })
}

beforeEach(() => {
  sent = []
  onPost = null
  toastSuccess.mockReset()
  toastError.mockReset()
  server = [appt('a1', 'Ravi Menon', 'booked')]
  const adapter: AxiosAdapter = async (config) => {
    sent.push(config)
    const outcome =
      config.method === 'post'
        ? await (onPost ?? applyTransition)(config)
        : {
            status: 200,
            data: {
              success: true,
              message: 'ok',
              data: server.map((a) => ({ ...a })),
              metadata: {
                pagination: { page: 1, page_size: 25, total_records: server.length, total_pages: 1 },
              },
            },
          }
    const response = { ...outcome, statusText: '', headers: {}, config }
    if (outcome.status >= 400) {
      throw new AxiosError('Request failed', 'ERR_BAD_REQUEST', config, null, response)
    }
    return response
  }
  api.defaults.adapter = adapter
})

afterEach(() => {
  api.defaults.adapter = originalAdapter
  useAuthStore.setState({ user: null, isAuthenticated: false })
})

function renderQueue(permissions: string[]) {
  useAuthStore.setState({
    user: { id: 'u1', name: 'Test User', email: 'user@example.com', permissions },
    isAuthenticated: true,
  })
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

/** The lifecycle button labels in a row, in order. */
async function actionsIn(patient: string) {
  const row = await rowOf(patient)
  return within(row)
    .queryAllByRole('button')
    .map((b) => b.textContent?.trim())
}

describe('appointment lifecycle actions', () => {
  it('offers only the actions that fit each status', async () => {
    server = [
      appt('a1', 'Booked Patient', 'booked'),
      appt('a2', 'Arrived Patient', 'checked_in'),
      appt('a3', 'Consulting Patient', 'in_progress'),
      appt('a4', 'Done Patient', 'completed'),
      appt('a5', 'Dropped Patient', 'cancelled'),
      appt('a6', 'Absent Patient', 'no_show'),
    ]
    renderQueue(ADMIN)

    expect(await actionsIn('Booked Patient')).toEqual(['Check in', 'No-show', 'Cancel'])
    expect(await actionsIn('Arrived Patient')).toEqual(['Start', 'No-show', 'Cancel'])
    expect(await actionsIn('Consulting Patient')).toEqual(['Complete'])
    expect(await actionsIn('Done Patient')).toEqual([])
    expect(await actionsIn('Dropped Patient')).toEqual([])
    expect(await actionsIn('Absent Patient')).toEqual([])
  })

  it.each([
    { from: 'booked', button: 'Check in', path: 'check-in', badge: 'Checked in', next: ['Start', 'No-show', 'Cancel'], toast: 'Patient checked in' },
    { from: 'checked_in', button: 'Start', path: 'start', badge: 'In progress', next: ['Complete'], toast: 'Consultation started' },
    { from: 'in_progress', button: 'Complete', path: 'complete', badge: 'Completed', next: [], toast: 'Consultation completed' },
  ] as const)('$button posts to /$path and the row moves on', async ({ from, button, path, badge, next, toast }) => {
    server = [appt('a1', 'Ravi Menon', from)]
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(within(await rowOf('Ravi Menon')).getByRole('button', { name: `${button} Ravi Menon` }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(toast))
    expect(posts()).toHaveLength(1)
    const [request] = posts()
    expect(`${request.baseURL}${request.url}`).toBe(`/api/v1/appointments/a1/${path}`)
    // These endpoints take no body.
    expect(request.data).toBeUndefined()

    const row = await rowOf('Ravi Menon')
    expect(within(row).getByText(badge)).toBeInTheDocument()
    expect(await actionsIn('Ravi Menon')).toEqual(next)
    // The new status came from a refetch, not from a guess made in the browser.
    expect(gets().length).toBeGreaterThanOrEqual(2)
  })

  it('cancels only after a reason is given and confirmed', async () => {
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(
      within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Cancel appointment for Ravi Menon' }),
    )
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Cancel this appointment?')).toBeInTheDocument()

    // Confirming without a reason is blocked before any request.
    await user.click(within(dialog).getByRole('button', { name: 'Cancel appointment' }))
    expect(await within(dialog).findByText('Give a reason for cancelling')).toBeInTheDocument()
    expect(posts()).toHaveLength(0)

    await user.type(within(dialog).getByLabelText(/Reason/), '  Patient asked to cancel ')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel appointment' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment cancelled'))
    expect(posts()).toHaveLength(1)
    const [request] = posts()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/appointments/a1/cancel')
    expect(JSON.parse(request.data as string)).toEqual({ reason: 'Patient asked to cancel' })

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(within(await rowOf('Ravi Menon')).getByText('Cancelled')).toBeInTheDocument()
    expect(await actionsIn('Ravi Menon')).toEqual([])
  })

  it('keeps the appointment when the cancel dialog is dismissed', async () => {
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(
      within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Cancel appointment for Ravi Menon' }),
    )
    await user.click(await screen.findByRole('button', { name: 'Keep appointment' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(posts()).toHaveLength(0)
    expect(within(await rowOf('Ravi Menon')).getByText('Booked')).toBeInTheDocument()
  })

  it('sends one request and locks the row while an action is in flight', async () => {
    let finish: (outcome: Outcome) => void = () => {}
    onPost = (config) =>
      new Promise<Outcome>((resolve) => (finish = () => resolve(applyTransition(config))))
    renderQueue(ADMIN)
    const row = await rowOf('Ravi Menon')
    const checkIn = within(row).getByRole('button', { name: 'Check in Ravi Menon' })

    // Two clicks in the same tick, before React can disable the button.
    fireEvent.click(checkIn)
    fireEvent.click(checkIn)

    await waitFor(() => expect(checkIn).toHaveTextContent('Checking in…'))
    expect(checkIn).toBeDisabled()
    expect(checkIn).toHaveAttribute('aria-busy', 'true')
    expect(within(row).getByRole('button', { name: 'Cancel appointment for Ravi Menon' })).toBeDisabled()
    fireEvent.click(checkIn)
    expect(posts()).toHaveLength(1)

    finish(ok(null))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(posts()).toHaveLength(1)
    expect(await actionsIn('Ravi Menon')).toEqual(['Start', 'No-show', 'Cancel'])
  })
})

describe('appointment lifecycle errors', () => {
  it('shows a validation error from the API on the cancel form', async () => {
    onPost = () =>
      fail(422, 'Validation failed.', {
        error_code: 'VALIDATION_ERROR',
        errors: [{ field: 'reason', message: 'Cancellation reason must not be blank.' }],
      })
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(
      within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Cancel appointment for Ravi Menon' }),
    )
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Reason/), 'x')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel appointment' }))

    expect(
      await within(dialog).findByText('Cancellation reason must not be blank.'),
    ).toBeInTheDocument()
    // Still open so the reason can be corrected.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('explains a permission denial and leaves the appointment as it was', async () => {
    onPost = () => fail(403, 'Permission denied.', { error_code: 'PERMISSION_DENIED' })
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Check in Ravi Menon' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("You don't have permission to check in appointments."),
    )
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(within(await rowOf('Ravi Menon')).getByText('Booked')).toBeInTheDocument()
  })

  it('handles an invalid transition by refreshing to the real status', async () => {
    // Someone else completed the visit after this queue was loaded.
    onPost = () => {
      server[0].status = 'completed'
      return fail(400, "Cannot move an appointment from 'completed' to 'checked_in'.", {
        error_code: 'BUSINESS_RULE_VIOLATION',
      })
    }
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Check in Ravi Menon' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'This appointment can no longer be checked in — its status has changed. The queue has been refreshed.',
      ),
    )
    await waitFor(async () =>
      expect(within(await rowOf('Ravi Menon')).getByText('Completed')).toBeInTheDocument(),
    )
    expect(await actionsIn('Ravi Menon')).toEqual([])
  })

  it('handles an appointment that no longer exists', async () => {
    onPost = () => {
      server = []
      return fail(404, 'Appointment not found.', { error_code: 'RESOURCE_NOT_FOUND' })
    }
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Check in Ravi Menon' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'This appointment no longer exists. The queue has been refreshed.',
      ),
    )
    await waitFor(() => expect(screen.queryByText('Ravi Menon')).not.toBeInTheDocument())
  })

  it('handles a conflicting update', async () => {
    onPost = () => fail(409, 'Conflict.', { error_code: 'RESOURCE_CONFLICT' })
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Check in Ravi Menon' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(expect.stringContaining('Someone else just updated')),
    )
  })

  it('does not show internal error text for a server failure', async () => {
    onPost = () => fail(500, 'Traceback (most recent call last): asyncpg.exceptions…')
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Check in Ravi Menon' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't check in the appointment. Please try again."),
    )
    // The row is usable again for a retry.
    expect(within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Check in Ravi Menon' })).toBeEnabled()
  })

  it('closes the cancel dialog when the appointment can no longer be cancelled', async () => {
    onPost = () => {
      server[0].status = 'in_progress'
      return fail(400, "Cannot move an appointment from 'in_progress' to 'cancelled'.")
    }
    const user = userEvent.setup()
    renderQueue(ADMIN)

    await user.click(
      within(await rowOf('Ravi Menon')).getByRole('button', { name: 'Cancel appointment for Ravi Menon' }),
    )
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Patient asked to cancel')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel appointment' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'This appointment can no longer be cancelled — its status has changed. The queue has been refreshed.',
      ),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await actionsIn('Ravi Menon')).toEqual(['Complete'])
  })
})

describe('appointment lifecycle permissions', () => {
  const all = () => [
    appt('a1', 'Booked Patient', 'booked'),
    appt('a2', 'Arrived Patient', 'checked_in'),
    appt('a3', 'Consulting Patient', 'in_progress'),
  ]

  it('gives a receptionist check-in and cancel, but not start or complete', async () => {
    server = all()
    renderQueue(RECEPTIONIST)

    expect(await actionsIn('Booked Patient')).toEqual(['Check in', 'No-show', 'Cancel'])
    expect(await actionsIn('Arrived Patient')).toEqual(['No-show', 'Cancel'])
    expect(await actionsIn('Consulting Patient')).toEqual([])
  })

  it('gives a doctor check-in, start and complete, but not cancel', async () => {
    server = all()
    renderQueue(DOCTOR)

    expect(await actionsIn('Booked Patient')).toEqual(['Check in'])
    expect(await actionsIn('Arrived Patient')).toEqual(['Start'])
    expect(await actionsIn('Consulting Patient')).toEqual(['Complete'])
  })

  it('gives a nurse check-in only', async () => {
    server = all()
    renderQueue(NURSE)

    expect(await actionsIn('Booked Patient')).toEqual(['Check in'])
    expect(await actionsIn('Arrived Patient')).toEqual([])
    expect(await actionsIn('Consulting Patient')).toEqual([])
  })

  it('shows no lifecycle actions to a read-only user', async () => {
    server = all()
    renderQueue(['appointment.read'])

    expect(await actionsIn('Booked Patient')).toEqual([])
    expect(await actionsIn('Arrived Patient')).toEqual([])
    expect(await actionsIn('Consulting Patient')).toEqual([])
  })

  it('lets an authorized user act: a doctor completes a consultation', async () => {
    server = [appt('a3', 'Consulting Patient', 'in_progress')]
    const user = userEvent.setup()
    renderQueue(DOCTOR)

    await user.click(
      within(await rowOf('Consulting Patient')).getByRole('button', { name: 'Complete Consulting Patient' }),
    )

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Consultation completed'))
    expect(`${posts()[0].baseURL}${posts()[0].url}`).toBe('/api/v1/appointments/a3/complete')
  })
})
