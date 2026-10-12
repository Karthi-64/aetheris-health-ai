import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { AppointmentStatus, AppointmentSummary } from '@/api/appointments'
import type { LabOrder, LabOrderItem, LabOrderStatus, LabResultFlag, LabTest } from '@/api/lab'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { PatientInvoices } from '@/components/billing/PatientInvoices'
import { PatientLabOrders } from '@/components/laboratory/PatientLabOrders'
import { kindPresentation } from '@/components/notifications/kinds'
import { navForPermissions, type Permission } from '@/lib/rbac'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import AppointmentsPage from '../appointments/AppointmentsPage'
import LabCatalogPage from './LabCatalogPage'
import LabOrderDetailPage from './LabOrderDetailPage'
import LabOrdersPage from './LabOrdersPage'

/**
 * Laboratory, against the merged backend contract
 * (docs/18-API_CONTRACTS.md §8; `backend/app/api/v1/lab_orders.py`,
 * `tests_catalog.py`). The real hooks, permission check, `http` wrapper and
 * Axios instance run; only the network adapter is replaced by an in-memory
 * "server" that keeps the order lifecycle and — like the real one — is the
 * only thing that decides a result's flag.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → lab permissions as seeded in backend/app/seeds/seed.py (§8.2).
const BASE = ['notification.read.own', 'patient.read', 'appointment.read']
const ADMIN = [
  ...BASE,
  'invoice.read',
  'lab.test.read',
  'lab.test.create',
  'lab.test.update',
  'lab.order.read',
  'lab.order.create',
  'lab.order.cancel',
  'lab.order.collect_sample',
  'lab.order.enter_results',
  'lab.order.release',
  'lab.order.amend',
]
const DOCTOR = [
  ...BASE,
  'invoice.read.own',
  'appointment.check_in',
  'appointment.start',
  'appointment.complete',
  'lab.test.read',
  'lab.order.read',
  'lab.order.create',
  'lab.order.cancel',
]
const LAB_TECH = ['notification.read.own', 'lab.test.read', 'lab.order.read', 'lab.order.collect_sample', 'lab.order.enter_results']
const NURSE = [...BASE, 'appointment.check_in', 'lab.order.read']
const RECEPTIONIST = [...BASE, 'appointment.book', 'appointment.cancel', 'appointment.check_in', 'invoice.read']

// ── The in-memory server ────────────────────────────────────────────────────

function test(code: string, name: string, extra: Partial<LabTest> = {}): LabTest {
  return {
    id: `t-${code}`,
    code,
    name,
    category: 'Biochemistry',
    unit: null,
    result_type: 'numeric',
    reference_ranges: [{ sex: 'any', low: '1', high: '2' }],
    turnaround_hours: 4,
    price: '100.00',
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...extra,
  }
}

/** The server's ranges. The frontend never sees these — only the flags they produce. */
const SERVER_RANGES: Record<string, { low: number; high: number; critical: number }> = {
  HB: { low: 12, high: 15.5, critical: 20 },
  K: { low: 3.5, high: 5.1, critical: 6.5 },
}

function judge(item: LabOrderItem, value: string): Partial<LabOrderItem> {
  const range = SERVER_RANGES[item.test_code]
  if (item.result_type === 'text' || !range) {
    return { result_value: value, result_flag: null, reference_low: null, reference_high: null }
  }
  const n = Number(value)
  const flag: LabResultFlag =
    n >= range.critical ? 'critical' : n > range.high ? 'high' : n < range.low ? 'low' : 'normal'
  return {
    result_value: value,
    result_flag: flag,
    reference_low: range.low.toFixed(4),
    reference_high: range.high.toFixed(4),
  }
}

function item(t: LabTest, extra: Partial<LabOrderItem> = {}): LabOrderItem {
  return {
    id: `i-${t.code}`,
    test_id: t.id,
    test_code: t.code,
    test_name: t.name,
    result_type: t.result_type,
    price: t.price,
    sample_id: null,
    sample_collected_at: null,
    result_value: null,
    result_unit: t.unit,
    result_flag: null,
    reference_low: null,
    reference_high: null,
    result_entered_at: null,
    released_at: null,
    notes: null,
    amendments: [],
    ...extra,
  }
}

function order(id: string, status: LabOrderStatus, items: LabOrderItem[], extra: Partial<LabOrder> = {}): LabOrder {
  return {
    id,
    appointment_id: 'a1',
    patient_id: 'p1',
    patient_name: 'Ananya Rao',
    patient_mrn: 'MRN-2026-00007',
    doctor_id: 'd1',
    doctor_name: 'Dr. Priya Sharma',
    ordered_at: '2026-10-05T09:00:00Z',
    priority: 'routine',
    status,
    notes: null,
    collected_at: null,
    results_entered_at: null,
    released_at: null,
    released_by: null,
    cancelled_at: null,
    cancel_reason: null,
    invoice_id: 'inv1',
    turnaround_minutes: null,
    has_abnormal: false,
    has_critical: false,
    items,
    ...extra,
  }
}

function appt(id: string, patient: string, status: AppointmentStatus): AppointmentSummary {
  return {
    id,
    patient_id: `p-${id}`,
    patient_name: patient,
    doctor_id: 'd1',
    doctor_name: 'Dr. Priya Sharma',
    scheduled_start: '2026-10-05T04:00:00Z',
    scheduled_end: '2026-10-05T04:30:00Z',
    status,
    type: 'new',
  }
}

const HB = test('HB', 'Haemoglobin', { category: 'Haematology', unit: 'g/dL', price: '250.00' })
const K = test('K', 'Serum potassium', { unit: 'mmol/L', price: '300.00' })
const URINE = test('URINE', 'Urine microscopy', { category: 'Microbiology', result_type: 'text', reference_ranges: [], price: '150.00' })
const ESR = test('ESR', 'ESR (retired)', { category: 'Haematology', is_active: false })

let catalog: LabTest[]
let orders: LabOrder[]
let appointments: AppointmentSummary[]
/** Return an outcome to answer a request yourself; nothing to let the server answer. */
let intercept: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome> | undefined
let fake: FakeApi

