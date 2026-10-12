import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type {
  AppointmentsReport,
  NamedRef,
  OutstandingReport,
  PatientsReport,
  RevenueReport,
} from '@/api/reports'
import { signIn, signOut } from '@/test/auth'
import { fail, installFakeApi, ok, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import {
  appointmentsReportFixture,
  outstandingReportFixture,
  patientsReportFixture,
  revenueReportFixture,
} from '@/test/reportFixtures'
import AppointmentsReportPage from './AppointmentsReportPage'
import { APPOINTMENT_SERIES } from './reportPresentation'
import OutstandingReportPage from './OutstandingReportPage'
import PatientsReportPage from './PatientsReportPage'
import RevenueReportPage from './RevenueReportPage'

/**
 * The four report screens against the contract's exact shapes
 * (docs/modules/10-reports-dashboard.md §9). The real hooks,
 * permission check, `http` wrapper and Axios instance run; only the network
 * adapter is replaced by an in-memory "server" that answers with the
 * contract's example payloads and — like the real one — is the only thing
 * that knows the hospital's day, resolves a period that was not sent, names
 * the doctor it filtered on, and refuses a filter it will not accept.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({ toastSuccess: vi.fn(), toastError: vi.fn() }))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → the codes these screens look at, as seeded (backend/app/seeds/seed.py).
const ADMIN = [
  'report.admin.read',
  'report.doctor.read',
  'report.reception.read',
  'report.billing.read',
  'report.export',
  'doctor.read',
  'department.read',
  'invoice.read',
]
const BILLING = ['report.billing.read', 'report.export', 'invoice.read', 'patient.read']

// ── The in-memory server ────────────────────────────────────────────────────

const PRIYA = '5b0c1c7e-2f0a-4d0e-9c53-0a1d6f3b9a11'
const ARJUN = '8e1f4a22-91c3-4b6d-a7f2-5c0d9e8b7a12'
const MEERA = 'a94d2b33-0e7f-4c8a-b1d5-6e2f0a9c8b13'
/** A doctor the server knows — and has appointments for — who is not in the doctor list. */
const VIKRAM = 'bb5e3c44-1f80-4d9b-c2e6-7f3a1b0d9c14'
const CARDIOLOGY = 'c2a4e0f1-7b55-4a0c-8f0e-3d2b1a9e6c01'
const ORTHOPAEDICS = 'd3b5f1a2-8c66-4b1d-9a1f-4e3c2b0f7d02'

const DOCTOR_NAMES: Record<string, string> = {
  [PRIYA]: 'Priya Sharma',
  [ARJUN]: 'Arjun Nair',
  [MEERA]: 'Meera Krishnan',
  [VIKRAM]: 'Vikram Desai',
}
const DEPARTMENT_NAMES: Record<string, string> = { [CARDIOLOGY]: 'Cardiology', [ORTHOPAEDICS]: 'Orthopaedics' }

const doctorRow = (id: string, department_id: string | null, status = 'active') => ({
  id,
  user_id: `user-${id}`,
  full_name: DOCTOR_NAMES[id],
  specialization: 'General',
  department_id,
  department_name: department_id ? DEPARTMENT_NAMES[department_id] : null,
  consultation_fee: '500.00',
  status,
})
const LISTED_DOCTORS = [
  doctorRow(PRIYA, CARDIOLOGY),
  doctorRow(ARJUN, ORTHOPAEDICS),
  doctorRow(MEERA, null, 'inactive'),
]
const LISTED_DEPARTMENTS = [
  { id: CARDIOLOGY, code: 'CARD', name: 'Cardiology', location: null, status: 'active' },
  { id: ORTHOPAEDICS, code: 'ORTH', name: 'Orthopaedics', location: null, status: 'active' },
]

/** What the server answers each report with; a test replaces one to vary it. */
let patients: PatientsReport
let appointments: AppointmentsReport
let revenue: RevenueReport
let outstanding: OutstandingReport
/** The hospital's day, which only the server knows. */
let today: string
/** Return an outcome to answer a request yourself; nothing to let the server answer. */
let intercept: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome> | undefined
let fake: FakeApi

const rule = (field: string, message: string): Outcome =>
  fail(422, message, { error_code: 'VALIDATION_ERROR', errors: { errors: [{ field, message }] } })

type Query = Record<string, string | undefined>

/** The period the server resolves: what was sent, else what the example payload carries. */
function resolvePeriod<T extends { from: string; to: string; granularity: string }>(defaults: T, query: Query) {
  return {
    from: query.from ?? defaults.from,
    to: query.to ?? defaults.to,
    granularity: (query.granularity ?? defaults.granularity) as T['granularity'],
  }
}

function refuse(query: Query, allowed: string[]): Outcome | undefined {
  const unknown = Object.keys(query).find((key) => query[key] !== undefined && !allowed.includes(key))
  if (unknown) return rule(unknown, `Unknown query parameter: \`${unknown}\`.`)
  if (query.from && query.to && query.to < query.from) return rule('to', '`to` must not be before `from`.')
  if (query.granularity && !['day', 'week', 'month'].includes(query.granularity)) {
    return rule('granularity', '`granularity` must be one of: day, week, month.')
  }
  return undefined
}

async function server(config: InternalAxiosRequestConfig): Promise<Outcome> {
  const answered = await intercept(config)
  if (answered) return answered
  const query = (config.params ?? {}) as Query
  const meta = (m: PatientsReport['meta']) => ({ ...m, today })
  const period = ['from', 'to', 'granularity']

  switch (config.url) {
    case '/reports/patients':
      return (
        refuse(query, period) ??
        ok({ ...patients, meta: meta(patients.meta), filters: resolvePeriod(patients.filters, query) })
      )
    case '/reports/appointments': {
      const refused = refuse(query, [...period, 'doctor_id', 'department_id'])
      if (refused) return refused
      if (query.doctor_id && !DOCTOR_NAMES[query.doctor_id]) return rule('doctor_id', 'Doctor not found.')
      if (query.department_id && !DEPARTMENT_NAMES[query.department_id]) {
        return rule('department_id', 'Department not found.')
      }
      const named = (id: string | undefined, names: Record<string, string>): NamedRef | null =>
        id ? { id, name: names[id] } : null
      return ok({
        ...appointments,
        meta: meta(appointments.meta),
        filters: {
          ...resolvePeriod(appointments.filters, query),
          doctor: named(query.doctor_id, DOCTOR_NAMES),
          department: named(query.department_id, DEPARTMENT_NAMES),
        },
      })
    }
    case '/reports/revenue':
      return (
        refuse(query, period) ??
        ok({ ...revenue, meta: meta(revenue.meta), filters: resolvePeriod(revenue.filters, query) })
      )
    case '/reports/outstanding':
      return refuse(query, []) ?? ok({ ...outstanding, meta: meta(outstanding.meta) })
    case '/doctors':
      return paged(LISTED_DOCTORS.filter((d) => !query.department || d.department_id === query.department))
    case '/departments':
      return paged(LISTED_DEPARTMENTS)
    default:
      return fail(404, 'Not found.', { error_code: 'RESOURCE_NOT_FOUND' })
  }
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  patients = patientsReportFixture
  appointments = appointmentsReportFixture
  revenue = revenueReportFixture
  outstanding = outstandingReportFixture
  today = '2026-10-06'
  intercept = () => undefined
  fake = installFakeApi(server)
})

