import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { AppointmentSummary } from '@/api/appointments'
import { useRecordPayment } from '@/api/billing'
import { useCreatePatient } from '@/api/patients'
import type {
  AdminDashboard,
  BillingDashboard,
  DoctorDashboard,
  ReceptionDashboard,
} from '@/api/reports'
import { formatMoney, formatTimeIn, todayISODate } from '@/lib/format'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import {
  adminDashboardFixture,
  billingDashboardFixture,
  doctorDashboardFixture,
  receptionDashboardFixture,
} from '@/test/reportFixtures'
import DashboardPage from '../DashboardPage'

/**
 * The role sections of the home page, through the real page, permission checks,
 * hooks and Axios instance. Only the network is replaced, by an in-memory
 * server that answers with the API's own envelopes and example payloads
 * (`GET /dashboards/{admin,doctor,reception,billing}`).
 *
 * The permission sets are the dashboard-relevant part of each seeded role
 * (backend/app/seeds/seed.py).
 */
const REPORT_CODES = [
  'report.admin.read',
  'report.doctor.read',
  'report.reception.read',
  'report.billing.read',
  'report.export',
]
// A Hospital Admin holds every report code so that they can assign them.
const ADMIN = [
  'patient.read',
  'doctor.read',
  'appointment.read',
  'invoice.read',
  'invoice.payment.record',
  ...REPORT_CODES,
]
const BILLING_STAFF = [
  'patient.read',
  'doctor.read',
  'invoice.read',
  'invoice.payment.record',
  'report.billing.read',
  'report.export',
]
const RECEPTIONIST = [
  'patient.read',
  'doctor.read',
  'appointment.read',
  'appointment.check_in',
  'invoice.read',
  'invoice.payment.record.cash',
  'report.reception.read',
]
const DOCTOR = ['patient.read', 'doctor.read', 'appointment.read', 'report.doctor.read']
const NURSE = ['patient.read', 'doctor.read', 'appointment.read', 'appointment.check_in']

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

type DashboardName = 'admin' | 'doctor' | 'reception' | 'billing'

const MESSAGES: Record<DashboardName, string> = {
  admin: 'Admin dashboard loaded.',
  doctor: 'Doctor dashboard loaded.',
  reception: 'Reception dashboard loaded.',
  billing: 'Billing dashboard loaded.',
}

/** The success envelope a dashboard route sends. */
const loaded = (name: DashboardName, data: unknown): Outcome => ({
  status: 200,
  data: { success: true, message: MESSAGES[name], data, metadata: null },
})

const serverError = () => fail(500, 'Traceback (most recent call last): asyncpg.PostgresError')

const QUEUED: AppointmentSummary = {
  id: 'a1',
  patient_id: 'pat-2',
  patient_name: 'Meera Nair',
  doctor_id: 'doc-1',
  doctor_name: 'Priya Sharma',
  scheduled_start: '2026-10-06T09:30:00Z',
  scheduled_end: '2026-10-06T10:00:00Z',
  status: 'booked',
  type: 'new',
}

let api: FakeApi
/** What each dashboard route answers; `n` is which request this is, from 1. */
let dashboards: Record<DashboardName, (n: number) => Outcome | Promise<Outcome>>
let queue: AppointmentSummary[]

function server(config: InternalAxiosRequestConfig): Outcome | Promise<Outcome> {
  const url = config.url ?? ''
  if (config.method === 'get') {
    const dashboard = /^\/dashboards\/(admin|doctor|reception|billing)$/.exec(url)
    if (dashboard) {
      const name = dashboard[1] as DashboardName
      // The dashboards take no query parameters: any key is a 422.
      const keys = Object.keys((config.params ?? {}) as Record<string, unknown>)
      if (keys.length > 0) {
        return fail(422, `Unknown query parameter: \`${keys[0]}\`.`, { error_code: 'VALIDATION_ERROR' })
      }
      return dashboards[name](api.requests('get', url).length)
    }
    if (url === '/appointments') return paged(structuredClone(queue))
    if (url === '/invoices') return paged([])
    if (url === '/patients') return paged([], 137)
    if (url === '/doctors') return paged([], 5)
    return fail(404, 'Not found.')
  }
  const checkIn = /^\/appointments\/([^/]+)\/check-in$/.exec(url)
  if (checkIn) {
    const found = queue.find((a) => a.id === checkIn[1])
    if (!found) return fail(404, 'Appointment not found.')
    found.status = 'checked_in'
    return { status: 200, data: { success: true, message: 'ok', data: { ...found } } }
  }
  if (url === '/invoices/inv-1/payments') {
    return { status: 201, data: { success: true, message: 'ok', data: { payment: {}, invoice: {} } } }
  }
  if (url === '/patients') {
    return { status: 201, data: { success: true, message: 'ok', data: { id: 'pat-new' } } }
  }
  return fail(403, 'Permission denied.', { error_code: 'PERMISSION_DENIED' })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  queue = [structuredClone(QUEUED)]
  dashboards = {
    admin: () => loaded('admin', adminDashboardFixture),
    doctor: () => loaded('doctor', doctorDashboardFixture),
    reception: () => loaded('reception', receptionDashboardFixture),
    billing: () => loaded('billing', billingDashboardFixture),
  }
  api = installFakeApi(server)
})

