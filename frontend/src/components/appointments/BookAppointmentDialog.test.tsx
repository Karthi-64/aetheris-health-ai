import type { ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AxiosError, type AxiosAdapter, type InternalAxiosRequestConfig } from 'axios'
import { api } from '@/lib/api'
import type { AppointmentSummary } from '@/api/appointments'
import { BookAppointmentDialog } from './BookAppointmentDialog'
import AppointmentsPage from '@/pages/appointments/AppointmentsPage'

/**
 * The booking request itself is never mocked here: the real `useBookAppointment`
 * hook, `http` wrapper and Axios instance run, and only the network adapter is
 * replaced. Mocking the hook is how the missing `Idempotency-Key` (a 422 from
 * the real API) went unnoticed.
 */

const { toastSuccess, toastError, denied } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
  /** Permission codes the signed-in user does NOT hold. */
  denied: new Set<string>(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))
// The pickers only read; their data is fixed so the form can be filled in.
vi.mock('@/api/doctors', async (original) => ({
  // Everything but the doctor list is real — the slots request goes through Axios.
  ...(await original<typeof import('@/api/doctors')>()),
  useDoctors: () => ({
    data: { items: [{ id: 'doc-1', full_name: 'Dr. Anita Chen', specialization: 'Cardiology' }] },
  }),
}))
vi.mock('@/api/patients', () => ({
  usePatients: ({ q }: { q?: string }) => ({
    data: { items: q ? [{ id: 'pat-1', full_name: 'Ravi Menon', mrn: 'MRN-2026-00042' }] : [] },
  }),
}))
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    can: (code: string) => !denied.has(code),
    canAny: () => true,
    nav: [],
    role: undefined,
  }),
}))

type Outcome = { status: number; data: unknown }
type Handler = (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>

const originalAdapter = api.defaults.adapter
let sent: InternalAxiosRequestConfig[]
let onPost: Handler
let onGet: Handler

const ok = (data: unknown, status = 200): Outcome => ({
  status,
  data: { success: true, message: 'ok', data },
})

const listOf = (items: AppointmentSummary[]): Outcome => ({
  status: 200,
  data: {
    success: true,
    message: 'ok',
    data: items,
    metadata: {
      pagination: { page: 1, page_size: 25, total_records: items.length, total_pages: 1 },
    },
  },
})

const booked: AppointmentSummary = {
  id: 'appt-1',
  patient_id: 'pat-1',
  patient_name: 'Ravi Menon',
  doctor_id: 'doc-1',
  doctor_name: 'Dr. Anita Chen',
  scheduled_start: '2030-01-07T04:00:00Z',
  scheduled_end: '2030-01-07T04:15:00Z',
  status: 'booked',
  type: 'new',
}

const posts = () => sent.filter((c) => c.method === 'post')
const keyOf = (config: InternalAxiosRequestConfig) => config.headers.get('Idempotency-Key')

beforeEach(() => {
  sent = []
  // Without availability access the form falls back to a typed time, which is
  // what the request-contract tests below exercise. Slot booking has its own
  // describe block and clears this.
  denied.clear()
  denied.add('doctor.availability.read')
  toastSuccess.mockReset()
  toastError.mockReset()
  onPost = () => ok(booked, 201)
  onGet = () => listOf([])
  const adapter: AxiosAdapter = async (config) => {
    sent.push(config)
    const outcome = await (config.method === 'post' ? onPost(config) : onGet(config))
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
})

function withClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>{ui}</QueryClientProvider>
    </MemoryRouter>,
  )
}

function renderDialog() {
  return withClient(<BookAppointmentDialog trigger={<button type="button">Open booking</button>} />)
}

/** Fill every required field the way reception would, leaving the dialog ready to submit. */
async function fillForm(user: ReturnType<typeof userEvent.setup>, time = '09:30') {
  await screen.findByRole('dialog')
  await user.type(screen.getByLabelText(/Patient/), 'Ravi')
  await user.click(await screen.findByRole('button', { name: /Ravi Menon/ }))
  await user.click(screen.getByRole('combobox', { name: /Doctor/ }))
  await user.click(await screen.findByRole('option', { name: /Dr\. Anita Chen/ }))
  await user.type(screen.getByLabelText(/Date/), '2030-01-07')
  await user.type(screen.getByLabelText(/Time/), time)
}

async function openAndFill(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Open booking' }))
  await fillForm(user)
}

const submit = (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole('button', { name: /^Book$/ }))