afterEach(() => {
  fake.restore()
  signOut()
})

// ── Rendering and reading the screen ────────────────────────────────────────

function Address() {
  const location = useLocation()
  const navigation = useNavigationType()
  return (
    <>
      <p aria-label="address">{location.pathname + location.search}</p>
      <p aria-label="navigation">{navigation}</p>
    </>
  )
}

function renderAt(path: string, permissions: string[] = ADMIN) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Address />
        <Routes>
          <Route path="/reports/patients" element={<PatientsReportPage />} />
          <Route path="/reports/appointments" element={<AppointmentsReportPage />} />
          <Route path="/reports/revenue" element={<RevenueReportPage />} />
          <Route path="/reports/outstanding" element={<OutstandingReportPage />} />
          <Route path="*" element={<p>another page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

type User = ReturnType<typeof userEvent.setup>

/** Money as the screen should show it — computed here from a literal, not from the payload. */
const inr = (amount: number) => new Intl.NumberFormat(undefined, { style: 'currency', currency: 'INR' }).format(amount)

const address = () => screen.getByLabelText('address').textContent
const navigation = () => screen.getByLabelText('navigation').textContent
const sentTo = (url: string) => fake.sent.filter((config) => config.url === url)
const lastTo = (url: string) => {
  const all = sentTo(url)
  return all[all.length - 1]
}
/** The query a request carried, without the keys that had no value (Axios does not send those). */
const queryOf = (config: InternalAxiosRequestConfig) =>
  Object.fromEntries(Object.entries((config.params ?? {}) as Query).filter(([, value]) => value !== undefined))
const reportRequests = () => fake.sent.filter((config) => (config.url ?? '').startsWith('/reports/'))
const skeletons = () => document.querySelectorAll('[data-slot="skeleton"]').length

/** A headline figure's tile: its label, value and hint. */
function tile(label: string): HTMLElement {
  const heading = screen
    .getAllByText(label)
    .find((el) => el.tagName === 'P' && el.className.includes('text-label-caps'))
  if (!heading) throw new Error(`No tile labelled ${label}`)
  return heading.parentElement?.parentElement as HTMLElement
}

/** The table whose first column is `firstHeader`. */
function table(firstHeader: string): HTMLElement {
  const found = screen
    .getAllByRole('table')
    .find((t) => within(t).getAllByRole('columnheader')[0].textContent === firstHeader)
  if (!found) throw new Error(`No table starting with ${firstHeader}`)
  return found
}
const headersOf = (t: HTMLElement) => within(t).getAllByRole('columnheader').map((h) => h.textContent)
const rowsOf = (t: HTMLElement) =>
  within(t)
    .getAllByRole('row')
    .slice(1)
    .map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent))

const ready = () => screen.findByText(/All figures in Asia\/Kolkata/)
const fromInput = () => screen.getByLabelText('From') as HTMLInputElement
const toInput = () => screen.getByLabelText('To') as HTMLInputElement
const groupBy = () => screen.getByRole('combobox', { name: 'Group by' })
const preset = (name: string) => screen.getByRole('button', { name })

async function choose(user: User, combobox: HTMLElement, option: string) {
  await waitFor(() => expect(combobox).toBeEnabled())
  await user.click(combobox)
  await user.click(await screen.findByRole('option', { name: option }))
}

/** A request that stays unanswered until the test lets it go. */
function held() {
  let release: (outcome: Outcome) => void = () => {}
  const promise = new Promise<Outcome>((resolve) => {
    release = resolve
  })
  return { promise, release }
}

// ── Patients ────────────────────────────────────────────────────────────────