afterEach(() => {
  vi.useRealTimers()
  api.restore()
  signOut()
})

/** Two writes made elsewhere in the app, through the real mutation hooks. */
function OtherScreens() {
  const pay = useRecordPayment('inv-1')
  const register = useCreatePatient()
  return (
    <>
      <button type="button" onClick={() => pay.mutate({ amount: '450.00', method: 'cash' })}>
        Take a payment elsewhere
      </button>
      <button
        type="button"
        onClick={() =>
          register.mutate({
            first_name: 'Kavya',
            last_name: 'Iyer',
            date_of_birth: '1990-04-12',
            gender: 'female',
          })
        }
      >
        Register a patient elsewhere
      </button>
    </>
  )
}

function renderDashboard(permissions: string[]) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <QueryClientProvider client={client}>
        <DashboardPage />
        <OtherScreens />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const region = (name: string) => screen.findByRole('region', { name })
/** The tile carrying `label`: the label sits in the tile's header row. */
const tile = async (section: HTMLElement, label: string) =>
  (await within(section).findByText(label)).parentElement?.parentElement as HTMLElement
/** The URL of every GET sent, sorted. */
const reads = () =>
  api
    .requests('get')
    .map((c) => c.url)
    .sort()
const dashboardReads = () => reads().filter((url) => url?.startsWith('/dashboards/'))
// Testing Library collapses whitespace in the page's text, and some locales put
// a no-break space between the amount and the currency.
const inr = (amount: string) => formatMoney(amount, 'INR').replace(/\s+/g, ' ')
/**
 * No money in a section that carries none: no currency anywhere in it, and no
 * decimal amount in its tiles. The amount check stays off the lists, whose
 * clock times some locales write with a dot ("13.45").
 */
function expectNoMoney(section: HTMLElement, tiles: HTMLElement[]) {
  const symbol = inr('0.00').replace(/[\d.,\s]/g, '')
  expect(symbol).not.toBe('')
  expect(section.textContent).not.toContain(symbol)
  expect(section.textContent).not.toMatch(/₹|INR/)
  for (const each of tiles) expect(each.textContent).not.toMatch(/\d[.,]\d{2}(?!\d)/)
}
const queueDates = () =>
  api.requests('get', '/appointments').map((c) => (c.params as { date?: string }).date)