describe('BookAppointmentDialog', () => {
  it('opens and blocks submit with validation errors when required fields are empty', async () => {
    const user = userEvent.setup()
    renderDialog()

    await user.click(screen.getByRole('button', { name: 'Open booking' }))
    // Dialog is open with the form.
    expect(screen.getByText('Book appointment')).toBeInTheDocument()

    // Submit empty -> zod validation surfaces required-field errors.
    await submit(user)
    expect(await screen.findByText('Choose a patient')).toBeInTheDocument()
    expect(screen.getByText('Choose a doctor')).toBeInTheDocument()
    expect(screen.getByText('Pick a date')).toBeInTheDocument()
    expect(screen.getByText('Pick a time')).toBeInTheDocument()
    expect(posts()).toHaveLength(0)
  })

  it('books with the body and Idempotency-Key the API requires, then closes', async () => {
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)
    await user.type(screen.getByLabelText(/Reason/), 'Persistent cough')

    await submit(user)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
    expect(posts()).toHaveLength(1)
    const [request] = posts()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/appointments')
    expect(request.headers.get('Content-Type')).toBe('application/json')

    const key = keyOf(request)
    expect(typeof key).toBe('string')
    expect((key as string).length).toBeGreaterThanOrEqual(8)
    expect((key as string).length).toBeLessThanOrEqual(200)

    // Reception's local wall-clock time, sent as a tz-aware instant.
    const start = new Date('2030-01-07T09:30')
    expect(JSON.parse(request.data as string)).toEqual({
      patient_id: 'pat-1',
      doctor_id: 'doc-1',
      scheduled_start: start.toISOString(),
      scheduled_end: new Date(start.getTime() + 15 * 60_000).toISOString(),
      type: 'new',
      reason: 'Persistent cough',
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('treats an idempotent replay (200) as the same success', async () => {
    onPost = () => ok(booked, 200)
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)

    await submit(user)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('sends one request and shows progress while a booking is in flight', async () => {
    let finish: (outcome: Outcome) => void = () => {}
    onPost = () => new Promise<Outcome>((resolve) => (finish = resolve))
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)

    // Two submits in the same tick, before React can disable the button.
    const form = screen.getByRole('dialog').querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    const busy = await screen.findByRole('button', { name: /Booking/ })
    expect(busy).toBeDisabled()
    expect(busy).toHaveAttribute('aria-busy', 'true')
    await user.click(busy)
    expect(posts()).toHaveLength(1)

    finish(ok(booked, 201))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(posts()).toHaveLength(1)
  })

  it('retries a failed booking with the same Idempotency-Key', async () => {
    onPost = () => ({ status: 503, data: { success: false, message: 'Service unavailable.' } })
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)

    await submit(user)
    // A 5xx is not a refusal the API explains to a user: its text is not shown.
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Could not book the appointment. Please try again.'),
    )
    // The dialog stays open with the details intact, ready to retry.
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    onPost = () => ok(booked, 200)
    await submit(user)
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled())

    expect(posts()).toHaveLength(2)
    expect(keyOf(posts()[1])).toBe(keyOf(posts()[0]))
  })

  it('shows API validation errors on the form', async () => {
    onPost = () => ({
      status: 422,
      data: {
        success: false,
        message: 'Validation failed.',
        error_code: 'VALIDATION_ERROR',
        errors: [
          { field: 'scheduled_start', message: 'scheduled_start must include a UTC offset.' },
          { field: '', message: 'Duration must be one of [10, 15, 20, 30, 45, 60] minutes.' },
        ],
      },
    })
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)

    await submit(user)

    expect(await screen.findByText('scheduled_start must include a UTC offset.')).toBeInTheDocument()
    expect(
      screen.getByText('Duration must be one of [10, 15, 20, 30, 45, 60] minutes.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('explains a double-booking conflict (409)', async () => {
    onPost = () => ({ status: 409, data: { success: false, message: 'Overlap.' } })
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)

    await submit(user)

    expect(await screen.findByText('Time unavailable')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('shows a business-rule rejection (400) from the API', async () => {
    onPost = () => ({
      status: 400,
      data: { success: false, message: 'The doctor is not available at that time.' },
    })
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)

    await submit(user)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The doctor is not available at that time.',
    )
  })

  it('shows a plain sentence, not transport text, when the server fails', async () => {
    // A proxy answering 502 with no envelope: Axios's own message is all there is.
    onPost = () => ({ status: 502, data: '<html>Bad Gateway</html>' })
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)

    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Could not book the appointment. Please try again.'),
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('rejects a reason longer than the API allows without sending it', async () => {
    const user = userEvent.setup()
    renderDialog()
    await openAndFill(user)
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: 'x'.repeat(501) } })

    await submit(user)

    expect(await screen.findByText('Keep the reason under 500 characters')).toBeInTheDocument()
    expect(posts()).toHaveLength(0)
  })
})

