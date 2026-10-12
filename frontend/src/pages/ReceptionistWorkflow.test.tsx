import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { AppointmentSummary } from '@/api/appointments'
import type { Invoice, Payment } from '@/api/billing'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { formatMoney, todayISODate } from '@/lib/format'
import { signIn, signOut } from '@/test/auth'
import { issuedInvoice, draftInvoice, payment, summaryOf } from '@/test/billingFixtures'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import { receptionDashboardFixture } from '@/test/reportFixtures'
import DashboardPage from './DashboardPage'
import PatientsPage from './patients/PatientsPage'
import PatientDetailPage from './patients/PatientDetailPage'
import AppointmentsPage from './appointments/AppointmentsPage'
import BillingPage from './billing/BillingPage'
import InvoiceDetailPage from './billing/InvoiceDetailPage'

/**
 * The front-desk journey, as a receptionist, through the real pages, routes,
 * permission checks and hooks. Only the network is replaced, by an in-memory
 * server that enforces the same rules the API does for this role: booking
 * needs an Idempotency-Key, a payment by anything but cash is a 403.
 *
 * The permission set is the seeded Receptionist role
 * (backend/app/seeds/seed.py). Note what is NOT in it: `patient.update`,
 * `appointment.start`, `appointment.complete`, `appointment.book_override`,
 * and every invoice write except the cash payment.
 */
const RECEPTIONIST = [
  'notification.read.own',
  'notification.preference.update.own',
  'patient.read',
  'patient.create',
  'appointment.read',
  'appointment.book',
  'appointment.reschedule',
  'appointment.cancel',
  'appointment.check_in',
  'appointment.recommend_slot',
  'service.read',
  'invoice.read',
  'invoice.payment.record.cash',
  'department.read',
  'doctor.read',
  'doctor.availability.read',
  // The reception dashboard only; it opens no report.
  'report.reception.read',
]

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const PATIENT = {
  id: 'pat-1',
  hospital_id: 'h1',
  mrn: 'MRN-2026-00004',
  first_name: 'Thomas',
  last_name: 'George',
  full_name: 'Thomas George',
  date_of_birth: '1965-01-09',
  age: 61,
  gender: 'male',
  phone: '+919812345604',
  status: 'active',
  blood_group: 'AB+',
  email: null,
  address: null,
  emergency_contact: null,
  marital_status: null,
  occupation: null,
  allergies: [],
  chronic_conditions: [],
  current_medications: [],
  notes: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}
const DOCTOR = { id: 'doc-1', user_id: 'u9', full_name: 'Priya Sharma', specialization: 'Cardiology', status: 'active' }
const BOOKING_DAY = '2030-01-07'
const SLOT = { start: `${BOOKING_DAY}T09:00:00+05:30`, end: `${BOOKING_DAY}T09:30:00+05:30` }
const slotLabel = new Date(SLOT.start).toLocaleTimeString(undefined, {
  timeStyle: 'short',
  timeZone: 'Asia/Kolkata',
})

/** An appointment plus the calendar day the server files it under. */
type StoredAppointment = AppointmentSummary & { day: string }

let api: FakeApi
let appointments: StoredAppointment[]
let invoices: Invoice[]
let payments: Payment[]
/** Override to make the next write fail. */
let onWrite: ((config: InternalAxiosRequestConfig) => Outcome | null) | null

function appointment(overrides: Partial<StoredAppointment>): StoredAppointment {
  return {
    id: 'a-today',
    patient_id: 'pat-2',
    patient_name: 'Meera Nair',
    doctor_id: 'doc-1',
    doctor_name: 'Priya Sharma',
    scheduled_start: '2030-01-01T09:30:00Z',
    scheduled_end: '2030-01-01T10:00:00Z',
    status: 'booked',
    type: 'new',
    day: todayISODate(),
    ...overrides,
  }
}