describe('dashboard: which sections each role gets', () => {
  it('asks a hospital admin for the admin and billing dashboards only', async () => {
    renderDashboard(ADMIN)

    await region('Hospital overview')
    await region('Billing')
    await screen.findByRole('row', { name: /Meera Nair/ })
    await waitFor(() => expect(api.requests('get', '/invoices')).toHaveLength(2))
    // Holding the reception and doctor codes does not open those sections.
    expect(reads()).toEqual([
      '/appointments',
      '/dashboards/admin',
      '/dashboards/billing',
      '/invoices',
      '/invoices',
    ])
    for (const request of api.requests('get', '/dashboards/')) {
      expect(request.params).toBeUndefined()
    }
    expect(screen.queryByRole('region', { name: 'Front desk' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'My day' })).not.toBeInTheDocument()
    // Admin first, then Billing.
    expect(
      screen
        .getAllByRole('heading', { level: 2 })
        .map((h) => h.textContent)
        .slice(0, 2),
    ).toEqual(['Hospital overview', 'Billing'])
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
    expect(api.sent.every((c) => c.method === 'get')).toBe(true)
  })

  it('asks billing staff for the billing dashboard only', async () => {
    renderDashboard(BILLING_STAFF)

    await region('Billing')
    await waitFor(() => expect(api.requests('get', '/invoices')).toHaveLength(2))
    expect(reads()).toEqual(['/dashboards/billing', '/invoices', '/invoices'])
    expect(api.requests('get', '/dashboards/billing')[0].params).toBeUndefined()
    expect(screen.queryByRole('region', { name: 'Hospital overview' })).not.toBeInTheDocument()
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
  })

  it('asks a receptionist for the reception dashboard only', async () => {
    renderDashboard(RECEPTIONIST)

    await region('Front desk')
    await screen.findByRole('row', { name: /Meera Nair/ })
    await waitFor(() => expect(api.requests('get', '/invoices')).toHaveLength(2))
    expect(reads()).toEqual(['/appointments', '/dashboards/reception', '/invoices', '/invoices'])
    expect(api.requests('get', '/dashboards/reception')[0].params).toBeUndefined()
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
  })

  it('asks a doctor for the doctor dashboard only', async () => {
    renderDashboard(DOCTOR)

    await region('My day')
    await screen.findByRole('row', { name: /Meera Nair/ })
    expect(reads()).toEqual(['/appointments', '/dashboards/doctor'])
    expect(api.requests('get', '/dashboards/doctor')[0].params).toBeUndefined()
    expect(screen.queryByText('Total patients')).not.toBeInTheDocument()
  })

  it('asks for no dashboard for a nurse, who keeps the three list tiles', async () => {
    renderDashboard(NURSE)

    expect(await screen.findByText('137')).toBeInTheDocument()
    await screen.findByRole('row', { name: /Meera Nair/ })
    expect(reads()).toEqual(['/appointments', '/doctors', '/patients'])
    expect(dashboardReads()).toEqual([])
    expect(screen.getByText('Total patients')).toBeInTheDocument()
    expect(screen.getByText('Doctors')).toBeInTheDocument()
    expect(screen.getByText('5')).toBeInTheDocument()
    for (const name of ['Hospital overview', 'Billing', 'Front desk', 'My day']) {
      expect(screen.queryByRole('region', { name })).not.toBeInTheDocument()
    }
  })
})