describe('patients report', () => {
  it('asks for the report with no filters and shows the server\'s figures', async () => {
    renderAt('/reports/patients')

    expect(await screen.findByText('All figures in Asia/Kolkata')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Patients report' })).toBeInTheDocument()

    expect(reportRequests()).toHaveLength(1)
    const sent = lastTo('/reports/patients')
    expect(sent.method).toBe('get')
    // Nothing is in the address, so nothing is sent: the defaults are the server's.
    expect(queryOf(sent)).toEqual({})

    expect(tile('Registered in period')).toHaveTextContent('Registered in period11')
    expect(tile('Active patients')).toHaveTextContent('Active patients11')
    expect(tile('Female')).toHaveTextContent('Female5')
    expect(tile('Male')).toHaveTextContent('Male6')
    // Zero in the payload: the two rarer answers get no tile.
    expect(screen.queryByText('Other')).not.toBeInTheDocument()
    expect(screen.queryByText('Unspecified')).not.toBeInTheDocument()

    expect(headersOf(table('Period'))).toEqual(['Period', 'Registered'])
    expect(rowsOf(table('Period'))).toEqual([
      ['4 Oct', '0'],
      ['5 Oct', '0'],
      ['6 Oct', '11'],
      ['Total', '11'],
    ])

    // The chart is a picture of those rows; its text alternative says so in words.
    const chart = screen.getByRole('region', { name: 'Registrations' })
    expect(chart).toHaveTextContent('Patients registered per day')
    expect(chart).toHaveTextContent('11 in total')
    // No money on this report, so no currency is named.
    expect(document.body.textContent).not.toMatch(/INR|₹/)
  })

  it('shows Other and Unspecified once the server counts any', async () => {
    patients = {
      ...patientsReportFixture,
      summary: { ...patientsReportFixture.summary, by_gender: { male: 6, female: 3, other: 1, unspecified: 1 } },
    }
    renderAt('/reports/patients')
    await ready()

    expect(tile('Other')).toHaveTextContent('Other1')
    expect(tile('Unspecified')).toHaveTextContent('Unspecified1')
  })

  it('shows skeletons, and no number, until the report answers', async () => {
    const pending = held()
    intercept = (config) => (config.url === '/reports/patients' ? pending.promise : undefined)
    renderAt('/reports/patients')

    expect(await screen.findByRole('status', { name: 'Loading Registered in period' })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Loading Registrations' })).toBeInTheDocument()
    expect(skeletons()).toBeGreaterThan(4)
    expect(screen.queryByText('Total')).not.toBeInTheDocument()
    // No figure and no date is on screen yet — not even a placeholder zero.
    expect(screen.queryByText(/^\d+$/)).not.toBeInTheDocument()
    expect(fromInput().value).toBe('')
    // The presets count from the hospital's day, which has not arrived.
    expect(preset('Last 7 days')).toBeDisabled()

    pending.release(ok(patientsReportFixture))
    await ready()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(tile('Registered in period')).toHaveTextContent('11')
    expect(preset('Last 7 days')).toBeEnabled()
  })

  it('says the report could not be loaded, without the server\'s text, and retries', async () => {
    const user = userEvent.setup()
    intercept = (config) =>
      config.url === '/reports/patients'
        ? fail(500, 'psycopg.OperationalError: connection refused', { error_code: 'INTERNAL_ERROR' })
        : undefined
    renderAt('/reports/patients')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load the report")
    expect(alert).toHaveTextContent('The report could not be reached. Check your connection and try again.')
    expect(document.body.textContent).not.toMatch(/psycopg|OperationalError/)
    expect(screen.queryByText('Registered in period')).not.toBeInTheDocument()
    expect(sentTo('/reports/patients')).toHaveLength(1)

    intercept = () => undefined
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))

    await ready()
    expect(sentTo('/reports/patients')).toHaveLength(2)
    expect(queryOf(lastTo('/reports/patients'))).toEqual({})
    expect(tile('Registered in period')).toHaveTextContent('11')
  })

  it('says so when nobody was registered, and still shows the active total', async () => {
    patients = {
      ...patientsReportFixture,
      summary: { registered: 0, active_total: 11, by_gender: { male: 0, female: 0, other: 0, unspecified: 0 } },
      buckets: patientsReportFixture.buckets.map((bucket) => ({ ...bucket, registered: 0 })),
    }
    renderAt('/reports/patients')
    await ready()

    expect(screen.getByText('No patients were registered in this period.')).toBeInTheDocument()
    expect(tile('Registered in period')).toHaveTextContent('Registered in period0')
    expect(tile('Active patients')).toHaveTextContent('Active patients11')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('marks a period the selected dates cut short', async () => {
    patients = {
      ...patientsReportFixture,
      buckets: [
        { bucket_start: '2026-09-28', bucket_end: '2026-10-04', partial: true, registered: 0 },
        { bucket_start: '2026-10-05', bucket_end: '2026-10-11', partial: true, registered: 11 },
      ],
    }
    renderAt('/reports/patients?from=2026-10-04&to=2026-10-06&granularity=week')
    await ready()

    expect(rowsOf(table('Period'))).toEqual([
      ['28 Sep – 4 Octpartial', '0'],
      ['5–11 Octpartial', '11'],
      ['Total', '11'],
    ])
  })

  it('does not ask for a report the user may not read', async () => {
    renderAt('/reports/patients', BILLING)

    expect(await screen.findByRole('alert')).toHaveTextContent("You can't view this report.")
    expect(fake.sent).toHaveLength(0)
    expect(screen.queryByRole('button', { name: 'Export CSV' })).not.toBeInTheDocument()
  })
})

// ── Filters ─────────────────────────────────────────────────────────────────

describe('report filters', () => {
  it('reads the period from the address and sends exactly that', async () => {
    renderAt('/reports/patients?from=2026-09-01&to=2026-09-30&granularity=week')
    await ready()

    expect(reportRequests()).toHaveLength(1)
    expect(queryOf(lastTo('/reports/patients'))).toEqual({
      from: '2026-09-01',
      to: '2026-09-30',
      granularity: 'week',
    })
    expect(fromInput().value).toBe('2026-09-01')
    expect(toInput().value).toBe('2026-09-30')
    expect(groupBy()).toHaveTextContent('Weekly')
  })

  it('fills the inputs with the period the server resolved when the address has none', async () => {
    renderAt('/reports/patients')
    await ready()

    // The example payload's `filters`, echoed by the server.
    expect(fromInput().value).toBe('2026-10-04')
    expect(toInput().value).toBe('2026-10-06')
    expect(groupBy()).toHaveTextContent('Daily')
    // Showing a default is not choosing it: the address stays clean.
    expect(address()).toBe('/reports/patients')
  })

  it('writes a changed date to the address with the other date on screen, replacing the entry, and asks again', async () => {
    renderAt('/reports/patients')
    await ready()
    // The server's default period: 2026-10-04 to 2026-10-06.
    expect(toInput().value).toBe('2026-10-06')

    fireEvent.change(fromInput(), { target: { value: '2026-09-20' } })

    // Both ends are written, so the server cannot resolve the untouched one afresh.
    await waitFor(() => expect(address()).toBe('/reports/patients?from=2026-09-20&to=2026-10-06'))
    expect(navigation()).toBe('REPLACE')
    await waitFor(() => expect(sentTo('/reports/patients')).toHaveLength(2))
    expect(lastTo('/reports/patients').method).toBe('get')
    expect(queryOf(lastTo('/reports/patients'))).toEqual({ from: '2026-09-20', to: '2026-10-06' })
    await ready()
    expect(toInput().value).toBe('2026-10-06')

    fireEvent.change(toInput(), { target: { value: '2026-09-25' } })

    await waitFor(() => expect(address()).toBe('/reports/patients?from=2026-09-20&to=2026-09-25'))
    await waitFor(() => expect(sentTo('/reports/patients')).toHaveLength(3))
    expect(queryOf(lastTo('/reports/patients'))).toEqual({ from: '2026-09-20', to: '2026-09-25' })

    // Emptying a date takes it back out; the server's default applies again.
    fireEvent.change(fromInput(), { target: { value: '' } })
    await waitFor(() => expect(address()).toBe('/reports/patients?to=2026-09-25'))
    await waitFor(() => expect(sentTo('/reports/patients')).toHaveLength(4))
    expect(queryOf(lastTo('/reports/patients'))).toEqual({ to: '2026-09-25' })
  })

  it('keeps the From date on screen when only To is edited', async () => {
    renderAt('/reports/patients')
    await ready()
    expect(fromInput().value).toBe('2026-10-04')

    fireEvent.change(toInput(), { target: { value: '2026-10-05' } })

    await waitFor(() => expect(address()).toBe('/reports/patients?from=2026-10-04&to=2026-10-05'))
    await waitFor(() => expect(sentTo('/reports/patients')).toHaveLength(2))
    expect(queryOf(lastTo('/reports/patients'))).toEqual({ from: '2026-10-04', to: '2026-10-05' })
    await ready()
    expect(fromInput().value).toBe('2026-10-04')
  })

  it('judges an edited date against the default date on screen before asking the server', async () => {
    renderAt('/reports/patients')
    await ready()

    // After the default To of 2026-10-06: refused here, in the server's words.
    fireEvent.change(fromInput(), { target: { value: '2026-10-09' } })

    expect(await screen.findByText('`to` must not be before `from`.')).toBeInTheDocument()
    expect(address()).toBe('/reports/patients?from=2026-10-09&to=2026-10-06')
    expect(sentTo('/reports/patients')).toHaveLength(1)

    // More than twelve months before it: the same.
    fireEvent.change(fromInput(), { target: { value: '2025-10-06' } })

    expect(await screen.findByText('Date range must not exceed 12 months.')).toBeInTheDocument()
    expect(sentTo('/reports/patients')).toHaveLength(1)
  })

  it('writes only the edited date when no report has answered yet', async () => {
    const pending = held()
    intercept = (config) => (config.url === '/reports/patients' ? pending.promise : undefined)
    renderAt('/reports/patients')
    await waitFor(() => expect(sentTo('/reports/patients')).toHaveLength(1))

    fireEvent.change(fromInput(), { target: { value: '2026-09-20' } })

    await waitFor(() => expect(address()).toBe('/reports/patients?from=2026-09-20'))
    await waitFor(() => expect(sentTo('/reports/patients')).toHaveLength(2))
    expect(queryOf(lastTo('/reports/patients'))).toEqual({ from: '2026-09-20' })
    pending.release(ok(patientsReportFixture))
    await ready()
  })

  it('does not leave the last period\'s figures on screen while the next period loads', async () => {
    renderAt('/reports/patients')
    await ready()
    expect(tile('Registered in period')).toHaveTextContent('11')

    const pending = held()
    intercept = (config) => (config.url === '/reports/patients' ? pending.promise : undefined)
    fireEvent.change(fromInput(), { target: { value: '2026-09-01' } })

    // The filters now say September; the figures of October must not sit under them.
    expect(await screen.findByRole('status', { name: 'Loading Registered in period' })).toBeInTheDocument()
    expect(screen.queryByText('Total')).not.toBeInTheDocument()
    expect(fromInput().value).toBe('2026-09-01')

    pending.release(
      ok({
        ...patientsReportFixture,
        filters: { from: '2026-09-01', to: '2026-10-06', granularity: 'day' },
        summary: { ...patientsReportFixture.summary, registered: 4 },
      }),
    )
    await ready()
    expect(tile('Registered in period')).toHaveTextContent('Registered in period4')
  })

  it('asks again with the granularity when it changes', async () => {
    const user = userEvent.setup()
    renderAt('/reports/revenue')
    await ready()
    expect(queryOf(lastTo('/reports/revenue'))).toEqual({})

    await choose(user, groupBy(), 'Monthly')

    await waitFor(() => expect(address()).toBe('/reports/revenue?granularity=month'))
    expect(navigation()).toBe('REPLACE')
    await waitFor(() => expect(sentTo('/reports/revenue')).toHaveLength(2))
    expect(lastTo('/reports/revenue').method).toBe('get')
    expect(queryOf(lastTo('/reports/revenue'))).toEqual({ granularity: 'month' })
    await waitFor(() => expect(groupBy()).toHaveTextContent('Monthly'))
  })

  it('says a range that ends before it starts is not allowed, and asks the server nothing', async () => {
    renderAt('/reports/patients?from=2026-10-06&to=2026-10-01')

    expect(await screen.findByText('`to` must not be before `from`.')).toBeInTheDocument()
    expect(toInput()).toHaveAttribute('aria-invalid', 'true')
    expect(fake.sent).toHaveLength(0)
    // Nothing is drawn for a period that was never requested — no skeleton either.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByText('Registered in period')).not.toBeInTheDocument()

    fireEvent.change(toInput(), { target: { value: '2026-10-08' } })

    await ready()
    expect(screen.queryByText('`to` must not be before `from`.')).not.toBeInTheDocument()
    expect(sentTo('/reports/patients')).toHaveLength(1)
    expect(queryOf(lastTo('/reports/patients'))).toEqual({ from: '2026-10-06', to: '2026-10-08' })
  })

  it('holds back a range longer than twelve months, and lets exactly twelve through', async () => {
    renderAt('/reports/revenue?from=2026-01-01&to=2027-01-01')

    expect(await screen.findByText('Date range must not exceed 12 months.')).toBeInTheDocument()
    expect(fake.sent).toHaveLength(0)

    fireEvent.change(toInput(), { target: { value: '2026-12-31' } })

    await ready()
    expect(queryOf(lastTo('/reports/revenue'))).toEqual({ from: '2026-01-01', to: '2026-12-31' })
  })

  it('drops values that are not a date, a granularity or an id from the address and never sends them', async () => {
    renderAt(`/reports/appointments?from=yesterday&to=2026-02-30&granularity=hourly&doctor_id=7&department_id=${CARDIOLOGY}`)
    await ready()

    await waitFor(() => expect(address()).toBe(`/reports/appointments?department_id=${CARDIOLOGY}`))
    expect(navigation()).toBe('REPLACE')
    for (const sent of sentTo('/reports/appointments')) {
      expect(queryOf(sent)).toEqual({ department_id: CARDIOLOGY })
    }
  })

  it('never sends a filter the report does not take', async () => {
    renderAt(`/reports/patients?to=2026-10-06&doctor_id=${PRIYA}`)
    await ready()

    expect(sentTo('/reports/patients')).toHaveLength(1)
    expect(queryOf(lastTo('/reports/patients'))).toEqual({ to: '2026-10-06' })
  })

  it('shows the server\'s own words for a filter it refuses, and offers a way out', async () => {
    const user = userEvent.setup()
    const unknown = '00000000-0000-4000-8000-000000000099'
    renderAt(`/reports/appointments?doctor_id=${unknown}`)

    const alert = await screen.findByText("These filters can't be used")
    const box = alert.closest('[role="alert"]') as HTMLElement
    expect(box).toHaveTextContent('Doctor not found.')
    expect(queryOf(lastTo('/reports/appointments'))).toEqual({ doctor_id: unknown })

    await user.click(within(box).getByRole('button', { name: 'Clear filters' }))

    await ready()
    expect(address()).toBe('/reports/appointments')
    expect(queryOf(lastTo('/reports/appointments'))).toEqual({})
    expect(tile('Total')).toHaveTextContent('14')
  })

  it('shows the field message when the server only says "Validation failed."', async () => {
    intercept = (config) =>
      config.url === '/reports/revenue'
        ? fail(422, 'Validation failed.', {
            error_code: 'VALIDATION_ERROR',
            errors: [{ field: 'query.from', message: 'Input should be a valid date' }],
          })
        : undefined
    renderAt('/reports/revenue?from=2026-10-01')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Input should be a valid date')
    expect(alert).not.toHaveTextContent('Validation failed.')
  })

  describe('presets, counted from the hospital\'s day', () => {
    it.each([
      ['Last 7 days', '2026-09-30'],
      ['Last 30 days', '2026-09-07'],
      ['This month', '2026-10-01'],
      ['Last 12 months', '2025-10-07'],
    ])('%s on 6 Oct 2026 starts on %s', async (name, from) => {
      const user = userEvent.setup()
      // The browser's clock is not on 6 Oct 2026; only the payload says so.
      renderAt('/reports/revenue')
      await ready()

      await user.click(preset(name))

      await waitFor(() => expect(address()).toBe(`/reports/revenue?from=${from}&to=2026-10-06`))
      expect(navigation()).toBe('REPLACE')
      await waitFor(() => expect(sentTo('/reports/revenue')).toHaveLength(2))
      expect(queryOf(lastTo('/reports/revenue'))).toEqual({ from, to: '2026-10-06' })
      await ready()
      expect(preset(name)).toHaveAttribute('aria-pressed', 'true')
    })

    it('Last 12 months on 28 Feb 2025 starts on 1 Mar 2024, a range the server accepts', async () => {
      const user = userEvent.setup()
      today = '2025-02-28'
      renderAt('/reports/patients')
      await ready()

      await user.click(preset('Last 12 months'))

      await waitFor(() => expect(address()).toBe('/reports/patients?from=2024-03-01&to=2025-02-28'))
      // Not held back by the page's own twelve-month check: the request is sent.
      await waitFor(() => expect(sentTo('/reports/patients')).toHaveLength(2))
      expect(queryOf(lastTo('/reports/patients'))).toEqual({ from: '2024-03-01', to: '2025-02-28' })
      expect(screen.queryByText('Date range must not exceed 12 months.')).not.toBeInTheDocument()
    })

    it('stays usable after a refused period, so there is a way back', async () => {
      const user = userEvent.setup()
      renderAt('/reports/patients')
      await ready()

      fireEvent.change(fromInput(), { target: { value: '2027-01-01' } })
      fireEvent.change(toInput(), { target: { value: '2026-12-01' } })
      expect(await screen.findByText('`to` must not be before `from`.')).toBeInTheDocument()

      expect(preset('Last 7 days')).toBeEnabled()
      await user.click(preset('Last 7 days'))

      await ready()
      expect(address()).toBe('/reports/patients?from=2026-09-30&to=2026-10-06')
    })
  })
})

describe('appointments chart series', () => {
  it('gives each of the six statuses a colour of its own and a name', () => {
    expect(APPOINTMENT_SERIES.map((series) => series.key)).toEqual([
      'completed',
      'booked',
      'checked_in',
      'in_progress',
      'cancelled',
      'no_show',
    ])
    expect(new Set(APPOINTMENT_SERIES.map((series) => series.color)).size).toBe(APPOINTMENT_SERIES.length)
    expect(new Set(APPOINTMENT_SERIES.map((series) => series.label)).size).toBe(APPOINTMENT_SERIES.length)
    // Colours of the theme, so they follow light and dark mode.
    for (const series of APPOINTMENT_SERIES) expect(series.color).toMatch(/^var\(--color-[a-z0-9-]+\)$/)
  })
})

// ── Appointments ────────────────────────────────────────────────────────────

describe('appointments report', () => {
  it('shows the server\'s counts by status, by doctor and by department', async () => {
    renderAt('/reports/appointments')
    await ready()

    const sent = lastTo('/reports/appointments')
    expect(sent.method).toBe('get')
    expect(queryOf(sent)).toEqual({})

    expect(tile('Total')).toHaveTextContent('Total14')
    expect(tile('Completed')).toHaveTextContent('Completed3')
    expect(tile('Cancelled')).toHaveTextContent('Cancelled1')
    expect(tile('No-show')).toHaveTextContent('No-show125.0% of appointments that were due')

    expect(headersOf(table('Period'))).toEqual([
      'Period',
      'Total',
      'Booked',
      'Checked in',
      'In progress',
      'Completed',
      'Cancelled',
      'No-show',
    ])
    expect(rowsOf(table('Period'))).toEqual([
      ['5 Oct', '5', '0', '0', '0', '3', '1', '1'],
      ['6 Oct', '5', '1', '3', '1', '0', '0', '0'],
      ['7 Oct', '2', '2', '0', '0', '0', '0', '0'],
      ['8 Oct', '2', '2', '0', '0', '0', '0', '0'],
      ['Total', '14', '5', '3', '1', '3', '1', '1'],
    ])

    expect(screen.getByRole('heading', { name: 'By doctor' })).toBeInTheDocument()
    expect(rowsOf(table('Doctor'))).toEqual([
      ['Priya Sharma', 'Cardiology', '6', '2', '0', '0'],
      ['Arjun Nair', 'Orthopaedics', '4', '0', '0', '1'],
      ['Meera Krishnan', 'Paediatrics', '3', '1', '0', '0'],
      // A doctor with no department.
      ['Vikram Desai', 'Unassigned', '1', '0', '1', '0'],
    ])
    expect(screen.getByRole('heading', { name: 'By department' })).toBeInTheDocument()
    expect(rowsOf(table('Department'))).toEqual([
      ['Cardiology', '6', '2', '0', '0'],
      ['Orthopaedics', '4', '0', '0', '1'],
      ['Paediatrics', '3', '1', '0', '0'],
      ['Unassigned', '1', '0', '1', '0'],
    ])

    const chart = screen.getByRole('region', { name: 'Appointments by status' })
    expect(chart).toHaveTextContent('14 in total, 3 completed, 1 cancelled and 1 no-show')
  })

  it('leaves the no-show rate out when the server sends none', async () => {
    appointments = {
      ...appointmentsReportFixture,
      summary: { ...appointmentsReportFixture.summary, no_show_rate_percent: null },
    }
    renderAt('/reports/appointments')
    await ready()

    expect(tile('No-show')).toHaveTextContent(/^No-show1$/)
    expect(document.body.textContent).not.toMatch(/% of appointments/)
  })

  it('shows the server\'s totals and rate even where they differ from the rows', async () => {
    // Deliberately not the sum of the periods, nor a rate the counts would give.
    appointments = {
      ...appointmentsReportFixture,
      summary: {
        total: 99,
        booked: 40,
        checked_in: 20,
        in_progress: 10,
        completed: 17,
        cancelled: 7,
        no_show: 5,
        no_show_rate_percent: '61.5',
      },
    }
    renderAt('/reports/appointments')
    await ready()

    expect(tile('Total')).toHaveTextContent('Total99')
    expect(tile('No-show')).toHaveTextContent('No-show561.5% of appointments that were due')
    const rows = rowsOf(table('Period'))
    expect(rows[rows.length - 1]).toEqual(['Total', '99', '40', '20', '10', '17', '7', '5'])
  })

  it('shows skeletons while loading, then an empty state when nothing was scheduled', async () => {
    const pending = held()
    intercept = (config) => (config.url === '/reports/appointments' ? pending.promise : undefined)
    renderAt('/reports/appointments')

    expect(await screen.findByRole('status', { name: 'Loading Total' })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Loading Appointments by status' })).toBeInTheDocument()
    expect(skeletons()).toBeGreaterThan(4)

    const zero = { total: 0, booked: 0, checked_in: 0, in_progress: 0, completed: 0, cancelled: 0, no_show: 0 }
    pending.release(
      ok({
        ...appointmentsReportFixture,
        summary: { ...zero, no_show_rate_percent: null },
        buckets: appointmentsReportFixture.buckets.map((bucket) => ({ ...bucket, ...zero })),
        by_doctor: [],
        by_department: [],
      }),
    )
    await ready()

    expect(screen.getByText('No appointments were scheduled in this period.')).toBeInTheDocument()
    expect(tile('Total')).toHaveTextContent('Total0')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'By doctor' })).not.toBeInTheDocument()
  })

  it('reports a failure plainly and retries', async () => {
    const user = userEvent.setup()
    intercept = (config) => (config.url === '/reports/appointments' ? fail(503, 'upstream timed out') : undefined)
    renderAt('/reports/appointments')

    const alert = (await screen.findByText("Couldn't load the report")).closest('[role="alert"]') as HTMLElement
    expect(alert).not.toHaveTextContent('upstream')
    // Nothing is filtered, so there is nothing to clear.
    expect(within(alert).queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument()

    intercept = () => undefined
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))

    await ready()
    expect(sentTo('/reports/appointments')).toHaveLength(2)
    expect(tile('Total')).toHaveTextContent('14')
  })

  it('lists doctors, deactivated ones included, and departments for the filters', async () => {
    renderAt('/reports/appointments')
    await ready()

    await waitFor(() => expect(sentTo('/doctors')).toHaveLength(1))
    const doctors = lastTo('/doctors')
    expect(doctors.method).toBe('get')
    expect(queryOf(doctors)).toEqual({ include_inactive: true, page_size: 100 })
    const departments = lastTo('/departments')
    expect(departments.method).toBe('get')
    expect(queryOf(departments)).toEqual({ page: 1, page_size: 100 })

    expect(screen.getByRole('combobox', { name: 'Doctor' })).toHaveTextContent('All doctors')
    expect(screen.getByRole('combobox', { name: 'Department' })).toHaveTextContent('All departments')
  })

  it('narrows to a doctor chosen from the list', async () => {
    const user = userEvent.setup()
    renderAt('/reports/appointments')
    await ready()

    await choose(user, screen.getByRole('combobox', { name: 'Doctor' }), 'Arjun Nair')

    await waitFor(() => expect(address()).toBe(`/reports/appointments?doctor_id=${ARJUN}`))
    expect(navigation()).toBe('REPLACE')
    await waitFor(() => expect(sentTo('/reports/appointments')).toHaveLength(2))
    expect(queryOf(lastTo('/reports/appointments'))).toEqual({ doctor_id: ARJUN })
    await ready()
    expect(screen.getByRole('combobox', { name: 'Doctor' })).toHaveTextContent('Arjun Nair')
    expect(screen.getByRole('region', { name: 'Appointments by status' })).toHaveTextContent('Arjun Nair')

    // A deactivated doctor is offered, and marked.
    await user.click(screen.getByRole('combobox', { name: 'Doctor' }))
    expect(await screen.findByRole('option', { name: 'Meera Krishnan (inactive)' })).toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'All doctors' }))
    await waitFor(() => expect(address()).toBe('/reports/appointments'))
  })

  it('narrows to a department, and offers that department\'s doctors', async () => {
    const user = userEvent.setup()
    renderAt('/reports/appointments?granularity=week')
    await ready()

    await choose(user, screen.getByRole('combobox', { name: 'Department' }), 'Cardiology')

    await waitFor(() =>
      expect(address()).toBe(`/reports/appointments?granularity=week&department_id=${CARDIOLOGY}`),
    )
    await waitFor(() => expect(sentTo('/reports/appointments')).toHaveLength(2))
    expect(queryOf(lastTo('/reports/appointments'))).toEqual({ granularity: 'week', department_id: CARDIOLOGY })
    await waitFor(() =>
      expect(queryOf(lastTo('/doctors'))).toEqual({
        department: CARDIOLOGY,
        include_inactive: true,
        page_size: 100,
      }),
    )
    await ready()
    expect(screen.getByRole('combobox', { name: 'Department' })).toHaveTextContent('Cardiology')
  })

  it('names a doctor from the address who is missing from the list, using the report\'s own answer', async () => {
    renderAt(`/reports/appointments?doctor_id=${VIKRAM}`)
    await ready()

    expect(queryOf(lastTo('/reports/appointments'))).toEqual({ doctor_id: VIKRAM })
    await waitFor(() => expect(sentTo('/doctors').length).toBeGreaterThan(0))
    // Not among the doctors the list returned; the name is `filters.doctor.name`.
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Doctor' })).toHaveTextContent('Vikram Desai'))
  })

  it('narrows to a doctor when their name is clicked in the By doctor table', async () => {
    const user = userEvent.setup()
    renderAt('/reports/appointments?from=2026-10-05&to=2026-10-08')
    await ready()

    await user.click(within(table('Doctor')).getByRole('link', { name: 'Show only Vikram Desai' }))

    await waitFor(() =>
      expect(address()).toBe(`/reports/appointments?from=2026-10-05&to=2026-10-08&doctor_id=${VIKRAM}`),
    )
    expect(navigation()).toBe('REPLACE')
    await waitFor(() => expect(sentTo('/reports/appointments')).toHaveLength(2))
    expect(queryOf(lastTo('/reports/appointments'))).toEqual({
      from: '2026-10-05',
      to: '2026-10-08',
      doctor_id: VIKRAM,
    })
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Doctor' })).toHaveTextContent('Vikram Desai'))
  })

  it('asks for neither list from a user who may not read them, yet shows a filter that is applied', async () => {
    const user = userEvent.setup()
    renderAt(`/reports/appointments?doctor_id=${PRIYA}`, ['report.admin.read'])
    await ready()

    expect(sentTo('/doctors')).toHaveLength(0)
    expect(sentTo('/departments')).toHaveLength(0)
    expect(screen.queryByRole('combobox', { name: 'Doctor' })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Department' })).not.toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Doctor filter: Priya Sharma' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Clear the doctor filter' }))
    await waitFor(() => expect(address()).toBe('/reports/appointments'))
  })
})