const page = (items: unknown[]): Outcome => ({
  status: 200,
  data: {
    success: true,
    message: 'ok',
    // Copies, as a real response would be: the stored rows are changed in place later.
    data: structuredClone(items),
    metadata: { pagination: { page: 1, page_size: 25, total_records: items.length, total_pages: 1 } },
  },
})

function read(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const params = (config.params ?? {}) as Record<string, unknown>
  if (url === '/patients') {
    const q = typeof params.q === 'string' ? params.q.toLowerCase() : ''
    return page(!q || PATIENT.first_name.toLowerCase().startsWith(q) ? [PATIENT] : [])
  }
  if (url === '/patients/pat-1') return ok(PATIENT)
  if (url === '/dashboards/reception') {
    // The hospital's day is the day this server files today's appointments under.
    return ok({
      ...receptionDashboardFixture,
      meta: { ...receptionDashboardFixture.meta, today: todayISODate() },
    })
  }
  if (url === '/doctors') return page([DOCTOR])
  if (url === '/doctors/doc-1/slots') {
    const taken = appointments.some((a) => a.scheduled_start === SLOT.start && a.status === 'booked')
    return ok({
      date: params.date,
      doctor_id: 'doc-1',
      timezone: 'Asia/Kolkata',
      slots: [{ ...SLOT, status: taken ? 'booked' : 'available', appointment_id: null }],
    })
  }
  if (url === '/appointments') {
    return page(
      appointments
        .filter((a) => !params.date || a.day === params.date)
        .filter((a) => !params.patient_id || a.patient_id === params.patient_id)
        .filter((a) => !params.status || a.status === params.status),
    )
  }
  if (url === '/invoices') {
    return page(
      invoices
        .filter((i) => !params.status || i.status === params.status)
        .filter((i) => !params.patient_id || i.patient_id === params.patient_id)
        .map(summaryOf),
    )
  }
  const invoice = invoices.find((i) => url.startsWith(`/invoices/${i.id}`))
  if (invoice) {
    if (url.endsWith('/payments')) return ok(payments.filter((p) => p.invoice_id === invoice.id))
    if (url.endsWith('/refunds')) return ok([])
    return ok(structuredClone(invoice))
  }
  return fail(404, 'Not found.')
}

function write(config: InternalAxiosRequestConfig): Outcome {
  const refused = onWrite?.(config)
  if (refused) return refused
  const url = config.url ?? ''
  if (url === '/appointments') {
    if (!config.headers.get('Idempotency-Key')) {
      return fail(422, 'Validation failed.', {
        errors: [{ field: 'header.Idempotency-Key', message: 'Field required' }],
      })
    }
    const body = bodyOf(config) as { patient_id: string; scheduled_start: string; scheduled_end: string }
    const created = appointment({
      id: `a-${appointments.length + 1}`,
      patient_id: body.patient_id,
      patient_name: PATIENT.full_name,
      scheduled_start: body.scheduled_start,
      scheduled_end: body.scheduled_end,
      day: BOOKING_DAY,
    })
    appointments.push(created)
    return ok(created, 201)
  }
  const transition = /^\/appointments\/([^/]+)\/(check-in|cancel)$/.exec(url)
  if (transition) {
    const found = appointments.find((a) => a.id === transition[1])
    if (!found) return fail(404, 'Appointment not found.')
    found.status = transition[2] === 'cancel' ? 'cancelled' : 'checked_in'
    return ok({ ...found })
  }
  const pay = /^\/invoices\/([^/]+)\/payments$/.exec(url)
  if (pay) {
    const body = bodyOf(config) as { amount: string; method: Payment['method'] }
    // The receptionist's code is `invoice.payment.record.cash`.
    if (body.method !== 'cash') {
      return fail(403, 'Your role can record cash payments only.', { error_code: 'PERMISSION_DENIED' })
    }
    const invoice = invoices.find((i) => i.id === pay[1]) as Invoice
    // The test server settles the whole balance; the API does the arithmetic for real.
    Object.assign(invoice, { status: 'paid', amount_paid: invoice.total, balance_due: '0.00' })
    const recorded = payment({ id: 'pay-new', invoice_id: invoice.id, reference: null, ...body })
    payments.push(recorded)
    return ok({ payment: recorded, invoice: summaryOf(invoice) }, 201)
  }
  return fail(403, 'Permission denied.', { error_code: 'PERMISSION_DENIED' })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  onWrite = null
  appointments = [appointment({})]
  invoices = [
    issuedInvoice({ id: 'inv-issued', appointment_id: null, patient_name: 'Ishaan Kulkarni', patient_id: 'pat-3' }),
    draftInvoice({ id: 'inv-draft', appointment_id: null, patient_name: 'Fatima Sheikh', patient_id: 'pat-4' }),
  ]
  payments = []
  api = installFakeApi((config) => (config.method === 'get' ? read(config) : write(config)))
})