const pageOf = (items: unknown[], config: InternalAxiosRequestConfig): Outcome => {
  const { page = 1, page_size: size = 25 } = (config.params ?? {}) as { page?: number; page_size?: number }
  return {
    status: 200,
    data: {
      success: true,
      message: 'ok',
      data: items.slice((page - 1) * size, page * size).map((x) => structuredClone(x)),
      metadata: {
        pagination: { page, page_size: size, total_records: items.length, total_pages: Math.max(1, Math.ceil(items.length / size)) },
      },
    },
  }
}

function summarise(o: LabOrder) {
  o.has_abnormal = o.items.some((i) => i.result_flag !== null && i.result_flag !== 'normal')
  o.has_critical = o.items.some((i) => i.result_flag === 'critical')
}

const wrongStatus = (verb: string, o: LabOrder) =>
  fail(400, `Cannot ${verb} a lab order that is ${o.status.replace('_', ' ')}.`, { error_code: 'BUSINESS_RULE_VIOLATION' })

function server(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const method = config.method ?? 'get'
  const params = (config.params ?? {}) as Record<string, unknown>

  if (url === '/tests-catalog' && method === 'get') {
    const q = typeof params.q === 'string' ? params.q.toLowerCase() : ''
    return pageOf(
      catalog.filter(
        (t) =>
          (!q || t.name.toLowerCase().startsWith(q) || t.code.toLowerCase() === q) &&
          (params.category === undefined || t.category === params.category) &&
          (params.is_active === undefined || t.is_active === params.is_active),
      ),
      config,
    )
  }
  if (url === '/tests-catalog' && method === 'post') {
    const body = bodyOf(config) as LabTest
    if (catalog.some((t) => t.code === body.code)) return fail(409, `A lab test with code '${body.code}' already exists.`)
    const created = test(body.code, body.name, { ...body, id: `t-${body.code}` })
    catalog.push(created)
    return ok(created, 201)
  }
  const testPatch = /^\/tests-catalog\/([^/]+)$/.exec(url)
  if (testPatch && method === 'patch') {
    const t = catalog.find((x) => x.id === testPatch[1])
    if (!t) return fail(404, 'Lab test not found.')
    Object.assign(t, bodyOf(config))
    return ok({ ...t })
  }

  if (url === '/lab-orders' && method === 'get') {
    return pageOf(
      orders.filter(
        (o) =>
          (params.status === undefined || o.status === params.status) &&
          (params.priority === undefined || o.priority === params.priority) &&
          (params.patient_id === undefined || o.patient_id === params.patient_id) &&
          (params.appointment_id === undefined || o.appointment_id === params.appointment_id),
      ),
      config,
    )
  }
  if (url === '/lab-orders' && method === 'post') {
    const body = bodyOf(config) as { appointment_id: string; test_ids: string[]; priority: LabOrder['priority']; notes?: string }
    const visit = appointments.find((a) => a.id === body.appointment_id)
    const inactive = body.test_ids.map((id) => catalog.find((t) => t.id === id)).find((t) => t && !t.is_active)
    if (inactive) return fail(422, `Lab test '${inactive.code}' is inactive and cannot be ordered.`, { errors: { errors: [{ field: 'test_ids.0', message: 'Inactive.' }] } })
    const created = order(
      `o${orders.length + 1}`,
      'ordered',
      body.test_ids.map((id) => item(catalog.find((t) => t.id === id)!)),
      { appointment_id: body.appointment_id, patient_id: visit?.patient_id ?? 'p1', patient_name: visit?.patient_name ?? 'Ananya Rao', priority: body.priority, notes: body.notes ?? null },
    )
    orders.unshift(created)
    return ok(structuredClone(created), 201)
  }

  const match = /^\/lab-orders\/([^/]+)(?:\/(collect|enter-results|release|cancel)|\/items\/([^/]+)\/amend)?$/.exec(url)
  if (match) {
    const [, id, step, amendItem] = match
    const o = orders.find((x) => x.id === id)
    if (!o) return fail(404, 'Lab order not found.')
    if (method === 'get') return ok(structuredClone(o))

    if (step === 'collect') {
      if (o.status !== 'ordered') return wrongStatus('collect samples for', o)
      o.items.forEach((i, n) => Object.assign(i, { sample_id: `S-000000000${n}`, sample_collected_at: '2026-10-05T09:10:00Z' }))
      Object.assign(o, { status: 'collected', collected_at: '2026-10-05T09:10:00Z' })
    } else if (step === 'enter-results') {
      if (!['collected', 'in_progress', 'results_entered'].includes(o.status)) return wrongStatus('enter results for', o)
      const { results } = bodyOf(config) as { results: { item_id: string; value: string; notes?: string }[] }
      for (const r of results) {
        const target = o.items.find((i) => i.id === r.item_id)!
        Object.assign(target, judge(target, r.value), { notes: r.notes ?? null, result_entered_at: '2026-10-05T10:00:00Z' })
      }
      const complete = o.items.every((i) => i.result_value !== null)
      Object.assign(o, { status: complete ? 'results_entered' : 'in_progress', results_entered_at: complete ? '2026-10-05T10:00:00Z' : null })
    } else if (step === 'release') {
      if (o.status !== 'results_entered') return wrongStatus('release', o)
      Object.assign(o, { status: 'released', released_at: '2026-10-05T12:20:00Z', released_by: 'u-admin', turnaround_minutes: 200 })
    } else if (step === 'cancel') {
      if (o.status === 'released' || o.status === 'cancelled') return wrongStatus('cancel', o)
      Object.assign(o, { status: 'cancelled', cancelled_at: '2026-10-05T09:30:00Z', cancel_reason: (bodyOf(config) as { reason: string }).reason })
    } else if (amendItem) {
      if (o.status !== 'released') return wrongStatus('amend a result on', o)
      const target = o.items.find((i) => i.id === amendItem)!
      const { new_value, reason } = bodyOf(config) as { new_value: string; reason: string }
      const before = { previous_value: target.result_value, previous_flag: target.result_flag }
      Object.assign(target, judge(target, new_value))
      target.amendments.push({ id: `am${target.amendments.length + 1}`, ...before, new_value, new_flag: target.result_flag, reason, amended_by: 'u-admin', amended_at: '2026-10-05T13:00:00Z' })
    }
    summarise(o)
    return ok(structuredClone(o))
  }

  if (url === '/appointments') return pageOf(appointments, config)
  if (url === '/notifications/unread-count') return ok({ unread: 0 })
  return pageOf([], config)
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  catalog = [HB, K, URINE, ESR].map((t) => structuredClone(t))
  orders = []
  appointments = [appt('a1', 'Ananya Rao', 'checked_in')]
  intercept = () => undefined
  fake = installFakeApi(async (config) => (await intercept(config)) ?? server(config))
})