// ── Revenue ─────────────────────────────────────────────────────────────────

describe('revenue report', () => {
  it('shows the server\'s amounts in the hospital\'s currency', async () => {
    renderAt('/reports/revenue', BILLING)

    expect(await screen.findByText('All figures in Asia/Kolkata · INR')).toBeInTheDocument()
    const sent = lastTo('/reports/revenue')
    expect(sent.method).toBe('get')
    expect(queryOf(sent)).toEqual({})

    expect(tile('Billed')).toHaveTextContent(`Billed${inr(4350)}5 invoices`)
    expect(tile('Collected')).toHaveTextContent(`Collected${inr(2800)}5 payments`)
    expect(tile('Refunded')).toHaveTextContent(`Refunded${inr(950)}2 refunds`)
    // "1850.00" on the wire.
    expect(tile('Net collected')).toHaveTextContent(`Net collected${inr(1850)}`)

    expect(headersOf(table('Period'))).toEqual([
      'Period',
      'Invoices',
      'Billed',
      'Collected',
      'Refunded',
      'Net collected',
    ])
    expect(rowsOf(table('Period'))).toEqual([
      ['5 Oct', '3', inr(2950), inr(1400), inr(0), inr(1400)],
      ['6 Oct', '2', inr(1400), inr(1400), inr(950), inr(450)],
      ['Total', '5', inr(4350), inr(2800), inr(950), inr(1850)],
    ])

    expect(screen.getByRole('heading', { name: 'By payment method' })).toBeInTheDocument()
    expect(rowsOf(table('Method'))).toEqual([
      ['Cash', '1', inr(500), '0', inr(0), inr(500)],
      ['Card', '2', inr(1100), '1', inr(350), inr(750)],
      ['UPI', '2', inr(1200), '1', inr(600), inr(600)],
      ['Bank transfer', '0', inr(0), '0', inr(0), inr(0)],
      ['Insurance', '0', inr(0), '0', inr(0), inr(0)],
    ])

    expect(
      screen.getByText(
        'Billed counts invoices by issue date. Collected and refunded count payments and refunds by the date they were recorded, so they can relate to invoices from an earlier period.',
      ),
    ).toBeInTheDocument()
    // Billing staff ask for nothing else: no doctors, no departments.
    expect(fake.sent).toHaveLength(1)
  })

  it('shows a negative net with its minus sign', async () => {
    revenue = {
      ...revenueReportFixture,
      summary: { ...revenueReportFixture.summary, collected_amount: '600.00', net_collected_amount: '-350.00' },
    }
    renderAt('/reports/revenue')
    await ready()

    expect(inr(-350)).toMatch(/^[-−]/)
    expect(tile('Net collected')).toHaveTextContent(`Net collected${inr(-350)}`)
    const rows = rowsOf(table('Period'))
    expect(rows[rows.length - 1][5]).toBe(inr(-350))
  })

  it('adds nothing up: a summary that differs from its rows is shown as the server sent it', async () => {
    revenue = {
      ...revenueReportFixture,
      summary: {
        invoice_count: 42,
        invoiced_amount: '9999.00',
        payment_count: 7,
        collected_amount: '1234.50',
        refund_count: 3,
        refunded_amount: '10.25',
        // Not collected less refunded either.
        net_collected_amount: '77.77',
      },
    }
    renderAt('/reports/revenue')
    await ready()

    expect(tile('Billed')).toHaveTextContent(`Billed${inr(9999)}42 invoices`)
    expect(tile('Net collected')).toHaveTextContent(`Net collected${inr(77.77)}`)
    const rows = rowsOf(table('Period'))
    expect(rows[rows.length - 1]).toEqual(['Total', '42', inr(9999), inr(1234.5), inr(10.25), inr(77.77)])
    // The periods are untouched.
    expect(rows[0]).toEqual(['5 Oct', '3', inr(2950), inr(1400), inr(0), inr(1400)])
  })

  it('shows skeletons while loading and an honest empty state for a period with no money', async () => {
    const pending = held()
    intercept = (config) => (config.url === '/reports/revenue' ? pending.promise : undefined)
    renderAt('/reports/revenue')

    expect(await screen.findByRole('status', { name: 'Loading Billed' })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Loading Net collected' })).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/₹|INR/)

    const none = {
      invoice_count: 0,
      invoiced_amount: '0.00',
      payment_count: 0,
      collected_amount: '0.00',
      refund_count: 0,
      refunded_amount: '0.00',
      net_collected_amount: '0.00',
    }
    pending.release(
      ok({
        ...revenueReportFixture,
        summary: none,
        buckets: revenueReportFixture.buckets.map((bucket) => ({ ...bucket, ...none })),
        by_method: revenueReportFixture.by_method.map((row) => ({
          ...row,
          payment_count: 0,
          collected_amount: '0.00',
          refund_count: 0,
          refunded_amount: '0.00',
          net_collected_amount: '0.00',
        })),
      }),
    )
    await ready()

    expect(screen.getByText('No invoices, payments or refunds in this period.')).toBeInTheDocument()
    expect(tile('Billed')).toHaveTextContent(`Billed${inr(0)}0 invoices`)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('reports a failure plainly and retries with the same filters', async () => {
    const user = userEvent.setup()
    intercept = (config) => (config.url === '/reports/revenue' ? fail(500, 'Traceback (most recent call last)') : undefined)
    renderAt('/reports/revenue?from=2026-10-05&to=2026-10-06')

    const alert = (await screen.findByText("Couldn't load the report")).closest('[role="alert"]') as HTMLElement
    expect(alert).not.toHaveTextContent('Traceback')

    intercept = () => undefined
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))

    await ready()
    expect(sentTo('/reports/revenue')).toHaveLength(2)
    expect(queryOf(lastTo('/reports/revenue'))).toEqual({ from: '2026-10-05', to: '2026-10-06' })
    expect(address()).toBe('/reports/revenue?from=2026-10-05&to=2026-10-06')
  })

  it('exports with exactly the filters in the address', async () => {
    const user = userEvent.setup()
    intercept = (config) =>
      config.url === '/reports/revenue/export'
        ? fail(422, 'PDF export is not available yet. Use format=csv.', { error_code: 'VALIDATION_ERROR' })
        : undefined
    renderAt('/reports/revenue?from=2026-10-05&granularity=week')
    await ready()

    await user.click(screen.getByRole('button', { name: 'Export CSV' }))

    await waitFor(() => expect(sentTo('/reports/revenue/export')).toHaveLength(1))
    const sent = lastTo('/reports/revenue/export')
    expect(sent.method).toBe('get')
    expect(sent.responseType).toBe('blob')
    // `to` is the server's default: it is in the inputs, not in the address, so it is not sent.
    expect(queryOf(sent)).toEqual({ format: 'csv', from: '2026-10-05', granularity: 'week' })
  })
})

// ── Outstanding ─────────────────────────────────────────────────────────────

describe('outstanding report', () => {
  it('asks with no query and shows the unpaid invoices, oldest first', async () => {
    renderAt('/reports/outstanding', BILLING)

    expect(await screen.findByText(/^As of .*2026 · All figures in Asia\/Kolkata · INR$/)).toBeInTheDocument()
    expect(fake.sent).toHaveLength(1)
    const sent = lastTo('/reports/outstanding')
    expect(sent.method).toBe('get')
    expect(sent.params).toBeUndefined()

    expect(tile('Outstanding')).toHaveTextContent(`Outstanding${inr(1550)}`)
    expect(tile('Unpaid invoices')).toHaveTextContent('Unpaid invoices2')
    expect(tile('Not yet paid')).toHaveTextContent('Not yet paid1')
    expect(tile('Part-paid')).toHaveTextContent('Part-paid1')

    // The chart's four bars, as a table.
    expect(rowsOf(table('Age'))).toEqual([
      ['0–30 days', '2', inr(1550)],
      ['31–60 days', '0', inr(0)],
      ['61–90 days', '0', inr(0)],
      ['Over 90 days', '0', inr(0)],
    ])

    const invoices = table('Invoice')
    expect(headersOf(invoices)).toEqual([
      'Invoice',
      'Issued',
      'Age (days)',
      'Patient',
      'MRN',
      'Status',
      'Total',
      'Paid',
      'Balance',
    ])
    const rows = rowsOf(invoices)
    expect(rows).toHaveLength(2)
    expect(rows[0][0]).toBe('INV-2026-000003')
    expect(rows[0].slice(2)).toEqual([
      '1',
      'Thomas George',
      'MRN-000002',
      'Partially paid',
      inr(750),
      inr(300),
      inr(450),
    ])
    expect(rows[1][0]).toBe('INV-2026-000004')
    expect(rows[1].slice(2)).toEqual(['1', 'Ishaan Kulkarni', 'MRN-000003', 'Issued', inr(1100), inr(0), inr(1100)])
    // The issue date is the hospital's (`issued_date`), not the instant read in the browser's zone.
    expect(rows[0][1]).toMatch(/5/)
    expect(rows[0][1]).toMatch(/2026/)

    expect(within(invoices).getByRole('link', { name: 'Invoice INV-2026-000003' })).toHaveAttribute(
      'href',
      '/billing/0f6a7b11-3c2d-4e5f-8a90-1b2c3d4e5f03',
    )
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    // A point-in-time report has no period to choose.
    expect(screen.queryByLabelText('From')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Last 7 days' })).not.toBeInTheDocument()
  })

  it('says how many of the invoices are listed, from the payload\'s own numbers', async () => {
    const [first, second] = outstandingReportFixture.invoices
    outstanding = {
      ...outstandingReportFixture,
      summary: { ...outstandingReportFixture.summary, invoice_count: 7 },
      invoices: [first, second, { ...second, invoice_id: '2b8c9d33-5e4f-4a71-8cb2-3d4e5f6a7b05', invoice_number: 'INV-2026-000005' }],
      invoices_total: 7,
      invoices_truncated: true,
    }
    renderAt('/reports/outstanding')
    await ready()

    expect(screen.getByRole('note')).toHaveTextContent('Showing the 3 oldest of 7. Export for the full list.')
    expect(rowsOf(table('Invoice'))).toHaveLength(3)
  })

  it('does not link an invoice for a user who cannot open it', async () => {
    renderAt('/reports/outstanding', ['report.billing.read'])
    await ready()

    const invoices = table('Invoice')
    expect(within(invoices).getByText('INV-2026-000003')).toBeInTheDocument()
    expect(within(invoices).queryByRole('link')).not.toBeInTheDocument()
  })

  it('shows skeletons while loading and says when nothing is unpaid', async () => {
    const pending = held()
    intercept = (config) => (config.url === '/reports/outstanding' ? pending.promise : undefined)
    renderAt('/reports/outstanding')

    expect(await screen.findByRole('status', { name: 'Loading Outstanding' })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Loading Outstanding by age' })).toBeInTheDocument()
    expect(skeletons()).toBeGreaterThan(4)

    pending.release(
      ok({
        ...outstandingReportFixture,
        summary: { invoice_count: 0, outstanding_amount: '0.00', issued_count: 0, partially_paid_count: 0 },
        ageing: outstandingReportFixture.ageing.map((bucket) => ({
          ...bucket,
          invoice_count: 0,
          outstanding_amount: '0.00',
        })),
        invoices: [],
        invoices_total: 0,
        invoices_truncated: false,
      }),
    )
    await ready()

    expect(screen.getByText('No unpaid invoices.')).toBeInTheDocument()
    expect(tile('Outstanding')).toHaveTextContent(`Outstanding${inr(0)}`)
    expect(tile('Unpaid invoices')).toHaveTextContent('Unpaid invoices0')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('reports a failure plainly and retries', async () => {
    const user = userEvent.setup()
    intercept = (config) => (config.url === '/reports/outstanding' ? fail(500, 'KeyError: hospital') : undefined)
    renderAt('/reports/outstanding')

    const alert = (await screen.findByText("Couldn't load the report")).closest('[role="alert"]') as HTMLElement
    expect(alert).not.toHaveTextContent('KeyError')

    intercept = () => undefined
    await user.click(within(alert).getByRole('button', { name: 'Retry' }))

    await ready()
    expect(sentTo('/reports/outstanding')).toHaveLength(2)
    expect(lastTo('/reports/outstanding').params).toBeUndefined()
    expect(tile('Outstanding')).toHaveTextContent(inr(1550))
  })

  it('shows the server\'s refusal when the account has no hospital', async () => {
    intercept = (config) =>
      config.url === '/reports/outstanding'
        ? fail(400, 'This account is not scoped to a hospital, so reports cannot be read.', {
            error_code: 'BUSINESS_RULE_VIOLATION',
          })
        : undefined
    renderAt('/reports/outstanding')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This account is not scoped to a hospital, so reports cannot be read.',
    )
  })

  it('does not ask for the report on behalf of a user who may not read it', async () => {
    renderAt('/reports/outstanding', ['report.doctor.read', 'report.export'])

    expect(await screen.findByRole('alert')).toHaveTextContent("You can't view this report.")
    expect(fake.sent).toHaveLength(0)
  })
})