afterEach(() => {
  api.restore()
  signOut()
})

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>
}

function renderApp(path: string) {
  signIn(RECEPTIONIST)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Where />
        <Routes>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route
            path="/patients"
            element={
              <RequirePermission permission="patient.read">
                <PatientsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/patients/:patientId"
            element={
              <RequirePermission permission="patient.read">
                <PatientDetailPage />
              </RequirePermission>
            }
          />
          <Route
            path="/appointments"
            element={
              <RequirePermission permission="appointment.read">
                <AppointmentsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/billing"
            element={
              <RequirePermission group="invoice.read">
                <BillingPage />
              </RequirePermission>
            }
          />
          <Route
            path="/billing/:invoiceId"
            element={
              <RequirePermission group="invoice.read">
                <InvoiceDetailPage />
              </RequirePermission>
            }
          />
          <Route
            path="/users"
            element={
              <RequirePermission permission="user.read">
                <p>Users admin</p>
              </RequirePermission>
            }
          />
          <Route
            path="/settings"
            element={
              <RequirePermission permission="settings.read">
                <p>Settings admin</p>
              </RequirePermission>
            }
          />
          <Route
            path="/reports"
            element={
              <RequirePermission group="report.admin.read">
                <p>Reports</p>
              </RequirePermission>
            }
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const where = () => screen.getByTestId('where').textContent
const writes = () => api.sent.filter((c) => c.method !== 'get')
const region = (name: string) => screen.findByRole('region', { name })

describe('receptionist: where they can go', () => {
  it.each(['/patients', '/appointments', '/billing'])('reaches %s', async (path) => {
    renderApp(path)
    await waitFor(() => expect(where()).toBe(path))
    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument()
  })

  it.each(['/users', '/settings', '/reports'])('is sent home from %s', async (path) => {
    renderApp(path)
    await waitFor(() => expect(where()).toBe('/dashboard'))
    expect(screen.queryByText(/admin|Reports/)).not.toBeInTheDocument()
  })
})

describe('receptionist: dashboard', () => {
  it('offers the front-desk actions and today\'s queue with check-in', async () => {
    renderApp('/dashboard')

    expect(screen.getByRole('button', { name: /Register Patient/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Book Appointment/ })).toBeInTheDocument()
    const row = await screen.findByRole('row', { name: /Meera Nair/ })
    expect(within(row).getByRole('button', { name: 'Check in Meera Nair' })).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /Cancel appointment/ })).toBeInTheDocument()
    // The front-desk tiles come from one read of the reception dashboard.
    expect(await screen.findByRole('region', { name: 'Front desk' })).toBeInTheDocument()
    expect(api.requests('get', '/dashboards/')).toHaveLength(1)
    expect(api.requests('get', '/dashboards/')[0].url).toBe('/dashboards/reception')
    // The patient's name opens their record.
    expect(within(row).getByRole('link', { name: 'Meera Nair' })).toHaveAttribute('href', '/patients/pat-2')
  })

  it('checks a patient in from the home page, and offers nothing a doctor does next', async () => {
    const user = userEvent.setup()
    renderApp('/dashboard')

    const row = await screen.findByRole('row', { name: /Meera Nair/ })
    await user.click(within(row).getByRole('button', { name: 'Check in Meera Nair' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Patient checked in'))
    expect(writes()[0].url).toBe('/appointments/a-today/check-in')
    // A check-in moves the front-desk tiles, so they are read again.
    await waitFor(() => expect(api.requests('get', '/dashboards/reception')).toHaveLength(2))
    const after = await screen.findByRole('row', { name: /Meera Nair/ })
    expect(within(after).getByText('Checked in')).toBeInTheDocument()
    // Starting and completing a consultation are not the receptionist's.
    expect(within(after).queryByRole('button', { name: /Start|Complete/ })).not.toBeInTheDocument()
    expect(within(after).getByRole('button', { name: /Cancel appointment/ })).toBeInTheDocument()
  })

  it('lists the invoices a payment can be taken against', async () => {
    renderApp('/dashboard')

    const waiting = within(await region('Awaiting payment'))
    const link = await waiting.findByRole('link', { name: /Ishaan Kulkarni/ })
    expect(link).toHaveAttribute('href', '/billing/inv-issued')
    expect(link).toHaveTextContent(formatMoney('600.00', 'INR'))
    // A draft cannot take a payment, so it is not listed.
    expect(waiting.queryByText(/Fatima Sheikh/)).not.toBeInTheDocument()
    const statuses = api.requests('get').filter((c) => c.url === '/invoices').map((c) => c.params.status)
    expect(statuses.sort()).toEqual(['issued', 'partially_paid'])
  })

  it('reports a failed check-in without internal detail and leaves the row usable', async () => {
    onWrite = () => fail(500, 'Traceback (most recent call last)…')
    const user = userEvent.setup()
    renderApp('/dashboard')

    const row = await screen.findByRole('row', { name: /Meera Nair/ })
    await user.click(within(row).getByRole('button', { name: 'Check in Meera Nair' }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't check in the appointment. Please try again."),
    )
    expect(within(row).getByText('Booked')).toBeInTheDocument()
    await waitFor(() => expect(within(row).getByRole('button', { name: 'Check in Meera Nair' })).toBeEnabled())
  })
})

describe('receptionist: patient to appointment', () => {
  it('finds a patient by name and opens the record, which cannot be edited', async () => {
    const user = userEvent.setup()
    renderApp('/patients')

    await user.type(await screen.findByPlaceholderText(/Search name, MRN or phone/), 'Thom')
    await waitFor(() =>
      expect(api.requests('get').some((c) => c.url === '/patients' && c.params.q === 'Thom')).toBe(true),
    )
    await user.click(await screen.findByRole('link', { name: 'View Thomas George, MRN-2026-00004' }))

    expect(await screen.findByRole('heading', { name: 'Thomas George' })).toBeInTheDocument()
    // No `patient.update` in the seeded role: the API would answer 403.
    expect(screen.queryByRole('button', { name: /Edit patient/ })).not.toBeInTheDocument()
    expect(await region('Appointments')).toHaveTextContent('No appointments for this patient yet.')
  })

  it('books from the patient\'s record into one of the doctor\'s slots, and shows the booking', async () => {
    const user = userEvent.setup()
    renderApp('/patients/pat-1')

    const section = within(await region('Appointments'))
    await user.click(await section.findByRole('button', { name: /Book appointment/ }))
    const dialog = await screen.findByRole('dialog')
    // The patient is already chosen.
    expect(within(dialog).getByText(/MRN-2026-00004/)).toBeInTheDocument()

    await user.click(within(dialog).getByRole('combobox', { name: /Doctor/ }))
    await user.click(await screen.findByRole('option', { name: /Priya Sharma/ }))
    await user.type(within(dialog).getByLabelText(/Date/), BOOKING_DAY)
    await user.click(await within(dialog).findByRole('button', { name: slotLabel }))
    await user.click(within(dialog).getByRole('button', { name: /^Book$/ }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment booked'))
    const [request] = writes()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/appointments')
    expect(bodyOf(request)).toEqual({
      patient_id: 'pat-1',
      doctor_id: 'doc-1',
      scheduled_start: SLOT.start,
      scheduled_end: SLOT.end,
      type: 'new',
    })
    expect(String(request.headers.get('Idempotency-Key')).length).toBeGreaterThanOrEqual(8)

    // It persists: the record now lists it.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await section.findByText('Booked')).toBeInTheDocument()
    expect(section.getByText(/Priya Sharma/)).toBeInTheDocument()
  })

  it('shows the API\'s reason when a booking is refused', async () => {
    onWrite = () =>
      fail(400, "That time is outside the doctor's availability. Book a published slot, or retry with the override permission.", {
        error_code: 'BUSINESS_RULE_VIOLATION',
      })
    const user = userEvent.setup()
    renderApp('/patients/pat-1')

    const section = within(await region('Appointments'))
    await user.click(await section.findByRole('button', { name: /Book appointment/ }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('combobox', { name: /Doctor/ }))
    await user.click(await screen.findByRole('option', { name: /Priya Sharma/ }))
    await user.type(within(dialog).getByLabelText(/Date/), BOOKING_DAY)
    await user.click(await within(dialog).findByRole('button', { name: slotLabel }))
    await user.click(within(dialog).getByRole('button', { name: /^Book$/ }))

    expect(await within(dialog).findByText(/outside the doctor's availability/)).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('cancels an appointment from the queue, with a reason', async () => {
    const user = userEvent.setup()
    renderApp('/appointments')

    const row = await screen.findByRole('row', { name: /Meera Nair/ })
    await user.click(within(row).getByRole('button', { name: 'Cancel appointment for Meera Nair' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Patient asked to cancel')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel appointment' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Appointment cancelled'))
    const [request] = writes()
    expect(request.url).toBe('/appointments/a-today/cancel')
    expect(bodyOf(request)).toEqual({ reason: 'Patient asked to cancel' })
    const after = await screen.findByRole('row', { name: /Meera Nair/ })
    expect(within(after).getByText('Cancelled')).toBeInTheDocument()
    expect(within(after).queryAllByRole('button')).toHaveLength(0)
  })
})

describe('receptionist: billing', () => {
  it('sees invoices but cannot raise one', async () => {
    renderApp('/billing')

    expect(await screen.findByRole('row', { name: /Ishaan Kulkarni/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New invoice/ })).not.toBeInTheDocument()
  })

  it('takes a cash payment on an issued invoice — cash being the only method offered', async () => {
    const user = userEvent.setup()
    renderApp('/billing/inv-issued')

    const actions = within(await screen.findByRole('group', { name: 'Invoice actions' }))
    // Nothing but the payment: no void, refund, discount or edit.
    expect(actions.getAllByRole('button').map((b) => b.textContent?.trim())).toEqual(['Record payment'])

    await user.click(actions.getByRole('button', { name: 'Record payment' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/cash payments only/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('combobox', { name: /Method/ }))
    expect((await screen.findAllByRole('option')).map((o) => o.textContent)).toEqual(['Cash'])
    await user.keyboard('{Escape}')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith('Payment recorded — invoice paid in full'),
    )
    const [request] = writes()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices/inv-issued/payments')
    expect(bodyOf(request)).toEqual({ amount: '600.00', method: 'cash' })
    expect(String(request.headers.get('Idempotency-Key')).length).toBeGreaterThanOrEqual(16)

    expect(await screen.findByText('Paid', { selector: 'span' })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Invoice actions' })).not.toBeInTheDocument()
  })

  it('has no action at all on a draft invoice', async () => {
    renderApp('/billing/inv-draft')

    expect(await screen.findByRole('heading', { name: 'Draft invoice' })).toBeInTheDocument()
    await screen.findByRole('region', { name: 'Totals' })
    expect(screen.queryByRole('group', { name: 'Invoice actions' })).not.toBeInTheDocument()
  })
})