describe('dashboard: admin section', () => {
  it('shows each tile with the figures the server sent', async () => {
    // `in_clinic` is the server's own sum; 9 is not checked in + in progress (3 + 1).
    const data: AdminDashboard = {
      ...adminDashboardFixture,
      appointments_today: { ...adminDashboardFixture.appointments_today, in_clinic: 9 },
    }
    dashboards.admin = () => loaded('admin', data)
    renderDashboard(ADMIN)

    const section = await region('Hospital overview')
    const appointments = await within(section).findByRole('link', { name: /Today's appointments/ })
    expect(appointments).toHaveAttribute('href', '/appointments')
    expect(within(appointments).getByText('5')).toBeInTheDocument()
    expect(within(appointments).getByText('0 completed · 9 in clinic · 1 to come')).toBeInTheDocument()

    const billed = within(section).getByRole('link', { name: /Billed this week/ })
    expect(billed).toHaveAttribute('href', '/reports/revenue')
    expect(within(billed).getByText(inr('4350.00'))).toBeInTheDocument()
    expect(
      within(billed).getByText(`Collected ${inr('2800.00')} · Refunded ${inr('950.00')}`),
    ).toBeInTheDocument()

    const registered = within(section).getByRole('link', { name: /New patients this month/ })
    expect(registered).toHaveAttribute('href', '/reports/patients')
    expect(within(registered).getByText('11')).toBeInTheDocument()
    expect(within(registered).getByText('11 active patients')).toBeInTheDocument()
    expect(within(section).queryByText('Nothing to report yet')).not.toBeInTheDocument()
  })

  it("shows today's appointments without a link to a user who may not open the list", async () => {
    renderDashboard(['report.admin.read'])

    const section = await region('Hospital overview')
    const appointments = await tile(section, "Today's appointments")
    expect(await within(appointments).findByText('5')).toBeInTheDocument()
    expect(within(appointments).getByText('0 completed · 4 in clinic · 1 to come')).toBeInTheDocument()
    // The two report links stay; nothing leads to a page this user cannot open.
    expect(
      within(section)
        .getAllByRole('link')
        .map((a) => a.getAttribute('href')),
    ).toEqual(['/reports/revenue', '/reports/patients'])
    expect(document.querySelector('a[href="/appointments"]')).toBeNull()
    expect(reads()).toEqual(['/dashboards/admin'])
  })

  it('says there is nothing to report yet only when the hospital has no active patients', async () => {
    const empty: AdminDashboard = {
      ...adminDashboardFixture,
      patient_registrations_this_month: {
        ...adminDashboardFixture.patient_registrations_this_month,
        registered: 0,
        active_total: 0,
      },
    }
    dashboards.admin = () => loaded('admin', empty)
    renderDashboard(ADMIN)

    const section = await region('Hospital overview')
    expect(await within(section).findByText('Nothing to report yet')).toBeInTheDocument()
    expect(
      within(section).getByText(
        'Figures appear here as soon as your team registers patients, books appointments and issues invoices. Start with Register Patient or Book Appointment above.',
      ),
    ).toBeInTheDocument()
    // Zero is a figure, not an error.
    expect(within(await tile(section, 'New patients this month')).getByText('0')).toBeInTheDocument()
    expect(within(section).queryByText(/Couldn't load/)).not.toBeInTheDocument()
    // The other sections give no tips.
    expect(within(await region('Billing')).queryByText('Nothing to report yet')).not.toBeInTheDocument()
  })

  it('shows skeleton tiles and no figure while loading, and holds the queue back', async () => {
    dashboards.admin = () => new Promise<Outcome>(() => {})
    renderDashboard(ADMIN)

    const section = await region('Hospital overview')
    for (const label of ["Today's appointments", 'Billed this week', 'New patients this month']) {
      expect(within(section).getByRole('status', { name: `Loading ${label}` })).toBeInTheDocument()
    }
    expect(section.textContent).not.toMatch(/\d/)
    // The billing section is independent of it.
    expect(await within(await region('Billing')).findByText('Unpaid invoices')).toBeInTheDocument()
    // The hospital's day is not known yet, so the queue has not been asked for.
    expect(api.requests('get', '/appointments')).toHaveLength(0)
  })
})

describe('dashboard: billing section', () => {
  it('shows each tile with the figures the server sent', async () => {
    renderDashboard(BILLING_STAFF)

    const section = await region('Billing')
    const unpaid = await within(section).findByRole('link', { name: /Unpaid invoices/ })
    expect(unpaid).toHaveAttribute('href', '/reports/outstanding')
    expect(within(unpaid).getByText('2')).toBeInTheDocument()
    expect(within(unpaid).getByText(`${inr('1550.00')} outstanding`)).toBeInTheDocument()

    const expected: [string, string, string][] = [
      ['Billed today', '1400.00', '1400.00'],
      ['Billed this week', '4350.00', '2800.00'],
      ['Billed this month', '4350.00', '2800.00'],
    ]
    for (const [label, invoiced, collected] of expected) {
      const billed = within(section).getByRole('link', { name: new RegExp(label) })
      expect(billed).toHaveAttribute('href', '/reports/revenue')
      expect(within(billed).getByText(inr(invoiced))).toBeInTheDocument()
      expect(within(billed).getByText(`Collected ${inr(collected)}`)).toBeInTheDocument()
    }

    const discounts = await tile(section, 'Discounts awaiting approval')
    expect(within(discounts).getByText('1')).toBeInTheDocument()
    expect(within(discounts).getByText(`${inr('300.00')} in discounts`)).toBeInTheDocument()
    expect(within(section).getAllByRole('link')).toHaveLength(4)
  })
})

describe('dashboard: reception section', () => {
  it('shows each tile with the figures the server sent, and no money', async () => {
    // `in_clinic` is the server's own sum; 8 is not checked in + in progress (3 + 1).
    const data: ReceptionDashboard = {
      ...receptionDashboardFixture,
      schedule_today: { ...receptionDashboardFixture.schedule_today, in_clinic: 8 },
    }
    dashboards.reception = () => loaded('reception', data)
    renderDashboard(RECEPTIONIST)

    const section = await region('Front desk')
    const appointments = await tile(section, "Today's appointments")
    expect(await within(appointments).findByText('5')).toBeInTheDocument()
    expect(within(appointments).getByText('0 completed · 8 in clinic · 1 to come')).toBeInTheDocument()

    const walkIns = await tile(section, 'Walk-ins waiting')
    expect(within(walkIns).getByText('2')).toBeInTheDocument()
    expect(within(walkIns).getByText('Longest wait 42 min · 0 not arrived')).toBeInTheDocument()

    const noShows = await tile(section, 'Possible no-shows')
    expect(within(noShows).getByText('1')).toBeInTheDocument()
    expect(within(noShows).getByText('0 marked no-show today')).toBeInTheDocument()

    expectNoMoney(section, [appointments, walkIns, noShows])
  })

  it('lists who is late, by how many minutes, on the hospital clock', async () => {
    renderDashboard(RECEPTIONIST)

    const section = await region('Front desk')
    const late = (await within(section).findByText('17 min late')).closest('li') as HTMLElement
    expect(within(late).getByRole('link', { name: 'Sunita Verma' })).toHaveAttribute(
      'href',
      '/patients/b5061728-93a4-4e5f-d0c2-b2a39e8f7006',
    )
    expect(within(late).getByText('Arjun Nair')).toBeInTheDocument()
    // 08:15 UTC is 1:45 PM in Asia/Kolkata, whatever the viewer's zone.
    expect(
      within(late).getByText(formatTimeIn('2026-10-06T08:15:00Z', 'Asia/Kolkata')),
    ).toBeInTheDocument()
    expect(within(section).getAllByRole('listitem')).toHaveLength(1)
    expect(within(section).queryByText(/Showing the first/)).not.toBeInTheDocument()
  })

  it('names a late patient without a link for a user who may not open patient records', async () => {
    renderDashboard(['report.reception.read'])

    const section = await region('Front desk')
    const late = (await within(section).findByText('17 min late')).closest('li') as HTMLElement
    expect(within(late).getByText('Sunita Verma')).toBeInTheDocument()
    expect(within(section).queryByRole('link')).not.toBeInTheDocument()
    expect(document.querySelector('a[href^="/patients"]')).toBeNull()
    expect(reads()).toEqual(['/dashboards/reception'])
  })

  it('leaves the wait out when nobody is waiting, and shows no list when nobody is late', async () => {
    const quiet: ReceptionDashboard = {
      ...receptionDashboardFixture,
      walk_in_queue: { waiting: 0, not_arrived: 3, in_consultation: 0, longest_wait_minutes: null },
      no_show_alerts: { marked_today: 2, at_risk: 0, at_risk_appointments: [] },
    }
    dashboards.reception = () => loaded('reception', quiet)
    renderDashboard(RECEPTIONIST)

    const section = await region('Front desk')
    expect(await within(section).findByText('3 not arrived')).toBeInTheDocument()
    expect(within(section).queryByText(/Longest wait/)).not.toBeInTheDocument()
    expect(within(await tile(section, 'Possible no-shows')).getByText('0')).toBeInTheDocument()
    expect(within(section).getByText('2 marked no-show today')).toBeInTheDocument()
    expect(within(section).queryByRole('list')).not.toBeInTheDocument()
  })

  it('says so when the list is shorter than the count', async () => {
    const many: ReceptionDashboard = {
      ...receptionDashboardFixture,
      no_show_alerts: { ...receptionDashboardFixture.no_show_alerts, at_risk: 23 },
    }
    dashboards.reception = () => loaded('reception', many)
    renderDashboard(RECEPTIONIST)

    const section = await region('Front desk')
    expect(await within(section).findByText('Showing the first 1 of 23.')).toBeInTheDocument()
    expect(within(await tile(section, 'Possible no-shows')).getByText('23')).toBeInTheDocument()
  })
})

describe('dashboard: doctor section', () => {
  it('shows each tile with the figures the server sent, and no money', async () => {
    // `to_see` is the server's own sum; 7 is not booked + checked in (0 + 1).
    const data: DoctorDashboard = {
      ...doctorDashboardFixture,
      schedule_today: { ...doctorDashboardFixture.schedule_today, to_see: 7 },
    }
    dashboards.doctor = () => loaded('doctor', data)
    renderDashboard(DOCTOR)

    const section = await region('My day')
    const today = await tile(section, 'My appointments today')
    expect(await within(today).findByText('2')).toBeInTheDocument()
    expect(within(today).getByText('0 done · 7 to see')).toBeInTheDocument()

    expect(within(await tile(section, 'My patients')).getByText('5')).toBeInTheDocument()

    const week = await tile(section, 'My week')
    expect(within(week).getByText('6')).toBeInTheDocument()
    expect(within(week).getByText('2 completed · 0 no-show · 0 cancelled')).toBeInTheDocument()

    expectNoMoney(section, [today, await tile(section, 'My patients'), week])
  })

  it("lists today's schedule in order, with time, patient, MRN and status", async () => {
    renderDashboard(DOCTOR)

    const section = await region('My day')
    await within(section).findByText("Today's schedule")
    const rows = within(section).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByRole('link', { name: 'Ananya Rao' })).toHaveAttribute(
      'href',
      '/patients/93e4f506-7182-4c3d-bea0-90817c6d5e04',
    )
    expect(within(rows[0]).getByText('MRN-000006')).toBeInTheDocument()
    expect(within(rows[0]).getByText('In progress')).toBeInTheDocument()
    expect(
      within(rows[0]).getByText(formatTimeIn('2026-10-06T08:00:00Z', 'Asia/Kolkata')),
    ).toBeInTheDocument()
    expect(within(rows[1]).getByText('Harish Pillai')).toBeInTheDocument()
    expect(within(rows[1]).getByText('MRN-000007')).toBeInTheDocument()
    expect(within(rows[1]).getByText('Checked in')).toBeInTheDocument()
  })

  it('says so when the doctor has no appointments today', async () => {
    const free: DoctorDashboard = {
      ...doctorDashboardFixture,
      schedule_today: {
        ...doctorDashboardFixture.schedule_today,
        total: 0,
        checked_in: 0,
        in_progress: 0,
        to_see: 0,
        appointments: [],
      },
    }
    dashboards.doctor = () => loaded('doctor', free)
    renderDashboard(DOCTOR)

    const section = await region('My day')
    expect(await within(section).findByText('You have no appointments today.')).toBeInTheDocument()
    expect(within(await tile(section, 'My appointments today')).getByText('0')).toBeInTheDocument()
    expect(within(section).queryByRole('list')).not.toBeInTheDocument()
  })

  it('shows a note, not an error, to an account with no doctor profile', async () => {
    dashboards.doctor = () =>
      fail(404, 'No active doctor profile is linked to this account.', {
        error_code: 'RESOURCE_NOT_FOUND',
      })
    renderDashboard(DOCTOR)

    expect(
      await screen.findByText(
        'No doctor profile is linked to your account, so there is no personal schedule to show.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(screen.queryByText('My appointments today')).not.toBeInTheDocument()
    // The queue still loads, and the answer is not asked for again.
    expect(await screen.findByRole('row', { name: /Meera Nair/ })).toBeInTheDocument()
    expect(api.requests('get', '/dashboards/doctor')).toHaveLength(1)
  })
})

describe('dashboard: a section that cannot be read', () => {
  const CASES: [DashboardName, string[], string, string][] = [
    ['admin', ADMIN, 'Hospital overview', 'New patients this month'],
    ['billing', BILLING_STAFF, 'Billing', 'Unpaid invoices'],
    ['reception', RECEPTIONIST, 'Front desk', 'Walk-ins waiting'],
    ['doctor', DOCTOR, 'My day', 'My patients'],
  ]

  it.each(CASES)(
    'says the %s dashboard could not be loaded, and Retry asks again',
    async (name, permissions, heading, label) => {
      const answer = dashboards[name]
      dashboards[name] = (n) => (n === 1 ? serverError() : answer(n))
      const user = userEvent.setup()
      renderDashboard(permissions)

      const section = await region(heading)
      expect(
        await within(section).findByText(`Couldn't load the ${name} dashboard`),
      ).toBeInTheDocument()
      // The wording names no cause: a refusal is not an outage.
      expect(within(section).getByText('These figures are not available.')).toBeInTheDocument()
      expect(within(section).queryByText(/reached|right now/)).not.toBeInTheDocument()
      // Nothing the server said is shown, and no tile pretends to a figure.
      expect(document.body.textContent).not.toMatch(/Traceback|asyncpg/)
      expect(within(section).queryByText(label)).not.toBeInTheDocument()
      expect(api.requests('get', `/dashboards/${name}`)).toHaveLength(1)

      await user.click(within(section).getByRole('button', { name: 'Retry' }))

      expect(await within(section).findByText(label)).toBeInTheDocument()
      expect(within(section).queryByText(/Couldn't load/)).not.toBeInTheDocument()
      const requests = api.requests('get', `/dashboards/${name}`)
      expect(requests).toHaveLength(2)
      expect(requests[1].params).toBeUndefined()
    },
  )

  it('keeps the billing section when only the admin dashboard fails', async () => {
    dashboards.admin = () => serverError()
    renderDashboard(ADMIN)

    expect(
      await within(await region('Hospital overview')).findByText("Couldn't load the admin dashboard"),
    ).toBeInTheDocument()
    const billing = await region('Billing')
    expect(await within(billing).findByText('Unpaid invoices')).toBeInTheDocument()
    expect(within(billing).queryByText(/Couldn't load/)).not.toBeInTheDocument()
  })
})

describe("dashboard: the queue's day", () => {
  /** Move the browser's clock three days past the hospital's `meta.today`. */
  function browserClockOnAnotherDay() {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-09T12:00:00Z'))
    const browserDay = todayISODate()
    expect(browserDay).not.toBe(receptionDashboardFixture.meta.today)
    return browserDay
  }

  it("asks for the hospital's day, not the browser's", async () => {
    browserClockOnAnotherDay()
    renderDashboard(RECEPTIONIST)

    await screen.findByRole('row', { name: /Meera Nair/ })
    const requests = api.requests('get', '/appointments')
    expect(requests).toHaveLength(1)
    expect(requests[0].url).toBe('/appointments')
    expect(requests[0].params).toEqual({ date: '2026-10-06', page: 1, page_size: 25 })
    // One dashboard request serves both the section and the date.
    expect(api.requests('get', '/dashboards/reception')).toHaveLength(1)
  })

  it("takes the day from the first section shown when there are two", async () => {
    browserClockOnAnotherDay()
    dashboards.admin = () =>
      loaded('admin', {
        ...adminDashboardFixture,
        meta: { ...adminDashboardFixture.meta, today: '2026-10-07' },
      })
    renderDashboard(ADMIN)

    await screen.findByRole('row', { name: /Meera Nair/ })
    expect(queueDates()).toEqual(['2026-10-07'])
    expect(dashboardReads()).toEqual(['/dashboards/admin', '/dashboards/billing'])
  })

  it("falls back to the browser's day when the dashboard cannot be read", async () => {
    const browserDay = browserClockOnAnotherDay()
    dashboards.reception = () => serverError()
    renderDashboard(RECEPTIONIST)

    // The failed section does not blank the queue.
    expect(await screen.findByRole('row', { name: /Meera Nair/ })).toBeInTheDocument()
    expect(screen.getByText("Couldn't load the reception dashboard")).toBeInTheDocument()
    expect(queueDates()).toEqual([browserDay])
  })

  it("sends a nurse's browser day, with no dashboard request", async () => {
    const browserDay = browserClockOnAnotherDay()
    renderDashboard(NURSE)

    await screen.findByRole('row', { name: /Meera Nair/ })
    expect(queueDates()).toEqual([browserDay])
    expect(dashboardReads()).toEqual([])
  })
})

describe('dashboard: figures after a write', () => {
  it('reads the reception dashboard again after a check-in', async () => {
    const after: ReceptionDashboard = {
      ...receptionDashboardFixture,
      walk_in_queue: { ...receptionDashboardFixture.walk_in_queue, not_arrived: 6 },
    }
    dashboards.reception = (n) => loaded('reception', n === 1 ? receptionDashboardFixture : after)
    const user = userEvent.setup()
    renderDashboard(RECEPTIONIST)

    const row = await screen.findByRole('row', { name: /Meera Nair/ })
    const section = await region('Front desk')
    expect(await within(section).findByText('Longest wait 42 min · 0 not arrived')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Check in Meera Nair' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Patient checked in'))
    const writes = api.sent.filter((c) => c.method !== 'get')
    expect(writes).toHaveLength(1)
    expect(writes[0].method).toBe('post')
    expect(writes[0].url).toBe('/appointments/a1/check-in')
    // The tile shows what the server says now, without waiting for it to age out.
    expect(await within(section).findByText('Longest wait 42 min · 6 not arrived')).toBeInTheDocument()
    expect(api.requests('get', '/dashboards/reception')).toHaveLength(2)
  })

  it('keeps the figures and says they could not be refreshed when the read after a write fails', async () => {
    const after: ReceptionDashboard = {
      ...receptionDashboardFixture,
      walk_in_queue: { ...receptionDashboardFixture.walk_in_queue, not_arrived: 6 },
    }
    dashboards.reception = (n) =>
      n === 2 ? serverError() : loaded('reception', n === 1 ? receptionDashboardFixture : after)
    const user = userEvent.setup()
    renderDashboard(RECEPTIONIST)

    const row = await screen.findByRole('row', { name: /Meera Nair/ })
    const section = await region('Front desk')
    expect(await within(section).findByText('Longest wait 42 min · 0 not arrived')).toBeInTheDocument()
    expect(within(section).queryByText(/Couldn't refresh/)).not.toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Check in Meera Nair' }))

    // The earlier figures stay on show, marked as possibly out of date.
    expect(
      await within(section).findByText("Couldn't refresh these figures. They may be out of date."),
    ).toBeInTheDocument()
    expect(within(section).getByText('Longest wait 42 min · 0 not arrived')).toBeInTheDocument()
    expect(within(section).queryByText(/Couldn't load/)).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/Traceback|asyncpg/)
    expect(api.requests('get', '/dashboards/reception')).toHaveLength(2)
    // The queue keeps the hospital's day; it does not fall back to the browser's.
    expect(new Set(queueDates())).toEqual(new Set([receptionDashboardFixture.meta.today]))

    await user.click(within(section).getByRole('button', { name: 'Retry' }))

    expect(await within(section).findByText('Longest wait 42 min · 6 not arrived')).toBeInTheDocument()
    expect(within(section).queryByText(/Couldn't refresh/)).not.toBeInTheDocument()
    const requests = api.requests('get', '/dashboards/reception')
    expect(requests).toHaveLength(3)
    expect(requests[2].params).toBeUndefined()
  })

  it('reads the billing dashboard again after a payment is recorded', async () => {
    const after: BillingDashboard = {
      ...billingDashboardFixture,
      unpaid_invoices: {
        invoice_count: 1,
        outstanding_amount: '1100.00',
        issued_count: 1,
        partially_paid_count: 0,
      },
    }
    dashboards.billing = (n) => loaded('billing', n === 1 ? billingDashboardFixture : after)
    const user = userEvent.setup()
    renderDashboard(BILLING_STAFF)

    const section = await region('Billing')
    expect(await within(section).findByText(`${inr('1550.00')} outstanding`)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Take a payment elsewhere' }))

    expect(await within(section).findByText(`${inr('1100.00')} outstanding`)).toBeInTheDocument()
    const writes = api.sent.filter((c) => c.method !== 'get')
    expect(writes).toHaveLength(1)
    expect(writes[0].method).toBe('post')
    expect(writes[0].url).toBe('/invoices/inv-1/payments')
    expect(bodyOf(writes[0])).toEqual({ amount: '450.00', method: 'cash' })
    expect(writes[0].headers.get('Idempotency-Key')).toBeTruthy()
    expect(api.requests('get', '/dashboards/billing')).toHaveLength(2)
  })

  it('reads the visible dashboards again after a patient is registered', async () => {
    const after: AdminDashboard = {
      ...adminDashboardFixture,
      patient_registrations_this_month: {
        ...adminDashboardFixture.patient_registrations_this_month,
        registered: 12,
        active_total: 12,
      },
    }
    dashboards.admin = (n) => loaded('admin', n === 1 ? adminDashboardFixture : after)
    const user = userEvent.setup()
    renderDashboard(ADMIN)

    const section = await region('Hospital overview')
    expect(await within(section).findByText('11 active patients')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Register a patient elsewhere' }))

    expect(await within(section).findByText('12 active patients')).toBeInTheDocument()
    const writes = api.sent.filter((c) => c.method !== 'get')
    expect(writes).toHaveLength(1)
    expect(writes[0].method).toBe('post')
    expect(writes[0].url).toBe('/patients')
    expect(bodyOf(writes[0])).toEqual({
      first_name: 'Kavya',
      last_name: 'Iyer',
      date_of_birth: '1990-04-12',
      gender: 'female',
    })
    await waitFor(() => expect(api.requests('get', '/dashboards/billing')).toHaveLength(2))
    expect(api.requests('get', '/dashboards/admin')).toHaveLength(2)
    // Still only the two dashboards this user is shown.
    expect([...new Set(dashboardReads())]).toEqual(['/dashboards/admin', '/dashboards/billing'])
  })
})