afterEach(() => {
  fake.restore()
  signOut()
})

function renderAt(path: string, permissions: string[], extra?: React.ReactNode) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        {extra}
        <Routes>
          <Route path="/dashboard" element={<p>Dashboard home</p>} />
          <Route path="/appointments" element={<AppointmentsPage />} />
          <Route path="/billing/:invoiceId" element={<p>Invoice page</p>} />
          <Route path="/patients/:patientId" element={<p>Patient page</p>} />
          <Route
            path="/laboratory"
            element={
              <RequirePermission permission="lab.order.read">
                <LabOrdersPage />
              </RequirePermission>
            }
          />
          <Route
            path="/laboratory/orders/:orderId"
            element={
              <RequirePermission permission="lab.order.read">
                <LabOrderDetailPage />
              </RequirePermission>
            }
          />
          <Route
            path="/laboratory/catalog"
            element={
              <RequirePermission permission="lab.test.read">
                <LabCatalogPage />
              </RequirePermission>
            }
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const sent = (method: string, urlPart: string) => fake.requests(method, urlPart)
const wire = (config: InternalAxiosRequestConfig) => JSON.parse(JSON.stringify(config.params ?? {})) as Record<string, unknown>
const actionNames = () =>
  screen
    .queryAllByRole('button')
    .map((b) => b.textContent?.trim() ?? '')
    .filter((t) => /Collect samples|results|Release|Cancel order|Amend/.test(t))
const dialog = () => screen.findByRole('dialog')
/** The order's header: its status and priority badges, apart from the details below. */
const header = () => within(document.querySelector('header') as HTMLElement)
const choose = async (user: ReturnType<typeof userEvent.setup>, combobox: HTMLElement, option: string) => {
  await user.click(combobox)
  await user.click(await screen.findByRole('option', { name: option }))
}

const resulted = () =>
  order('o1', 'results_entered', [
    item(HB, { sample_id: 'S-1', sample_collected_at: '2026-10-05T09:10:00Z', ...judge(item(HB), '13.5'), result_entered_at: '2026-10-05T10:00:00Z' }),
    item(K, { sample_id: 'S-2', sample_collected_at: '2026-10-05T09:10:00Z', ...judge(item(K), '7.9'), result_entered_at: '2026-10-05T10:00:00Z' }),
  ], { collected_at: '2026-10-05T09:10:00Z', results_entered_at: '2026-10-05T10:00:00Z', has_abnormal: true, has_critical: true })

// ── Access ──────────────────────────────────────────────────────────────────

describe('laboratory access', () => {
  it.each([
    ['an admin', ADMIN, true],
    ['a doctor', DOCTOR, true],
    ['a lab technician', LAB_TECH, true],
    ['a nurse', NURSE, true],
    ['a receptionist', RECEPTIONIST, false],
  ])('the sidebar offers Laboratory to %s: %s', (_who, permissions, expected) => {
    const paths = navForPermissions(permissions as Permission[]).map((n) => n.to)
    expect(paths.includes('/laboratory')).toBe(expected)
  })

  it('sends a receptionist who types the address back to the dashboard, asking the API nothing', async () => {
    renderAt('/laboratory', RECEPTIONIST)

    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    expect(sent('get', '/lab-orders')).toHaveLength(0)
  })

  it('keeps a nurse, who can read orders but not the catalog, out of the catalog', async () => {
    renderAt('/laboratory/catalog', NURSE)

    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    expect(sent('get', '/tests-catalog')).toHaveLength(0)
  })

  it('offers the catalog link only to those who can read it', async () => {
    renderAt('/laboratory', NURSE)
    await screen.findByText('No lab orders yet')
    expect(screen.queryByRole('link', { name: /Test catalog/ })).not.toBeInTheDocument()
  })
})

// ── Worklist ────────────────────────────────────────────────────────────────

describe('lab worklist', () => {
  it('lists orders as the API returns them, with the server\'s critical marker', async () => {
    orders = [resulted(), order('o2', 'ordered', [item(URINE)], { patient_name: 'Ravi Menon', patient_mrn: 'MRN-2026-00002', priority: 'stat' })]
    renderAt('/laboratory', LAB_TECH)

    const first = await screen.findByRole('row', { name: /Ananya Rao/ })
    expect(within(first).getByText('MRN-2026-00007')).toBeInTheDocument()
    expect(within(first).getByText('HB, K')).toBeInTheDocument()
    expect(within(first).getByText('Dr. Priya Sharma')).toBeInTheDocument()
    expect(within(first).getByText('Awaiting release')).toBeInTheDocument()
    expect(within(first).getByText('Critical result')).toBeInTheDocument()
    expect(within(first).getByRole('link', { name: /^Open lab order for Ananya Rao, ordered / })).toHaveAttribute('href', '/laboratory/orders/o1')
    const second = screen.getByRole('row', { name: /Ravi Menon/ })
    expect(within(second).getByText('STAT')).toBeInTheDocument()
    expect(within(second).getByText('Ordered')).toBeInTheDocument()
    expect(within(second).queryByText(/result$/)).not.toBeInTheDocument()

    const [request] = sent('get', '/lab-orders')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/lab-orders')
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
  })

  it('filters by status and by priority using the API\'s own parameters', async () => {
    orders = [resulted(), order('o2', 'ordered', [item(URINE)], { patient_name: 'Ravi Menon', priority: 'stat' })]
    const user = userEvent.setup()
    renderAt('/laboratory', LAB_TECH)
    await screen.findByRole('row', { name: /Ananya Rao/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Awaiting release')
    await waitFor(() => expect(wire(sent('get', '/lab-orders').at(-1)!)).toMatchObject({ status: 'results_entered', page: 1 }))
    await waitFor(() => expect(screen.queryByRole('row', { name: /Ravi Menon/ })).not.toBeInTheDocument())

    await choose(user, screen.getByRole('combobox', { name: 'Filter by priority' }), 'STAT')
    await waitFor(() => expect(wire(sent('get', '/lab-orders').at(-1)!)).toMatchObject({ status: 'results_entered', priority: 'stat' }))
    expect(await screen.findByText('No matching lab orders')).toBeInTheDocument()
  })

  it('pages through the server\'s pages', async () => {
    orders = Array.from({ length: 30 }, (_, n) => order(`o${n}`, 'ordered', [item(HB)], { patient_name: `Patient ${n}` }))
    const user = userEvent.setup()
    renderAt('/laboratory', LAB_TECH)
    await screen.findByRole('row', { name: /Patient 0(?!\d)/ })

    await user.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() => expect(wire(sent('get', '/lab-orders').at(-1)!)).toMatchObject({ page: 2 }))
    expect(await screen.findByRole('row', { name: /Patient 29/ })).toBeInTheDocument()
  })

  it('narrows to one patient from a link, and says so', async () => {
    orders = [resulted()]
    renderAt('/laboratory?patient_id=p1', NURSE)

    expect(await screen.findByText(/Showing lab orders for/)).toBeInTheDocument()
    expect(wire(sent('get', '/lab-orders')[0])).toMatchObject({ patient_id: 'p1' })
  })

  it('says where orders come from when there are none', async () => {
    renderAt('/laboratory', LAB_TECH)

    expect(await screen.findByText('No lab orders yet')).toBeInTheDocument()
    expect(screen.getByText(/Tests are ordered from a visit/)).toBeInTheDocument()
  })

  it('offers a retry when the worklist cannot be loaded', async () => {
    intercept = (c) => (c.url === '/lab-orders' ? fail(500, 'boom') : undefined)
    const user = userEvent.setup()
    renderAt('/laboratory', LAB_TECH)

    expect(await screen.findByText("Couldn't load lab orders")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    orders = [resulted()]
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: /Ananya Rao/ })).toBeInTheDocument()
  })
})