describe('AppointmentsPage booking', () => {
  it('shows the new appointment in the queue after booking', async () => {
    // The queue is empty until the booking is accepted.
    let created = false
    onGet = () => listOf(created ? [booked] : [])
    onPost = () => {
      created = true
      return ok(booked, 201)
    }
    const user = userEvent.setup()
    withClient(<AppointmentsPage />)
    expect(await screen.findByText('No appointments today')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^Book$/ }))
    await fillForm(user)
    await user.click(screen.getAllByRole('button', { name: /^Book$/ }).at(-1) as HTMLElement)

    expect(await screen.findByRole('cell', { name: 'Ravi Menon' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'Dr. Anita Chen' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('booking from the doctor\'s slots', () => {
  const SLOTS = {
    date: '2030-01-07',
    doctor_id: 'doc-1',
    timezone: 'Asia/Kolkata',
    slots: [
      { start: '2030-01-07T09:00:00+05:30', end: '2030-01-07T09:30:00+05:30', status: 'available', appointment_id: null },
      { start: '2030-01-07T09:30:00+05:30', end: '2030-01-07T10:00:00+05:30', status: 'booked', appointment_id: 'appt-9' },
      { start: '2030-01-07T10:00:00+05:30', end: '2030-01-07T10:30:00+05:30', status: 'on_leave', appointment_id: null },
      { start: '2030-01-07T10:30:00+05:30', end: '2030-01-07T11:00:00+05:30', status: 'available', appointment_id: null },
    ],
  }
  let slots: typeof SLOTS
  const slotRequests = () => sent.filter((c) => (c.url ?? '').endsWith('/slots'))
  const time = (iso: string) =>
    new Date(iso).toLocaleTimeString(undefined, { timeStyle: 'short', timeZone: 'Asia/Kolkata' })

  beforeEach(() => {
    // A receptionist: reads availability, cannot override it.
    denied.clear()
    denied.add('appointment.book_override')
    slots = structuredClone(SLOTS)
    onGet = (config) => ((config.url ?? '').endsWith('/slots') ? ok(slots) : listOf([]))
  })

  async function chooseDoctorAndDate(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: 'Open booking' }))
    await screen.findByRole('dialog')
    await user.type(screen.getByLabelText(/Patient/), 'Ravi')
    await user.click(await screen.findByRole('button', { name: /Ravi Menon/ }))
    await user.click(screen.getByRole('combobox', { name: /Doctor/ }))
    await user.click(await screen.findByRole('option', { name: /Dr\. Anita Chen/ }))
    await user.type(screen.getByLabelText(/Date/), '2030-01-07')
    return within(await screen.findByRole('group', { name: 'Time slots' }))
  }

  it('asks for the doctor\'s slots on the chosen day and offers only the free ones', async () => {
    const user = userEvent.setup()
    renderDialog()
    const picker = await chooseDoctorAndDate(user)

    const free = await picker.findByRole('button', { name: time(SLOTS.slots[0].start) })
    expect(free).toBeEnabled()
    expect(picker.getByRole('button', { name: `${time(SLOTS.slots[1].start)}, booked` })).toBeDisabled()
    expect(picker.getByRole('button', { name: `${time(SLOTS.slots[2].start)}, on leave` })).toBeDisabled()
    // The times are the hospital's, and the form says so.
    expect(picker.getByText(/Asia\/Kolkata time/)).toBeInTheDocument()

    const request = slotRequests().at(-1) as InternalAxiosRequestConfig
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/doctors/doc-1/slots')
    expect(request.params).toEqual({ date: '2030-01-07' })
    // No typed time for someone who cannot override availability.
    expect(document.querySelector('input[type="time"]')).toBeNull()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('books the slot exactly as the API described it', async () => {
    const user = userEvent.setup()
    renderDialog()
    const picker = await chooseDoctorAndDate(user)
    await user.click(await picker.findByRole('button', { name: time(SLOTS.slots[3].start) }))
    expect(picker.getByRole('button', { name: time(SLOTS.slots[3].start) })).toHaveAttribute('aria-pressed', 'true')

    await submit(user)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
    // The slot's own start and end, offset included: the hospital's clock, not the browser's.
    expect(JSON.parse(posts()[0].data as string)).toEqual({
      patient_id: 'pat-1',
      doctor_id: 'doc-1',
      scheduled_start: '2030-01-07T10:30:00+05:30',
      scheduled_end: '2030-01-07T11:00:00+05:30',
      type: 'new',
    })
    expect(typeof keyOf(posts()[0])).toBe('string')
  })

  it('requires a slot before booking', async () => {
    const user = userEvent.setup()
    renderDialog()
    await chooseDoctorAndDate(user)

    await submit(user)

    expect(await screen.findByText('Pick a time slot')).toBeInTheDocument()
    expect(posts()).toHaveLength(0)
  })

  it('refreshes the slots and asks again when another desk took the slot (409)', async () => {
    onPost = () => {
      slots.slots[0].status = 'booked'
      return { status: 409, data: { success: false, message: 'Overlap.' } }
    }
    const user = userEvent.setup()
    renderDialog()
    const picker = await chooseDoctorAndDate(user)
    await user.click(await picker.findByRole('button', { name: time(SLOTS.slots[0].start) }))
    const before = slotRequests().length

    await submit(user)

    expect(await screen.findByText('Time unavailable')).toBeInTheDocument()
    await waitFor(() => expect(slotRequests().length).toBeGreaterThan(before))
    expect(
      await picker.findByRole('button', { name: `${time(SLOTS.slots[0].start)}, booked` }),
    ).toBeDisabled()
  })

  it('says so when the doctor has no slots that day', async () => {
    slots.slots = []
    const user = userEvent.setup()
    renderDialog()
    const picker = await chooseDoctorAndDate(user)

    expect(await picker.findByText(/no time slots on that day/)).toBeInTheDocument()
  })

  it('offers a retry when the slots cannot be loaded', async () => {
    let failing = true
    onGet = (config) =>
      (config.url ?? '').endsWith('/slots')
        ? failing
          ? { status: 500, data: { success: false, message: 'Internal error.' } }
          : ok(slots)
        : listOf([])
    const user = userEvent.setup()
    renderDialog()
    const picker = await chooseDoctorAndDate(user)

    expect(await picker.findByText(/couldn't be loaded/)).toBeInTheDocument()
    failing = false
    await user.click(picker.getByRole('button', { name: /Retry/ }))
    expect(await picker.findByRole('button', { name: time(SLOTS.slots[0].start) })).toBeEnabled()
  })

  it('lets someone with the override type a time instead', async () => {
    denied.clear()
    const user = userEvent.setup()
    renderDialog()
    await chooseDoctorAndDate(user)

    await user.click(screen.getByRole('checkbox'))
    await user.type(document.querySelector('input[type="time"]') as HTMLElement, '20:00')
    await submit(user)

    await waitFor(() => expect(posts()).toHaveLength(1))
    const start = new Date('2030-01-07T20:00')
    expect(JSON.parse(posts()[0].data as string)).toMatchObject({
      scheduled_start: start.toISOString(),
      scheduled_end: new Date(start.getTime() + 15 * 60_000).toISOString(),
    })
  })

  it('books for the patient it was opened from, with no patient search', async () => {
    const user = userEvent.setup()
    withClient(
      <BookAppointmentDialog
        patient={{ id: 'pat-7', full_name: 'Thomas George', mrn: 'MRN-2026-00004' }}
        trigger={<button type="button">Open booking</button>}
      />,
    )
    await user.click(screen.getByRole('button', { name: 'Open booking' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/MRN-2026-00004/)).toBeInTheDocument()
    expect(within(dialog).queryByPlaceholderText(/Search name/)).not.toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: /Doctor/ }))
    await user.click(await screen.findByRole('option', { name: /Dr\. Anita Chen/ }))
    await user.type(screen.getByLabelText(/Date/), '2030-01-07')
    const picker = within(await screen.findByRole('group', { name: 'Time slots' }))
    await user.click(await picker.findByRole('button', { name: time(SLOTS.slots[0].start) }))
    await submit(user)

    await waitFor(() => expect(posts()).toHaveLength(1))
    expect(JSON.parse(posts()[0].data as string)).toMatchObject({ patient_id: 'pat-7' })
  })

  /**
   * The suggestion is advice: the real capability read and recommend request
   * run through Axios here, and every test checks that asking for, reading or
   * using a suggestion never books — only the form's own Book button does.
   */
  describe('AI slot suggestion', () => {
    const FLAG = 'feature.ai.slot_recommendation'
    const SUGGEST = 'Suggest a slot with AI'
    const UNAVAILABLE = 'The suggestion is unavailable right now. Pick a slot below, or ask again.'
    const NO_LONGER_FREE = 'The suggested time is no longer free. Pick a slot below, or ask again.'

    let flagsAnswer: Handler
    let recommendAnswer: Handler
    let bookAnswer: Handler

    const flagRequests = () => sent.filter((c) => c.url === '/hospitals/current/feature-flags')
    const recommendRequests = () => posts().filter((c) => c.url === '/appointments/recommend-slot')
    const bookings = () => posts().filter((c) => c.url === '/appointments')

    const refused = (status: number, code: string): Outcome => ({
      status,
      data: { success: false, message: 'Refused.', error_code: code, errors: null },
    })

    const recommended = (
      slot: { start: string; end: string },
      reason: string | null = 'It is the earliest free slot of the morning.',
    ): Outcome =>
      ok({
        status: 'recommended',
        recommendation: { slot_start: slot.start, slot_end: slot.end, doctor_id: 'doc-1', reason },
        date: '2030-01-07',
        timezone: 'Asia/Kolkata',
        candidate_count: 2,
      })

    const noFreeSlots = (): Outcome =>
      ok({
        status: 'no_free_slots',
        recommendation: null,
        date: '2030-01-07',
        timezone: 'Asia/Kolkata',
        candidate_count: 0,
      })

    beforeEach(() => {
      flagsAnswer = () => ok({ flags: { [FLAG]: { available: true } } })
      recommendAnswer = () => recommended(SLOTS.slots[3])
      bookAnswer = () => ok(booked, 201)
      onGet = (config) => {
        const url = config.url ?? ''
        if (url.endsWith('/slots')) return ok(slots)
        if (url === '/hospitals/current/feature-flags') return flagsAnswer(config)
        return listOf([])
      }
      onPost = (config) =>
        config.url === '/appointments/recommend-slot' ? recommendAnswer(config) : bookAnswer(config)
    })

    afterEach(() => {
      vi.useRealTimers()
    })

    const group = () => screen.getByRole('group', { name: 'AI slot suggestion' })
    const status = () => within(group()).getByRole('status')
    const suggestButton = () => screen.findByRole('button', { name: SUGGEST })

    /** Fill the form as far as the slots, then ask for a suggestion. */
    async function ask(user: ReturnType<typeof userEvent.setup>) {
      const picker = await chooseDoctorAndDate(user)
      await user.click(await suggestButton())
      return picker
    }

    it('is not offered to a user who may not ask, and their capability is never read', async () => {
      denied.add('appointment.recommend_slot')
      const user = userEvent.setup()
      renderDialog()
      const picker = await chooseDoctorAndDate(user)
      await user.click(await picker.findByRole('button', { name: time(SLOTS.slots[0].start) }))

      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()
      expect(screen.queryByRole('group', { name: 'AI slot suggestion' })).not.toBeInTheDocument()
      expect(flagRequests()).toHaveLength(0)
      expect(recommendRequests()).toHaveLength(0)
    })

    it.each<[string, () => Outcome]>([
      ['the feature is not available', () => ok({ flags: { [FLAG]: { available: false } } })],
      ['the answer does not name the feature', () => ok({ flags: {} })],
      ['the capability read fails (500)', () => ({ status: 500, data: { success: false, message: 'Internal error.' } })],
      ['the capability read is not found (404)', () => ({ status: 404, data: { success: false, message: 'Not found.' } })],
    ])('is not offered when %s, and booking from a slot still works', async (_name, answer) => {
      flagsAnswer = answer
      const user = userEvent.setup()
      renderDialog()
      const picker = await chooseDoctorAndDate(user)
      await waitFor(() => expect(flagRequests()).toHaveLength(1))
      const [request] = flagRequests()
      expect(request.method).toBe('get')
      expect(`${request.baseURL}${request.url}`).toBe('/api/v1/hospitals/current/feature-flags')
      await user.click(await picker.findByRole('button', { name: time(SLOTS.slots[0].start) }))

      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()
      await submit(user)

      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
      expect(bookings()).toHaveLength(1)
      expect(JSON.parse(bookings()[0].data as string)).toEqual({
        patient_id: 'pat-1',
        doctor_id: 'doc-1',
        scheduled_start: SLOTS.slots[0].start,
        scheduled_end: SLOTS.slots[0].end,
        type: 'new',
      })
      // With AI off, nothing asks for a suggestion — and the read is not retried.
      expect(recommendRequests()).toHaveLength(0)
      expect(flagRequests()).toHaveLength(1)
    })

    it('appears only once patient, doctor and date are chosen and the slots have loaded', async () => {
      const user = userEvent.setup()
      renderDialog()
      await user.click(screen.getByRole('button', { name: 'Open booking' }))
      await screen.findByRole('dialog')
      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()

      // Doctor and date, but no patient yet: the slots load, the control does not show.
      await user.click(screen.getByRole('combobox', { name: /Doctor/ }))
      await user.click(await screen.findByRole('option', { name: /Dr\. Anita Chen/ }))
      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()
      await user.type(screen.getByLabelText(/Date/), '2030-01-07')
      const slotsGroup = await screen.findByRole('group', { name: 'Time slots' })
      await within(slotsGroup).findByRole('button', { name: time(SLOTS.slots[0].start) })
      await waitFor(() => expect(flagRequests()).toHaveLength(1))
      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()

      await user.type(screen.getByLabelText(/Patient/), 'Ravi')
      await user.click(await screen.findByRole('button', { name: /Ravi Menon/ }))

      const button = await suggestButton()
      expect(button).toHaveAttribute('type', 'button')
      expect(button).toHaveAttribute('aria-disabled', 'false')
      expect(button).toHaveAccessibleDescription(
        'Optional. It only suggests a time — nothing is booked until you press Book.',
      )
      // Ahead of the slot buttons, so a keyboard user reaches it first.
      expect(
        button.compareDocumentPosition(screen.getByRole('group', { name: 'Time slots' })) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy()
      // Nothing is said, and nothing is asked, until the user asks.
      expect(status()).toBeEmptyDOMElement()
      // Empty, but never hidden: a live region that is out of the accessibility
      // tree when its first text arrives does not announce that text.
      expect(status().className).not.toMatch(/hidden/)
      expect(status()).not.toHaveAttribute('hidden')
      expect(status()).not.toHaveAttribute('aria-hidden')
      expect(recommendRequests()).toHaveLength(0)
    })

    it('is not offered while a time is being typed instead of picked', async () => {
      denied.clear()
      const user = userEvent.setup()
      renderDialog()
      await chooseDoctorAndDate(user)
      expect(await suggestButton()).toBeInTheDocument()

      await user.click(screen.getByRole('checkbox'))

      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()
    })

    it('is not offered when the day has no free slot', async () => {
      slots.slots[0].status = 'booked'
      slots.slots[3].status = 'on_leave'
      const user = userEvent.setup()
      renderDialog()
      const picker = await chooseDoctorAndDate(user)
      await picker.findByRole('button', { name: `${time(SLOTS.slots[0].start)}, booked` })
      await waitFor(() => expect(flagRequests()).toHaveLength(1))
      await user.click(screen.getByLabelText(/Reason/))

      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()
    })

    it('is not offered when the only free slot has already started, though the picker still offers it', async () => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(new Date('2030-01-07T09:10:00+05:30'))
      slots.slots[3].status = 'booked'
      const user = userEvent.setup()
      renderDialog()
      const picker = await chooseDoctorAndDate(user)
      // In progress, not ended: still pickable by hand.
      expect(await picker.findByRole('button', { name: time(SLOTS.slots[0].start) })).toBeEnabled()
      await waitFor(() => expect(flagRequests()).toHaveLength(1))
      await user.click(screen.getByLabelText(/Reason/))

      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()
    })

    it('shows and announces progress, and sends exactly the patient, doctor and date', async () => {
      let finish: (outcome: Outcome) => void = () => {}
      recommendAnswer = () => new Promise<Outcome>((resolve) => (finish = resolve))
      const user = userEvent.setup()
      renderDialog()
      await ask(user)

      const busy = await screen.findByRole('button', { name: 'Getting a suggestion…' })
      expect(busy).toHaveAttribute('aria-busy', 'true')
      expect(busy).toHaveAttribute('aria-disabled', 'true')
      expect(busy).not.toBeDisabled()
      expect(status().textContent).toBe('Getting a suggestion…')
      expect(screen.queryByRole('button', { name: 'Use this slot' })).not.toBeInTheDocument()

      expect(recommendRequests()).toHaveLength(1)
      const [request] = recommendRequests()
      expect(request.method).toBe('post')
      expect(`${request.baseURL}${request.url}`).toBe('/api/v1/appointments/recommend-slot')
      expect(JSON.parse(request.data as string)).toEqual({
        patient_id: 'pat-1',
        doctor_id: 'doc-1',
        date: '2030-01-07',
      })
      expect(request.timeout).toBe(15_000)
      expect(keyOf(request)).toBeFalsy()

      finish(recommended(SLOTS.slots[3]))
      expect(await screen.findByRole('button', { name: 'Use this slot' })).toBeInTheDocument()
      expect(await suggestButton()).toHaveAttribute('aria-busy', 'false')
    })

    it('shows the suggestion as advice to review, and selects and books nothing', async () => {
      const user = userEvent.setup()
      renderDialog()
      const picker = await ask(user)

      const use = await screen.findByRole('button', { name: 'Use this slot' })
      const region = status()
      expect(within(region).getByText('AI suggested slot — review before booking.')).toBeInTheDocument()
      expect(within(region).getByText(`AI suggested: ${time(SLOTS.slots[3].start)}`)).toBeInTheDocument()
      expect(
        within(region).getByText('Reason from the AI: It is the earliest free slot of the morning.'),
      ).toBeInTheDocument()
      expect(region).toHaveAttribute('aria-live', 'polite')
      expect(region).toHaveAttribute('aria-atomic', 'true')
      // The action sits beside the announcement, not inside it.
      expect(group()).toContainElement(use)
      expect(region).not.toContainElement(use)
      expect(use).toHaveAttribute('type', 'button')

      for (const button of picker.getAllByRole('button')) {
        expect(button).toHaveAttribute('aria-pressed', 'false')
      }
      expect(bookings()).toHaveLength(0)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(toastSuccess).not.toHaveBeenCalled()
      expect(toastError).not.toHaveBeenCalled()
      // The feed already shows the slot as free, so it is not re-read.
      expect(slotRequests()).toHaveLength(1)
    })

    it('shows no reason line when the AI gave none', async () => {
      recommendAnswer = () => recommended(SLOTS.slots[3], null)
      const user = userEvent.setup()
      renderDialog()
      await ask(user)

      await screen.findByRole('button', { name: 'Use this slot' })
      expect(status().textContent).toBe(
        `AI suggested slot — review before booking.AI suggested: ${time(SLOTS.slots[3].start)}`,
      )
    })

    it('books the suggested slot only when the user uses it and then presses Book', async () => {
      // The same instant as the 10:30+05:30 slot, spelled differently.
      recommendAnswer = () =>
        recommended({ start: '2030-01-07T05:00:00Z', end: '2030-01-07T05:30:00Z' })
      const user = userEvent.setup()
      renderDialog()
      const picker = await ask(user)
      const label = time(SLOTS.slots[3].start)

      await user.click(await screen.findByRole('button', { name: 'Use this slot' }))

      expect(picker.getByRole('button', { name: label })).toHaveAttribute('aria-pressed', 'true')
      expect(
        within(status()).getByText(`${label} is selected below. Press Book to confirm.`),
      ).toBeInTheDocument()
      // Selected, not booked.
      expect(bookings()).toHaveLength(0)

      await submit(user)

      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
      expect(bookings()).toHaveLength(1)
      // The feed's own strings, not the recommendation's.
      expect(JSON.parse(bookings()[0].data as string)).toEqual({
        patient_id: 'pat-1',
        doctor_id: 'doc-1',
        scheduled_start: '2030-01-07T10:30:00+05:30',
        scheduled_end: '2030-01-07T11:00:00+05:30',
        type: 'new',
      })
      expect(typeof keyOf(bookings()[0])).toBe('string')
      expect(recommendRequests()).toHaveLength(1)
    })

    it('sends one request when the button is clicked twice at once', async () => {
      let finish: (outcome: Outcome) => void = () => {}
      recommendAnswer = () => new Promise<Outcome>((resolve) => (finish = resolve))
      const user = userEvent.setup()
      renderDialog()
      await chooseDoctorAndDate(user)
      const button = await suggestButton()

      // Two clicks in the same tick, before React can mark the button busy.
      fireEvent.click(button)
      fireEvent.click(button)
      const busy = await screen.findByRole('button', { name: 'Getting a suggestion…' })
      await user.click(busy)
      expect(recommendRequests()).toHaveLength(1)

      finish(recommended(SLOTS.slots[3]))
      await screen.findByRole('button', { name: 'Use this slot' })
      expect(recommendRequests()).toHaveLength(1)
    })

    it.each<[string, Handler, string]>([
      [
        'AI is not configured on the server (503)',
        () => refused(503, 'AI_NOT_CONFIGURED'),
        'AI suggestions are not set up on this server. Pick a slot below.',
      ],
      [
        'the hospital has not been given the feature (403)',
        () => refused(403, 'FEATURE_DISABLED'),
        'AI slot suggestions are not enabled for this hospital. Pick a slot below.',
      ],
      [
        'the role may not ask (403)',
        () => refused(403, 'PERMISSION_DENIED'),
        'Your role cannot ask for AI slot suggestions. Pick a slot below.',
      ],
      [
        'the AI service is unavailable (503)',
        () => refused(503, 'AI_PROVIDER_UNAVAILABLE'),
        'The AI service is unavailable right now. Pick a slot below, or ask again.',
      ],
      [
        'the server fails with a plain 500',
        () => ({ status: 500, data: '<html>Internal Server Error</html>' }),
        'The AI service is unavailable right now. Pick a slot below, or ask again.',
      ],
      [
        'the AI service times out (503)',
        () => refused(503, 'AI_PROVIDER_TIMEOUT'),
        'The AI service took too long to answer. Pick a slot below, or ask again.',
      ],
      [
        "the AI's answer is rejected by the server (503)",
        () => refused(503, 'AI_RESPONSE_INVALID'),
        "The AI's answer could not be used, so there is no suggestion. Pick a slot below, or ask again.",
      ],
      [
        'too many requests were made (429)',
        () => refused(429, 'RATE_LIMITED'),
        'Too many suggestion requests. Wait a minute, then ask again.',
      ],
      [
        'the request is rejected as invalid (422)',
        () => refused(422, 'VALIDATION_ERROR'),
        'A suggestion could not be requested for this patient, doctor and date. Pick a slot below.',
      ],
      [
        'the request is rejected as malformed (400)',
        () => refused(400, 'BAD_REQUEST'),
        'A suggestion could not be requested for this patient, doctor and date. Pick a slot below.',
      ],
      [
        'the request is forbidden for a reason this screen does not know (403)',
        () => refused(403, 'SOMETHING_ELSE'),
        'Your role cannot ask for AI slot suggestions. Pick a slot below.',
      ],
      ['the route answers not found (404)', () => refused(404, 'RESOURCE_NOT_FOUND'), UNAVAILABLE],
      ['the route answers with a conflict (409)', () => refused(409, 'RESOURCE_CONFLICT'), UNAVAILABLE],
      [
        'no response comes back at all',
        (config) => {
          throw new AxiosError('Network Error', 'ERR_NETWORK', config)
        },
        'No answer came back from the server. Pick a slot below, or ask again.',
      ],
      [
        'the day has no free slot left',
        () => noFreeSlots(),
        'There are no upcoming free slots on this day to suggest.',
      ],
      ['the suggested slot is one the picker shows as booked', () => recommended(SLOTS.slots[1]), NO_LONGER_FREE],
      [
        'the answer says recommended but carries no recommendation',
        () => ok({ status: 'recommended', recommendation: null }),
        UNAVAILABLE,
      ],
      ['the answer has an unknown status', () => ok({ status: 'maybe' }), UNAVAILABLE],
      ['the answer is not an object', () => ok([]), UNAVAILABLE],
    ])('says so plainly when %s, and booking by hand still works', async (_name, answer, message) => {
      recommendAnswer = answer
      const user = userEvent.setup()
      renderDialog()
      const picker = await ask(user)

      await waitFor(() => expect(status().textContent).toBe(message))
      expect(screen.queryByRole('button', { name: 'Use this slot' })).not.toBeInTheDocument()
      // Advice failing is not a booking failure: no alert, no toast.
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(toastError).not.toHaveBeenCalled()
      expect(toastSuccess).not.toHaveBeenCalled()
      expect(document.body.textContent).not.toContain('could not be reached')
      expect(recommendRequests()).toHaveLength(1)
      expect(bookings()).toHaveLength(0)

      await user.click(await picker.findByRole('button', { name: time(SLOTS.slots[0].start) }))
      await submit(user)

      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
      expect(bookings()).toHaveLength(1)
      expect(JSON.parse(bookings()[0].data as string)).toMatchObject({
        scheduled_start: SLOTS.slots[0].start,
        scheduled_end: SLOTS.slots[0].end,
      })
      expect(recommendRequests()).toHaveLength(1)
    })

    it.each<[string, Outcome]>([
      ['AI_NOT_CONFIGURED', refused(503, 'AI_NOT_CONFIGURED')],
      ['FEATURE_DISABLED', refused(403, 'FEATURE_DISABLED')],
      ['PERMISSION_DENIED', refused(403, 'PERMISSION_DENIED')],
      ['a 403 with any other code', refused(403, 'SOMETHING_ELSE')],
    ])('stops asking after %s, and keeps the button focusable', async (_code, outcome) => {
      recommendAnswer = () => outcome
      const user = userEvent.setup()
      renderDialog()
      await ask(user)

      await waitFor(() => expect(status()).not.toBeEmptyDOMElement())
      const button = await suggestButton()
      expect(button).toHaveAttribute('aria-disabled', 'true')
      expect(button.className).toContain('aria-disabled:opacity-50')
      expect(button.className).toContain('aria-disabled:cursor-not-allowed')
      expect(button).not.toHaveAttribute('disabled')
      button.focus()
      expect(document.activeElement).toBe(button)

      await user.click(button)
      fireEvent.click(button)

      expect(recommendRequests()).toHaveLength(1)
      // The capability said "available" and the server said no: it is read again, once.
      await waitFor(() => expect(flagRequests()).toHaveLength(2))

      // Another day remounts the control. The capability read still says
      // "available", but the server has refused: it is not offered again.
      fireEvent.change(screen.getByLabelText(/Date/), { target: { value: '2030-01-08' } })
      await waitFor(() => expect(slotRequests()).toHaveLength(2))
      const picker = within(screen.getByRole('group', { name: 'Time slots' }))
      await picker.findByRole('button', { name: time(SLOTS.slots[0].start) })
      await user.click(screen.getByLabelText(/Reason/))

      expect(screen.queryByRole('group', { name: 'AI slot suggestion' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: SUGGEST })).not.toBeInTheDocument()
      expect(recommendRequests()).toHaveLength(1)
      expect(flagRequests()).toHaveLength(2)
    })

    it('offers the control again when the dialog is opened afresh after a refusal', async () => {
      recommendAnswer = () => refused(503, 'AI_NOT_CONFIGURED')
      const user = userEvent.setup()
      renderDialog()
      await ask(user)
      await waitFor(() =>
        expect(status().textContent).toBe('AI suggestions are not set up on this server. Pick a slot below.'),
      )

      await user.click(screen.getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      await chooseDoctorAndDate(user)

      expect(await suggestButton()).toHaveAttribute('aria-disabled', 'false')
      expect(status()).toBeEmptyDOMElement()
      expect(recommendRequests()).toHaveLength(1)
    })

    it('stops offering the control when a refusal arrives after the date has changed', async () => {
      let finish: (outcome: Outcome) => void = () => {}
      recommendAnswer = () => new Promise<Outcome>((resolve) => (finish = resolve))
      const user = userEvent.setup()
      renderDialog()
      await ask(user)
      await screen.findByRole('button', { name: 'Getting a suggestion…' })

      fireEvent.change(screen.getByLabelText(/Date/), { target: { value: '2030-01-08' } })
      expect(await suggestButton()).toHaveAttribute('aria-disabled', 'false')

      finish(refused(403, 'FEATURE_DISABLED'))

      await waitFor(() =>
        expect(screen.queryByRole('group', { name: 'AI slot suggestion' })).not.toBeInTheDocument(),
      )
      expect(recommendRequests()).toHaveLength(1)
    })

    it('shows nothing from an answer that arrives after the date has changed', async () => {
      let finish: (outcome: Outcome) => void = () => {}
      recommendAnswer = () => new Promise<Outcome>((resolve) => (finish = resolve))
      const user = userEvent.setup()
      renderDialog()
      await ask(user)
      await screen.findByRole('button', { name: 'Getting a suggestion…' })

      fireEvent.change(screen.getByLabelText(/Date/), { target: { value: '2030-01-08' } })
      const button = await suggestButton()
      await waitFor(() => expect(slotRequests()).toHaveLength(2))

      // An answer that would both show a message and re-read the slots.
      finish(noFreeSlots())
      await new Promise((resolve) => setTimeout(resolve, 50))

      expect(status()).toBeEmptyDOMElement()
      expect(button).toHaveAttribute('aria-disabled', 'false')
      expect(button).toHaveAttribute('aria-busy', 'false')
      expect(screen.queryByRole('button', { name: 'Use this slot' })).not.toBeInTheDocument()
      // Neither day's slots are re-read for it, and nothing is asked for the new day.
      expect(slotRequests()).toHaveLength(2)
      expect(recommendRequests()).toHaveLength(1)
    })

    it('asks again with the same button after a failure', async () => {
      recommendAnswer = () => refused(503, 'AI_PROVIDER_UNAVAILABLE')
      const user = userEvent.setup()
      renderDialog()
      await ask(user)
      await waitFor(() =>
        expect(status().textContent).toBe(
          'The AI service is unavailable right now. Pick a slot below, or ask again.',
        ),
      )
      const button = await suggestButton()
      expect(button).toHaveAttribute('aria-disabled', 'false')

      recommendAnswer = () => recommended(SLOTS.slots[3])
      await user.click(button)

      expect(await screen.findByRole('button', { name: 'Use this slot' })).toBeInTheDocument()
      expect(
        within(status()).getByText(`AI suggested: ${time(SLOTS.slots[3].start)}`),
      ).toBeInTheDocument()
      expect(recommendRequests()).toHaveLength(2)
      expect(bookings()).toHaveLength(0)
    })

    it('drops the suggestion when the date changes', async () => {
      const user = userEvent.setup()
      renderDialog()
      await ask(user)
      await screen.findByRole('button', { name: 'Use this slot' })

      fireEvent.change(screen.getByLabelText(/Date/), { target: { value: '2030-01-08' } })

      await waitFor(() =>
        expect(screen.queryByRole('button', { name: 'Use this slot' })).not.toBeInTheDocument(),
      )
      expect(screen.queryByText(/AI suggested/)).not.toBeInTheDocument()
      // Offered afresh for the new day, with nothing carried over and nothing asked.
      expect(await suggestButton()).toBeInTheDocument()
      expect(status()).toBeEmptyDOMElement()
      expect(recommendRequests()).toHaveLength(1)
    })

    it("shows the AI's reason as plain text, never as markup or a link", async () => {
      const hostile = '<img src=x onerror=alert(1)> see http://example.test'
      recommendAnswer = () => recommended(SLOTS.slots[3], hostile)
      const user = userEvent.setup()
      renderDialog()
      await ask(user)

      await screen.findByRole('button', { name: 'Use this slot' })
      expect(within(status()).getByText(`Reason from the AI: ${hostile}`)).toBeInTheDocument()
      expect(group().querySelector('img')).toBeNull()
      expect(group().querySelector('a')).toBeNull()
      expect(within(group()).queryByRole('link')).not.toBeInTheDocument()
    })

    it('shows no figure the AI did not give: no percentage, score, vendor or model', async () => {
      const user = userEvent.setup()
      renderDialog()
      await ask(user)

      await screen.findByRole('button', { name: 'Use this slot' })
      const text = screen.getByRole('dialog').textContent ?? ''
      expect(text).not.toContain('%')
      expect(text).not.toMatch(/confidence|score|accura|groq|gpt|llama|openai|\bms\b|candidate/i)
    })

    it('recovers when the suggested slot is taken before Book (409), without asking or re-reading in a loop', async () => {
      bookAnswer = () => {
        slots.slots[3].status = 'booked'
        return refused(409, 'RESOURCE_CONFLICT')
      }
      const user = userEvent.setup()
      renderDialog()
      const picker = await ask(user)
      await user.click(await screen.findByRole('button', { name: 'Use this slot' }))
      const before = slotRequests().length

      await submit(user)

      expect(await screen.findByText('Time unavailable')).toBeInTheDocument()
      await waitFor(() => expect(status().textContent).toBe(NO_LONGER_FREE))
      expect(screen.queryByRole('button', { name: 'Use this slot' })).not.toBeInTheDocument()
      for (const button of picker.getAllByRole('button')) {
        expect(button).toHaveAttribute('aria-pressed', 'false')
      }
      expect(slotRequests()).toHaveLength(before + 1)
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(slotRequests()).toHaveLength(before + 1)
      expect(recommendRequests()).toHaveLength(1)

      bookAnswer = () => ok(booked, 201)
      await user.click(picker.getByRole('button', { name: time(SLOTS.slots[0].start) }))
      await submit(user)

      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
      expect(toastSuccess).toHaveBeenCalledTimes(1)
      // One refused, one accepted — and the accepted one is the slot picked by hand.
      expect(bookings()).toHaveLength(2)
      expect(JSON.parse(bookings()[1].data as string)).toMatchObject({
        scheduled_start: SLOTS.slots[0].start,
        scheduled_end: SLOTS.slots[0].end,
      })
      expect(recommendRequests()).toHaveLength(1)
    })

    it.each<[string, () => Outcome]>([
      ['is still being re-read', () => ok(slots)],
      ['could not be re-read', () => ({ status: 500, data: { success: false, message: 'Internal error.' } })],
    ])('does not offer "Use this slot" while the day %s', async (_name, reread) => {
      let hold = false
      let release: () => void = () => {}
      const answer = onGet
      onGet = (config) =>
        hold && (config.url ?? '').endsWith('/slots')
          ? new Promise<Outcome>((resolve) => (release = () => resolve(reread())))
          : answer(config)
      bookAnswer = () => refused(409, 'RESOURCE_CONFLICT')
      const user = userEvent.setup()
      renderDialog()
      await ask(user)
      await user.click(await screen.findByRole('button', { name: 'Use this slot' }))
      const before = slotRequests().length
      hold = true

      // The 409 makes the booking hook re-read the day; that read is held open.
      await submit(user)

      expect(await screen.findByText('Time unavailable')).toBeInTheDocument()
      await waitFor(() => expect(slotRequests()).toHaveLength(before + 1))
      expect(screen.queryByRole('button', { name: 'Use this slot' })).not.toBeInTheDocument()

      release()
      await new Promise((resolve) => setTimeout(resolve, 50))

      if (reread().status === 200) {
        // Still free in the settled feed: it can be chosen again.
        expect(await screen.findByRole('button', { name: 'Use this slot' })).toBeInTheDocument()
      } else {
        expect(screen.queryByRole('button', { name: 'Use this slot' })).not.toBeInTheDocument()
      }
      expect(slotRequests()).toHaveLength(before + 1)
      expect(recommendRequests()).toHaveLength(1)
    })

    it('re-reads the slots once when the suggested slot is not one the picker shows as free', async () => {
      recommendAnswer = () => recommended(SLOTS.slots[1])
      const user = userEvent.setup()
      renderDialog()
      await chooseDoctorAndDate(user)
      const button = await suggestButton()
      const before = slotRequests().length

      await user.click(button)

      await waitFor(() => expect(status().textContent).toBe(NO_LONGER_FREE))
      await waitFor(() => expect(slotRequests()).toHaveLength(before + 1))
      const reread = slotRequests()[before]
      expect(reread.method).toBe('get')
      expect(`${reread.baseURL}${reread.url}`).toBe('/api/v1/doctors/doc-1/slots')
      expect(reread.params).toEqual({ date: '2030-01-07' })
      await user.type(screen.getByLabelText(/Reason/), 'Persistent cough')
      await new Promise((resolve) => setTimeout(resolve, 50))

      expect(slotRequests()).toHaveLength(before + 1)
      expect(recommendRequests()).toHaveLength(1)
    })

    it('still shows the answer when the re-read day has no free slot left', async () => {
      recommendAnswer = () => {
        // Another desk took both free slots while the form was open. A new
        // object, as a real response is: the cache holds the one served before.
        slots = structuredClone(slots)
        slots.slots[0].status = 'booked'
        slots.slots[3].status = 'booked'
        return noFreeSlots()
      }
      const user = userEvent.setup()
      renderDialog()
      const picker = await chooseDoctorAndDate(user)
      const button = await suggestButton()
      const before = slotRequests().length

      await user.click(button)

      await waitFor(() => expect(slotRequests()).toHaveLength(before + 1))
      await picker.findByRole('button', { name: `${time(SLOTS.slots[0].start)}, booked` })
      // The control and its answer stay, and the pressed button keeps focus.
      expect(status().textContent).toBe('There are no upcoming free slots on this day to suggest.')
      const after = await suggestButton()
      expect(after).toBe(button)
      expect(document.activeElement).toBe(button)
      // Nothing is left to suggest, so asking again is switched off.
      expect(after).toHaveAttribute('aria-disabled', 'true')
      expect(after).not.toHaveAttribute('disabled')
      await user.click(after)
      fireEvent.click(after)
      expect(recommendRequests()).toHaveLength(1)
      expect(slotRequests()).toHaveLength(before + 1)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('still shows the answer when the last free slot starts between opening the form and asking', async () => {
      vi.useFakeTimers({ toFake: ['Date'] })
      vi.setSystemTime(new Date('2030-01-07T10:29:00+05:30'))
      slots.slots[0].status = 'booked'
      let finish: (outcome: Outcome) => void = () => {}
      recommendAnswer = () => new Promise<Outcome>((resolve) => (finish = resolve))
      const user = userEvent.setup()
      renderDialog()
      await chooseDoctorAndDate(user)
      const button = await suggestButton()

      // The 10:30 slot has now started: the server will find nothing to suggest.
      vi.setSystemTime(new Date('2030-01-07T10:31:00+05:30'))
      await user.click(button)

      const busy = await screen.findByRole('button', { name: 'Getting a suggestion…' })
      expect(busy).toBe(button)
      expect(status().textContent).toBe('Getting a suggestion…')
      expect(recommendRequests()).toHaveLength(1)

      finish(noFreeSlots())

      await waitFor(() =>
        expect(status().textContent).toBe('There are no upcoming free slots on this day to suggest.'),
      )
      expect(await suggestButton()).toBe(button)
      expect(button).toHaveAttribute('aria-disabled', 'true')
      await user.click(button)
      expect(recommendRequests()).toHaveLength(1)
    })

    it('re-reads the slots once when the server finds none free, and never again on re-render', async () => {
      recommendAnswer = () => noFreeSlots()
      const user = userEvent.setup()
      renderDialog()
      await chooseDoctorAndDate(user)
      const button = await suggestButton()
      const before = slotRequests().length

      await user.click(button)

      await waitFor(() =>
        expect(status().textContent).toBe('There are no upcoming free slots on this day to suggest.'),
      )
      await waitFor(() => expect(slotRequests()).toHaveLength(before + 1))
      await user.type(screen.getByLabelText(/Reason/), 'Persistent cough')
      await new Promise((resolve) => setTimeout(resolve, 50))

      expect(slotRequests()).toHaveLength(before + 1)
      expect(recommendRequests()).toHaveLength(1)
    })
  })
})