// ── Order detail ────────────────────────────────────────────────────────────

describe('lab order detail', () => {
  it('shows each result with the flag and range the server returned', async () => {
    const o = resulted()
    o.items.push(item(URINE, { result_value: 'No organisms seen', result_entered_at: '2026-10-05T10:00:00Z', notes: 'Clear sample' }))
    orders = [o]
    renderAt('/laboratory/orders/o1', ADMIN)

    expect(await screen.findByRole('heading', { level: 1, name: 'Lab order' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Ananya Rao' })).toHaveAttribute('href', '/patients/p1')
    const cards = screen.getAllByRole('listitem')
    const hb = cards.find((c) => within(c).queryByRole('heading', { name: 'Haemoglobin' }))!
    expect(within(hb).getByText('13.5 g/dL')).toBeInTheDocument()
    expect(within(hb).getByText('Normal')).toBeInTheDocument()
    expect(within(hb).getByText(/Reference range used:/)).toHaveTextContent('12 – 15.5 g/dL')
    const k = cards.find((c) => within(c).queryByRole('heading', { name: 'Serum potassium' }))!
    expect(within(k).getByText('7.9 mmol/L')).toBeInTheDocument()
    expect(within(k).getByText('Critical')).toBeInTheDocument()
    // A text test has no range, which is said — it is not shown as normal.
    const urine = cards.find((c) => within(c).queryByRole('heading', { name: 'Urine microscopy' }))!
    expect(within(urine).getByText('No reference range')).toBeInTheDocument()
    expect(within(urine).queryByText('Normal')).not.toBeInTheDocument()
    expect(within(urine).getByText('Note: Clear sample')).toBeInTheDocument()

    expect(screen.getByRole('alert')).toHaveTextContent('Critical result')
    expect(screen.getByRole('link', { name: /View invoice/ })).toHaveAttribute('href', '/billing/inv1')
  })

  it('names the invoice without a link for a user who cannot open it', async () => {
    orders = [resulted()]
    renderAt('/laboratory/orders/o1', LAB_TECH)

    await screen.findByRole('heading', { level: 1, name: 'Lab order' })
    expect(screen.queryByRole('link', { name: /View invoice/ })).not.toBeInTheDocument()
    expect(screen.getByText(/^Invoice ·/)).toBeInTheDocument()
    // And the patient is named, not linked, without patient.read.
    expect(screen.queryByRole('link', { name: 'Ananya Rao' })).not.toBeInTheDocument()
  })

  it('says an order is not found rather than failing', async () => {
    renderAt('/laboratory/orders/missing', NURSE)

    expect(await screen.findByText('Lab order not found')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
  })
})

// ── Lifecycle ───────────────────────────────────────────────────────────────

describe('lab order lifecycle', () => {
  it('a technician collects, with no body, and the next step appears', async () => {
    orders = [order('o1', 'ordered', [item(HB), item(K)])]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', LAB_TECH)
    await screen.findByRole('heading', { level: 1, name: 'Lab order' })
    expect(actionNames()).toEqual(['Collect samples'])

    await user.click(screen.getByRole('button', { name: /Collect samples/ }))
    expect(await dialog()).toHaveTextContent('2 samples have been taken from Ananya Rao')
    expect(sent('post', '/collect')).toHaveLength(0)
    await user.click(within(await dialog()).getByRole('button', { name: 'Mark collected' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Samples collected for Ananya Rao'))
    const [request] = sent('post', '/collect')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/lab-orders/o1/collect')
    expect(request.data).toBeUndefined()
    await waitFor(() => expect(header().getByText('Sample collected')).toBeInTheDocument())
    expect(screen.getByText('S-0000000000')).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual(['Enter results']))
  })

  it('sends only the results that were entered, and shows progress', async () => {
    orders = [order('o1', 'collected', [item(HB, { sample_id: 'S-1', sample_collected_at: 'x' }), item(K, { sample_id: 'S-2', sample_collected_at: 'x' })])]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', LAB_TECH)
    await user.click(await screen.findByRole('button', { name: /Enter results/ }))
    const d = await dialog()

    const [hbValue] = within(d).getAllByLabelText(/^Result/)
    const [hbNote] = within(d).getAllByLabelText(/^Note/)
    await user.type(hbValue, ' 13.5 ')
    await user.type(hbNote, 'Repeat in 3 months')
    await user.click(within(d).getByRole('button', { name: 'Save results' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('1 of 2 results entered'))
    const [request] = sent('post', '/enter-results')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/lab-orders/o1/enter-results')
    // No flag is sent — the server rejects one — and the blank test is left out.
    expect(bodyOf(request)).toEqual({ results: [{ item_id: 'i-HB', value: '13.5', notes: 'Repeat in 3 months' }] })
    await waitFor(() => expect(header().getByText('In progress')).toBeInTheDocument())
    expect(screen.getByText('13.5 g/dL')).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual(['Continue results']))
  })

  it('shows a critical flag as the server returned it, and no more', async () => {
    orders = [order('o1', 'collected', [item(HB, { sample_collected_at: 'x' }), item(K, { sample_collected_at: 'x' })])]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', LAB_TECH)
    await user.click(await screen.findByRole('button', { name: /Enter results/ }))
    const d = await dialog()
    const [hbValue, kValue] = within(d).getAllByLabelText(/^Result/)
    await user.type(hbValue, '13.5')
    await user.type(kValue, '7.9')
    await user.click(within(d).getByRole('button', { name: 'Save results' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'All results entered — awaiting release. A critical result was flagged and the ordering doctor has been notified.',
      ),
    )
    expect(await screen.findByText('Critical')).toBeInTheDocument()
    expect(screen.getByText('Critical result')).toBeInTheDocument()
    expect(screen.getByText(/have not been released yet/)).toBeInTheDocument()
    // A technician cannot release: it is said, and no button is offered.
    expect(screen.getByText('Awaiting release', { selector: 'p' })).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual(['Edit results']))
  })

  it('refuses text for a numeric test, and an empty form, before asking the server', async () => {
    orders = [order('o1', 'collected', [item(HB, { sample_collected_at: 'x' })])]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', LAB_TECH)
    await user.click(await screen.findByRole('button', { name: /Enter results/ }))
    const d = await dialog()

    const value = within(d).getByLabelText(/^Result/)
    await user.type(value, 'high')
    await user.click(within(d).getByRole('button', { name: 'Save results' }))
    expect(await within(d).findByText('Enter a number')).toBeInTheDocument()
    expect(value).toBeInvalid()

    await user.clear(value)
    await user.type(within(d).getByLabelText(/^Note/), 'x')
    await user.click(within(d).getByRole('button', { name: 'Save results' }))
    expect(await within(d).findByText('Enter the result this note belongs to')).toBeInTheDocument()
    expect(sent('post', '/enter-results')).toHaveLength(0)
  })

  it('names the test when the server rejects a value', async () => {
    orders = [order('o1', 'collected', [item(HB, { sample_collected_at: 'x' }), item(K, { sample_collected_at: 'x' })])]
    intercept = (c) =>
      c.url?.endsWith('/enter-results')
        ? fail(422, 'Validation failed.', { errors: { errors: [{ field: 'results.0.value', message: 'Must be a number.' }] } })
        : undefined
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', LAB_TECH)
    await user.click(await screen.findByRole('button', { name: /Enter results/ }))
    const d = await dialog()
    // Only potassium is filled, so it is item 0 of what is sent.
    await user.type(within(d).getAllByLabelText(/^Result/)[1], '5')
    await user.click(within(d).getByRole('button', { name: 'Save results' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Serum potassium: Must be a number.')
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('an admin releases only after confirming, with no body', async () => {
    orders = [resulted()]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', ADMIN)
    await screen.findByRole('heading', { level: 1, name: 'Lab order' })
    expect(actionNames()).toEqual(['Release results', 'Edit results', 'Cancel order'])

    await user.click(screen.getByRole('button', { name: /Release results/ }))
    const d = await dialog()
    expect(d).toHaveTextContent('2 results for Ananya Rao will be released and Dr. Priya Sharma will be notified')
    expect(d).toHaveTextContent('can only be changed by an amendment')
    expect(sent('post', '/release')).toHaveLength(0)
    await user.click(within(d).getByRole('button', { name: 'Release results' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Results released for Ananya Rao'))
    expect(sent('post', '/release')[0].data).toBeUndefined()
    await waitFor(() => expect(header().getByText('Released')).toBeInTheDocument())
    expect(screen.getByText('3 h 20 min')).toBeInTheDocument()
    // Released: nothing but amendment is left.
    await waitFor(() => expect(actionNames()).toEqual(['Amend', 'Amend']))
  })

  it('tells the user when the order moved on before they released it', async () => {
    orders = [resulted()]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', ADMIN)
    await user.click(await screen.findByRole('button', { name: /Release results/ }))
    orders[0].status = 'cancelled'
    orders[0].cancel_reason = 'Duplicate order'
    await user.click(within(await dialog()).getByRole('button', { name: 'Release results' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Cannot release a lab order that is cancelled.'))
    // The screen catches up with the server.
    expect(await screen.findByText('This order was cancelled')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('a doctor cancels with a reason, and is told the charge stays', async () => {
    orders = [order('o1', 'ordered', [item(HB)])]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', DOCTOR)
    await screen.findByRole('heading', { level: 1, name: 'Lab order' })
    expect(actionNames()).toEqual(['Cancel order'])

    await user.click(screen.getByRole('button', { name: /Cancel order/ }))
    const d = await dialog()
    expect(d).toHaveTextContent('a single test cannot be removed')
    expect(d).toHaveTextContent('Billing has to correct the invoice by hand')
    await user.click(within(d).getByRole('button', { name: 'Cancel order' }))
    expect(await within(d).findByText('Give a reason for cancelling')).toBeInTheDocument()
    expect(sent('post', '/cancel')).toHaveLength(0)

    await user.type(within(d).getByLabelText(/Reason/), 'Ordered for the wrong visit')
    await user.click(within(d).getByRole('button', { name: 'Cancel order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Lab order cancelled for Ananya Rao'))
    expect(bodyOf(sent('post', '/cancel')[0])).toEqual({ reason: 'Ordered for the wrong visit' })
    expect(await screen.findByText('This order was cancelled')).toBeInTheDocument()
    expect(screen.getByText(/Ordered for the wrong visit/)).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual([]))
  })

  it('an admin amends a released result, and the original stays on record', async () => {
    orders = [{ ...resulted(), status: 'released', released_at: '2026-10-05T12:20:00Z', turnaround_minutes: 200 }]
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', ADMIN)

    await user.click(await screen.findByRole('button', { name: 'Amend Haemoglobin result' }))
    const d = await dialog()
    expect(d).toHaveTextContent('recorded as an amendment')
    expect(within(d).getByText('13.5 g/dL')).toBeInTheDocument()

    await user.type(within(d).getByLabelText(/Corrected result/), '13.5')
    await user.type(within(d).getByLabelText(/Reason for the correction/), 'Transcription error')
    await user.click(within(d).getByRole('button', { name: 'Save amendment' }))
    expect(await within(d).findByText('This is the same as the released result')).toBeInTheDocument()
    expect(sent('post', '/amend')).toHaveLength(0)

    await user.clear(within(d).getByLabelText(/Corrected result/))
    await user.type(within(d).getByLabelText(/Corrected result/), '16.9')
    await user.click(within(d).getByRole('button', { name: 'Save amendment' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Haemoglobin amended'))
    const [request] = sent('post', '/amend')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/lab-orders/o1/items/i-HB/amend')
    expect(bodyOf(request)).toEqual({ new_value: '16.9', reason: 'Transcription error' })

    const hb = screen.getAllByRole('listitem').find((c) => within(c).queryByRole('heading', { name: 'Haemoglobin' }))!
    expect(within(hb).getByText('16.9 g/dL')).toBeInTheDocument()
    expect(within(hb).getByText('High')).toBeInTheDocument()
    expect(within(hb).getByText('Amended')).toBeInTheDocument()
    expect(within(hb).getByText('Amendment history')).toBeInTheDocument()
    expect(within(hb).getByText('13.5 (Normal)')).toBeInTheDocument()
    expect(within(hb).getByText('16.9 (High)')).toBeInTheDocument()
    expect(within(hb).getByText('Reason: Transcription error')).toBeInTheDocument()
  })

  it.each([
    ['a doctor', DOCTOR],
    ['a lab technician', LAB_TECH],
    ['a nurse', NURSE],
  ])('offers %s nothing on a released order', async (_who, permissions) => {
    orders = [{ ...resulted(), status: 'released' }]
    renderAt('/laboratory/orders/o1', permissions)

    await screen.findByRole('heading', { level: 1, name: 'Lab order' })
    expect(actionNames()).toEqual([])
  })

  it.each(['ordered', 'collected', 'in_progress', 'results_entered'] as const)(
    'gives a nurse no action on an order that is %s',
    async (status) => {
      orders = [{ ...resulted(), status }]
      renderAt('/laboratory/orders/o1', NURSE)

      await screen.findByRole('heading', { level: 1, name: 'Lab order' })
      expect(actionNames()).toEqual([])
    },
  )

  it('sends one request however many times a step is confirmed', async () => {
    orders = [order('o1', 'ordered', [item(HB)])]
    let release: () => void = () => {}
    intercept = (c) =>
      c.url?.endsWith('/collect') ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c)))) : undefined
    const user = userEvent.setup()
    renderAt('/laboratory/orders/o1', LAB_TECH)
    await user.click(await screen.findByRole('button', { name: /Collect samples/ }))
    const d = await dialog()
    const form = within(d).getByRole('button', { name: 'Mark collected' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Saving…' })).toBeDisabled()
    expect(sent('post', '/collect')).toHaveLength(1)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/collect')).toHaveLength(1)
  })
})

// ── Ordering from a visit ───────────────────────────────────────────────────

describe('ordering lab tests from a visit', () => {
  const labButton = (row: HTMLElement) => within(row).queryByRole('button', { name: /^Order lab tests for/ })
  const rowOf = (name: string) => screen.findByRole('row', { name: new RegExp(name) })

  it('is offered to a doctor on any visit the API accepts, and to no one who cannot order', async () => {
    appointments = [
      appt('a1', 'Booked Patient', 'booked'),
      appt('a2', 'Arrived Patient', 'checked_in'),
      appt('a3', 'Consulting Patient', 'in_progress'),
      appt('a4', 'Done Patient', 'completed'),
      appt('a5', 'Dropped Patient', 'cancelled'),
      appt('a6', 'Absent Patient', 'no_show'),
    ]
    renderAt('/appointments', DOCTOR)

    for (const name of ['Booked Patient', 'Arrived Patient', 'Consulting Patient', 'Done Patient']) {
      expect(labButton(await rowOf(name))).toBeInTheDocument()
    }
    // The API refuses an order on a visit that did not happen.
    expect(labButton(await rowOf('Dropped Patient'))).not.toBeInTheDocument()
    expect(labButton(await rowOf('Absent Patient'))).not.toBeInTheDocument()
  })

  it.each([
    ['a receptionist', RECEPTIONIST],
    ['a nurse', NURSE],
  ])('is not offered to %s', async (_who, permissions) => {
    renderAt('/appointments', permissions)

    expect(labButton(await rowOf('Ananya Rao'))).not.toBeInTheDocument()
  })

  it('places the order for the visit, and the visit\'s invoices are refetched', async () => {
    const user = userEvent.setup()
    renderAt('/appointments', DOCTOR, <PatientInvoices patientId="p-a1" />)
    await user.click(labButton(await rowOf('Ananya Rao'))!)
    const d = await dialog()

    expect(d).toHaveTextContent('Ananya Rao, seen by Dr. Priya Sharma')
    expect(d).toHaveTextContent("added to this visit's draft invoice")
    // Only active tests are asked for, in one request.
    await within(d).findByText('Haemoglobin')
    expect(wire(sent('get', '/tests-catalog')[0])).toEqual({ is_active: true, page_size: 100 })
    expect(within(d).queryByText('ESR (retired)')).not.toBeInTheDocument()

    await user.click(within(d).getByRole('checkbox', { name: /Haemoglobin/ }))
    await user.click(within(d).getByRole('checkbox', { name: /Serum potassium/ }))
    expect(within(d).getByText(/2 tests chosen · 550\.00 at catalog prices/)).toBeInTheDocument()
    await choose(user, within(d).getByRole('combobox', { name: /Priority/ }), 'Urgent')
    await user.type(within(d).getByLabelText(/Notes for the lab/), ' Fasting sample ')
    const invoicesBefore = sent('get', '/invoices').length
    await user.click(within(d).getByRole('button', { name: 'Place order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Lab order placed — 2 tests for Ananya Rao'))
    const [request] = sent('post', '/lab-orders')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/lab-orders')
    // The patient and the doctor are the visit's: sending either is a 422.
    expect(bodyOf(request)).toEqual({
      appointment_id: 'a1',
      test_ids: ['t-HB', 't-K'],
      priority: 'urgent',
      notes: 'Fasting sample',
    })
    // The server billed the tests; the invoice list on screen is asked for again.
    await waitFor(() => expect(sent('get', '/invoices').length).toBeGreaterThan(invoicesBefore))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps a ticked test ticked while the list is filtered', async () => {
    const user = userEvent.setup()
    renderAt('/appointments', DOCTOR)
    await user.click(labButton(await rowOf('Ananya Rao'))!)
    const d = await dialog()
    await user.click(await within(d).findByRole('checkbox', { name: /Haemoglobin/ }))

    await user.type(within(d).getByRole('textbox', { name: 'Filter tests' }), 'potass')
    expect(within(d).queryByRole('checkbox', { name: /Haemoglobin/ })).not.toBeInTheDocument()
    await user.click(within(d).getByRole('checkbox', { name: /Serum potassium/ }))
    await user.click(within(d).getByRole('button', { name: 'Place order' }))

    await waitFor(() => expect(sent('post', '/lab-orders')).toHaveLength(1))
    expect(bodyOf(sent('post', '/lab-orders')[0])).toEqual({ appointment_id: 'a1', test_ids: ['t-HB', 't-K'], priority: 'routine' })
  })

  it('needs at least one test', async () => {
    const user = userEvent.setup()
    renderAt('/appointments', DOCTOR)
    await user.click(labButton(await rowOf('Ananya Rao'))!)
    const d = await dialog()
    await within(d).findByText('Haemoglobin')

    await user.click(within(d).getByRole('button', { name: 'Place order' }))

    expect(await within(d).findByText('Choose at least one test')).toBeInTheDocument()
    expect(sent('post', '/lab-orders')).toHaveLength(0)
  })

  it('shows the API\'s reason when a test is refused', async () => {
    const user = userEvent.setup()
    renderAt('/appointments', DOCTOR)
    await user.click(labButton(await rowOf('Ananya Rao'))!)
    const d = await dialog()
    await user.click(await within(d).findByRole('checkbox', { name: /Haemoglobin/ }))
    // Deactivated by an admin after the list was loaded.
    catalog.find((t) => t.code === 'HB')!.is_active = false
    await user.click(within(d).getByRole('button', { name: 'Place order' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("Lab test 'HB' is inactive and cannot be ordered.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(orders).toHaveLength(0)
  })

  it('offers a retry when the catalog cannot be loaded', async () => {
    intercept = (c) => (c.url === '/tests-catalog' ? fail(500, 'boom') : undefined)
    const user = userEvent.setup()
    renderAt('/appointments', DOCTOR)
    await user.click(labButton(await rowOf('Ananya Rao'))!)
    const d = await dialog()

    expect(await within(d).findByText("The test catalog couldn't be loaded.")).toBeInTheDocument()
    intercept = () => undefined
    await user.click(within(d).getByRole('button', { name: /Retry/ }))
    expect(await within(d).findByText('Haemoglobin')).toBeInTheDocument()
  })

  it('places one order however many times the form is submitted', async () => {
    let release: () => void = () => {}
    intercept = (c) =>
      c.url === '/lab-orders' && c.method === 'post'
        ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c))))
        : undefined
    const user = userEvent.setup()
    renderAt('/appointments', DOCTOR)
    await user.click(labButton(await rowOf('Ananya Rao'))!)
    const d = await dialog()
    await user.click(await within(d).findByRole('checkbox', { name: /Haemoglobin/ }))
    const form = within(d).getByRole('button', { name: 'Place order' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Placing order…' })).toBeDisabled()
    await waitFor(() => expect(sent('post', '/lab-orders')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(orders).toHaveLength(1)
  })
})

// ── Test catalog ────────────────────────────────────────────────────────────

describe('test catalog', () => {
  it('lists the catalog and searches, filters and narrows with the API\'s parameters', async () => {
    const user = userEvent.setup()
    renderAt('/laboratory/catalog', DOCTOR)

    const hb = await screen.findByRole('row', { name: /Haemoglobin/ })
    expect(within(hb).getByText('HB')).toBeInTheDocument()
    expect(within(hb).getByText('g/dL')).toBeInTheDocument()
    expect(within(hb).getByText('250.00')).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /Urine microscopy/ })).getByText('Text result')).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /ESR/ })).getByText('Inactive')).toBeInTheDocument()

    await user.type(screen.getByLabelText('Search by name or exact code…'), 'haem')
    await waitFor(() => expect(wire(sent('get', '/tests-catalog').at(-1)!)).toMatchObject({ q: 'haem', page: 1 }))
    await waitFor(() => expect(screen.queryByRole('row', { name: /Serum potassium/ })).not.toBeInTheDocument())
    await user.clear(screen.getByLabelText('Search by name or exact code…'))

    await choose(user, screen.getByRole('combobox', { name: 'Filter by category' }), 'Haematology')
    await waitFor(() => expect(wire(sent('get', '/tests-catalog').at(-1)!)).toMatchObject({ category: 'Haematology' }))
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    await waitFor(() => expect(wire(sent('get', '/tests-catalog').at(-1)!)).toMatchObject({ category: 'Haematology', is_active: true }))
    await waitFor(() => expect(screen.queryByRole('row', { name: /ESR/ })).not.toBeInTheDocument())
  })

  it('shows add and edit to an admin, and to no one else', async () => {
    renderAt('/laboratory/catalog', LAB_TECH)
    await screen.findByRole('row', { name: /Haemoglobin/ })

    expect(screen.queryByRole('button', { name: /Add test/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Edit/ })).not.toBeInTheDocument()
  })

  it('adds a numeric test with its ranges, exactly as the API takes them', async () => {
    const user = userEvent.setup()
    renderAt('/laboratory/catalog', ADMIN)
    await screen.findByRole('row', { name: /Haemoglobin/ })
    await user.click(screen.getByRole('button', { name: /Add test/ }))
    const d = await dialog()

    await user.type(within(d).getByLabelText(/^Code/), 'crp')
    await user.type(within(d).getByLabelText(/^Name/), 'C-reactive protein')
    await user.type(within(d).getByLabelText(/^Category/), 'Immunology')
    await user.type(within(d).getByLabelText(/^Unit/), 'mg/L')
    await user.clear(within(d).getByLabelText(/^Price/))
    await user.type(within(d).getByLabelText(/^Price/), '450')
    await user.type(within(d).getByLabelText(/^Turnaround/), '6')
    await user.type(within(d).getByLabelText(/^High/), '5')
    await user.type(within(d).getByLabelText(/^Critical high/), '100')
    await user.click(within(d).getByRole('button', { name: 'Add test' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Added C-reactive protein (CRP)'))
    expect(bodyOf(sent('post', '/tests-catalog')[0])).toEqual({
      code: 'CRP',
      name: 'C-reactive protein',
      category: 'Immunology',
      unit: 'mg/L',
      result_type: 'numeric',
      reference_ranges: [
        { sex: 'any', age_min: null, age_max: null, low: null, high: '5', critical_low: null, critical_high: '100' },
      ],
      turnaround_hours: 6,
      price: '450',
    })
    expect(await screen.findByRole('row', { name: /C-reactive protein/ })).toBeInTheDocument()
  })

  it('will not add a numeric test without a range, or a band without a bound', async () => {
    const user = userEvent.setup()
    renderAt('/laboratory/catalog', ADMIN)
    await screen.findByRole('row', { name: /Haemoglobin/ })
    await user.click(screen.getByRole('button', { name: /Add test/ }))
    const d = await dialog()
    await user.type(within(d).getByLabelText(/^Code/), 'NA')
    await user.type(within(d).getByLabelText(/^Name/), 'Sodium')

    await user.click(within(d).getByRole('button', { name: 'Add test' }))
    expect(await within(d).findByText('Give a low or a high bound')).toBeInTheDocument()

    await user.click(within(d).getByRole('button', { name: 'Remove band 1' }))
    await user.click(within(d).getByRole('button', { name: 'Add test' }))
    expect(await within(d).findByText('A numeric test needs at least one reference range')).toBeInTheDocument()
    expect(sent('post', '/tests-catalog')).toHaveLength(0)
  })

  it('sends no ranges for a text test', async () => {
    const user = userEvent.setup()
    renderAt('/laboratory/catalog', ADMIN)
    await screen.findByRole('row', { name: /Haemoglobin/ })
    await user.click(screen.getByRole('button', { name: /Add test/ }))
    const d = await dialog()
    await user.type(within(d).getByLabelText(/^Code/), 'CULT')
    await user.type(within(d).getByLabelText(/^Name/), 'Blood culture')
    await choose(user, within(d).getByRole('combobox', { name: /Result type/ }), 'Text')

    expect(within(d).queryByText('Reference ranges')).not.toBeInTheDocument()
    await user.click(within(d).getByRole('button', { name: 'Add test' }))

    await waitFor(() => expect(sent('post', '/tests-catalog')).toHaveLength(1))
    expect(bodyOf(sent('post', '/tests-catalog')[0])).toMatchObject({ code: 'CULT', result_type: 'text', reference_ranges: [] })
  })

  it('shows the API\'s message for a code already in use', async () => {
    const user = userEvent.setup()
    renderAt('/laboratory/catalog', ADMIN)
    await screen.findByRole('row', { name: /Haemoglobin/ })
    await user.click(screen.getByRole('button', { name: /Add test/ }))
    const d = await dialog()
    await user.type(within(d).getByLabelText(/^Code/), 'hb')
    await user.type(within(d).getByLabelText(/^Name/), 'Haemoglobin again')
    await user.type(within(d).getByLabelText(/^Low/), '1')
    await user.click(within(d).getByRole('button', { name: 'Add test' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("A lab test with code 'HB' already exists.")
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('edits by sending only what changed, never the code or result type', async () => {
    const user = userEvent.setup()
    renderAt('/laboratory/catalog', ADMIN)
    await user.click(await screen.findByRole('button', { name: 'Edit Haemoglobin' }))
    const d = await dialog()

    expect(within(d).getByLabelText(/^Code/)).toBeDisabled()
    expect(within(d).getByRole('combobox', { name: /Result type/ })).toBeDisabled()
    expect(within(d).getByRole('button', { name: 'Save changes' })).toBeDisabled()

    await user.clear(within(d).getByLabelText(/^Price/))
    await user.type(within(d).getByLabelText(/^Price/), '275.00')
    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Haemoglobin'))
    const [request] = sent('patch', '/tests-catalog')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/tests-catalog/t-HB')
    expect(bodyOf(request)).toEqual({ price: '275.00', is_active: false })
    const row = await screen.findByRole('row', { name: /Haemoglobin/ })
    await waitFor(() => expect(within(row).getByText('Inactive')).toBeInTheDocument())
  })
})

// ── Integration ─────────────────────────────────────────────────────────────

describe('laboratory in the rest of the app', () => {
  it('shows a patient\'s lab orders on their record, to those who may read them', async () => {
    orders = [resulted()]
    renderAt('/nowhere', NURSE, <PatientLabOrders patientId="p1" />)

    const section = await screen.findByRole('region', { name: 'Lab orders' })
    expect(await within(section).findByText('HB, K')).toBeInTheDocument()
    expect(within(section).getByText('Critical result')).toBeInTheDocument()
    expect(within(section).getByRole('link', { name: /HB, K/ })).toHaveAttribute('href', '/laboratory/orders/o1')
    expect(wire(sent('get', '/lab-orders')[0])).toEqual({ patient_id: 'p1', page_size: 5 })
  })

  it('shows nothing on the record, and asks nothing, without lab.order.read', async () => {
    renderAt('/nowhere', RECEPTIONIST, <PatientLabOrders patientId="p1" />)

    expect(screen.queryByRole('region', { name: 'Lab orders' })).not.toBeInTheDocument()
    expect(sent('get', '/lab-orders')).toHaveLength(0)
  })

  it('files the lab notifications under Laboratory', () => {
    for (const kind of ['lab.results_released', 'lab.critical_result', 'lab.result_amended']) {
      expect(kindPresentation(kind).category).toBe('Laboratory')
    }
  })
})
