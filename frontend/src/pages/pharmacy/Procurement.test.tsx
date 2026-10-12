import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type {
  Medicine,
  PurchaseOrder,
  PurchaseOrderItem,
  PurchaseOrderStatus,
  ReceiptLineInput,
  Vendor,
} from '@/api/pharmacy'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { formatDate } from '@/lib/format'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import PurchaseOrderDetailPage from './PurchaseOrderDetailPage'
import PurchaseOrdersPage from './PurchaseOrdersPage'
import VendorsPage from './VendorsPage'

/**
 * Vendors and purchase orders, against the merged backend contract
 * (docs/18-API_CONTRACTS.md §9.7, §9.8; `backend/app/api/v1/vendors.py`,
 * `purchase_orders.py`, `services/procurement_service.py`). The real hooks,
 * permission check, `http` wrapper and Axios instance run; only the network
 * adapter is replaced by an in-memory "server" that keeps the vendors, the
 * orders and the stock, and — like the real one — refuses what the contract
 * refuses: a name in use, an inactive vendor or medicine, a step from the
 * wrong status, an expiry that disagrees with a batch already in stock.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → permissions as seeded in backend/app/seeds/seed.py (§9.2).
const HOSPITAL_ADMIN = [
  'pharmacy.medicine.read',
  'pharmacy.medicine.create',
  'pharmacy.medicine.update',
  'pharmacy.batch.read',
  'pharmacy.batch.create',
  'pharmacy.batch.update',
  'pharmacy.prescription.read',
  'pharmacy.prescription.create',
  'pharmacy.dispense.execute',
  'pharmacy.po.read',
  'pharmacy.po.create',
  'pharmacy.po.update',
  'pharmacy.po.receive',
  'pharmacy.vendor.read',
  'pharmacy.vendor.create',
  'pharmacy.vendor.update',
  'patient.read',
  'appointment.read',
  'invoice.read',
  'notification.read.own',
]
const DOCTOR = [
  'pharmacy.medicine.read',
  'pharmacy.prescription.read',
  'pharmacy.prescription.create',
  'patient.read',
  'appointment.read',
  'invoice.read.own',
  'notification.read.own',
]
const PHARMACIST = [
  'pharmacy.medicine.read',
  'pharmacy.batch.read',
  'pharmacy.batch.create',
  'pharmacy.batch.update',
  'pharmacy.prescription.read',
  'pharmacy.dispense.execute',
  'pharmacy.po.read',
  'pharmacy.po.receive',
  'pharmacy.vendor.read',
  'notification.read.own',
]
const INVENTORY_MANAGER = [
  'pharmacy.medicine.read',
  'pharmacy.batch.read',
  'pharmacy.po.read',
  'pharmacy.po.create',
  'pharmacy.po.update',
  'pharmacy.po.receive',
  'pharmacy.vendor.read',
  'pharmacy.vendor.create',
  'pharmacy.vendor.update',
  'notification.read.own',
]
// Neither holds a pharmacy code at all.
const NURSE = ['patient.read', 'appointment.read', 'notification.read.own']
const RECEPTIONIST = ['patient.read', 'appointment.read', 'invoice.read', 'notification.read.own']

// ── The in-memory server ────────────────────────────────────────────────────

/** Money the way the server keeps it: whole hundredths in, a decimal string out. */
const cents = (amount: string) => {
  const [whole, fraction = ''] = amount.split('.')
  return Number(whole) * 100 + Number(fraction.padEnd(2, '0'))
}
const money = (hundredths: number) => (hundredths / 100).toFixed(2)

function vendor(id: string, name: string, extra: Partial<Vendor> = {}): Vendor {
  return {
    id,
    name,
    contact: null,
    address: null,
    tax_id: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    ...extra,
  }
}

function medicine(sku: string, name: string, extra: Partial<Medicine> = {}): Medicine {
  return {
    id: `m-${sku}`,
    sku,
    name,
    generic_name: null,
    strength: null,
    form: null,
    atc_code: null,
    unit_price: '2.50',
    requires_prescription: false,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...extra,
  }
}

function line(m: Medicine, quantity: number, unitPrice: string): PurchaseOrderItem {
  return {
    id: `i-${m.sku}`,
    medicine_id: m.id,
    medicine_sku: m.sku,
    medicine_name: m.name,
    quantity,
    unit_price: unitPrice,
    total: money(cents(unitPrice) * quantity),
  }
}

function order(
  id: string,
  status: PurchaseOrderStatus,
  items: PurchaseOrderItem[],
  extra: Partial<PurchaseOrder> = {},
): PurchaseOrder {
  return {
    id,
    po_number: 'PO-2026-3FA85F64',
    vendor_id: 'v1',
    vendor_name: 'Sanjeevani Pharma Distributors',
    status,
    notes: null,
    ordered_at: status === 'sent' || status === 'received' ? '2026-10-04T09:00:00Z' : null,
    received_at: status === 'received' ? '2026-10-05T06:30:00Z' : null,
    total_amount: money(items.reduce((sum, i) => sum + cents(i.total), 0)),
    created_at: '2026-10-03T08:00:00Z',
    items,
    ...extra,
  }
}

const SANJEEVANI = vendor('v1', 'Sanjeevani Pharma Distributors', {
  contact: 'orders@sanjeevani-pharma.example',
  address: '14 Market Road, Demo City',
  tax_id: '29ABCDE1234F1Z5',
})
const MEDLINE = vendor('v2', 'Medline Wholesale')
const APEX = vendor('v3', 'Apex Remedies', { is_active: false })

const PARA = medicine('PARA-500', 'Paracetamol', { strength: '500 mg', form: 'tablet', unit_price: '2.50' })
const AMOX = medicine('AMOX-500', 'Amoxicillin', { strength: '500 mg', form: 'capsule', unit_price: '8.00' })
const CETZ = medicine('CETZ-10', 'Cetirizine', { strength: '10 mg', form: 'tablet', unit_price: '3.50' })
const RANI = medicine('RANI-150', 'Ranitidine (withdrawn)', { is_active: false })

/** A batch in stock, as far as receiving cares: its number fixes its expiry and its cost. */
interface StockBatch {
  medicine_id: string
  batch_number: string
  expiry_date: string
  quantity: number
  cost_per_unit: string
}

/** The hospital's local date, which is what the server judges an expiry against. */
const TODAY = '2026-10-05'

let vendors: Vendor[]
let medicines: Medicine[]
let orders: PurchaseOrder[]
let stock: StockBatch[]
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

/** A request-validation 422: the message is always the same and `errors` is a LIST. */
const invalid = (field: string, message: string) =>
  fail(422, 'Validation failed.', { error_code: 'VALIDATION_ERROR', errors: [{ field, message }] })

/** A rule a service enforces: the message is the reason and `errors` is an OBJECT holding the list. */
const refused = (field: string, message: string) =>
  fail(422, message, { error_code: 'VALIDATION_ERROR', errors: { errors: [{ field, message }] } })

const wrongStatus = (verb: string, o: PurchaseOrder) =>
  fail(400, `Cannot ${verb} a purchase order that is ${o.status}.`, {
    error_code: 'BUSINESS_RULE_VIOLATION',
    errors: { status: o.status },
  })

const nameTaken = (name: string) =>
  fail(409, `A vendor named '${name}' already exists.`, { error_code: 'RESOURCE_CONFLICT', errors: { name } })

const blankToNull = (value: unknown) => (typeof value === 'string' && value.trim() ? value.trim() : null)

const extraKey = (body: object, allowed: string[]) => Object.keys(body).find((k) => !allowed.includes(k))

/** The string limits both vendor request models share; lengths are measured before trimming. */
function vendorTextError(body: Record<string, unknown>): Outcome | undefined {
  const limits: Record<string, number> = { name: 200, contact: 200, address: 1000, tax_id: 50 }
  for (const [field, max] of Object.entries(limits)) {
    const value = body[field]
    if (typeof value === 'string' && value.length > max) {
      return invalid(field, `String should have at most ${max} characters`)
    }
  }
  if (typeof body.name === 'string') {
    if (body.name === '') return invalid('name', 'String should have at least 1 character')
    if (!body.name.trim()) return invalid('name', 'Value error, Name must not be blank.')
  }
  return undefined
}

let created = 0

function server(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const method = config.method ?? 'get'
  const params = (config.params ?? {}) as Record<string, unknown>

  // An empty query value is not "no filter": it fails to parse.
  const empty = Object.keys(params).find((key) => params[key] === '')
  if (empty) return invalid(`query.${empty}`, 'Input should be a valid value')

  // ── Vendors ──
  if (url === '/vendors' && method === 'get') {
    return pageOf(
      vendors
        .filter((v) => params.is_active === undefined || v.is_active === params.is_active)
        .sort((a, b) => a.name.localeCompare(b.name)),
      config,
    )
  }
  if (url === '/vendors' && method === 'post') {
    const body = bodyOf(config) as Record<string, unknown>
    const extra = extraKey(body, ['name', 'contact', 'address', 'tax_id'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    if (typeof body.name !== 'string') return invalid('name', 'Field required')
    const textError = vendorTextError(body)
    if (textError) return textError
    const name = body.name.trim()
    // Exact and case-sensitive, and an inactive vendor still holds its name.
    if (vendors.some((v) => v.name === name)) return nameTaken(name)
    const added = vendor(`v${vendors.length + 1}`, name, {
      contact: blankToNull(body.contact),
      address: blankToNull(body.address),
      tax_id: blankToNull(body.tax_id),
    })
    vendors.push(added)
    return ok(structuredClone(added), 201)
  }
  const vendorPath = /^\/vendors\/([^/]+)$/.exec(url)
  if (vendorPath && method === 'patch') {
    const body = bodyOf(config) as Record<string, unknown>
    const extra = extraKey(body, ['name', 'contact', 'address', 'tax_id', 'is_active'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    const nulls = ['is_active', 'name'].filter((k) => k in body && body[k] === null)
    if (nulls.length > 0) return invalid('', `Value error, Cannot be null: ${nulls.join(', ')}.`)
    const textError = vendorTextError(body)
    if (textError) return textError
    const v = vendors.find((x) => x.id === vendorPath[1])
    if (!v) return fail(404, 'Vendor not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { vendor_id: vendorPath[1] } })
    if (typeof body.name === 'string') {
      const name = body.name.trim()
      if (name !== v.name && vendors.some((x) => x.name === name)) return nameTaken(name)
      v.name = name
      // An order repeats its vendor's current name; it holds no copy of its own.
      orders.filter((o) => o.vendor_id === v.id).forEach((o) => (o.vendor_name = name))
    }
    for (const field of ['contact', 'address', 'tax_id'] as const) {
      if (field in body) v[field] = blankToNull(body[field])
    }
    if (typeof body.is_active === 'boolean') v.is_active = body.is_active
    return ok(structuredClone(v))
  }

  // ── Medicines (what the picker asks for) ──
  if (url === '/medicines' && method === 'get') {
    const q = typeof params.q === 'string' ? params.q.toLowerCase() : ''
    return pageOf(
      medicines.filter(
        (m) =>
          (!q || m.name.toLowerCase().startsWith(q) || m.sku.toLowerCase() === q) &&
          (params.is_active === undefined || m.is_active === params.is_active),
      ),
      config,
    )
  }

  // ── Purchase orders ──
  if (url === '/purchase-orders' && method === 'get') {
    return pageOf(
      orders.filter(
        (o) =>
          (params.status === undefined || o.status === params.status) &&
          (params.vendor_id === undefined || o.vendor_id === params.vendor_id),
      ),
      config,
    )
  }
  if (url === '/purchase-orders' && method === 'post') {
    const body = bodyOf(config) as { vendor_id?: string; notes?: string | null; items?: { medicine_id: string; quantity: number; unit_price: string }[] }
    const extra = extraKey(body, ['vendor_id', 'notes', 'items'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    if (typeof body.vendor_id !== 'string') return invalid('vendor_id', 'Field required')
    if (!Array.isArray(body.items) || body.items.length < 1) return invalid('items', 'List should have at least 1 item after validation, not 0')
    if (body.items.length > 50) return invalid('items', `List should have at most 50 items after validation, not ${body.items.length}`)
    for (const [i, item] of body.items.entries()) {
      const extraOnLine = extraKey(item, ['medicine_id', 'quantity', 'unit_price'])
      if (extraOnLine) return invalid(`items.${i}.${extraOnLine}`, 'Extra inputs are not permitted')
      if (!Number.isInteger(item.quantity) || item.quantity < 1) return invalid(`items.${i}.quantity`, 'Input should be greater than 0')
      if (item.quantity > 1_000_000) return invalid(`items.${i}.quantity`, 'Input should be less than or equal to 1000000')
      if (typeof item.unit_price !== 'string' || !/^\d+(\.\d{1,2})?$/.test(item.unit_price)) return invalid(`items.${i}.unit_price`, 'Input should be a valid decimal')
    }
    if (new Set(body.items.map((i) => i.medicine_id)).size !== body.items.length) {
      return invalid('items', 'Value error, Each medicine may appear only once in an order.')
    }
    // Service rules: the vendor first, then the lines in order; only the first failure is reported.
    const v = vendors.find((x) => x.id === body.vendor_id)
    if (!v) return refused('vendor_id', 'Vendor not found in this hospital.')
    if (!v.is_active) return refused('vendor_id', `Vendor '${v.name}' is inactive.`)
    const lines: PurchaseOrderItem[] = []
    for (const [i, item] of body.items.entries()) {
      const m = medicines.find((x) => x.id === item.medicine_id)
      if (!m) return refused(`items.${i}.medicine_id`, 'Medicine not found in this hospital.')
      if (!m.is_active) return refused(`items.${i}.medicine_id`, `Medicine '${m.sku}' is inactive and cannot be ordered.`)
      lines.push(line(m, item.quantity, money(cents(item.unit_price))))
    }
    created += 1
    const drafted = order(`new${created}`, 'draft', lines, {
      // Random on the real server; a client cannot know it before the answer.
      po_number: `PO-2026-${(0xc0ffee00 + created).toString(16).toUpperCase()}`,
      vendor_id: v.id,
      vendor_name: v.name,
      notes: blankToNull(body.notes),
      created_at: '2026-10-05T05:00:00Z',
    })
    orders.unshift(drafted)
    return ok(structuredClone(drafted), 201)
  }

  const orderPath = /^\/purchase-orders\/([^/]+)(?:\/(send|cancel|receive))?$/.exec(url)
  if (orderPath) {
    const [, id, step] = orderPath
    const notFound = fail(404, 'Purchase order not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { purchase_order_id: id } })
    const o = orders.find((x) => x.id === id)

    // Request validation runs before the order is even looked up.
    let rows: ReceiptLineInput[] = []
    if (step === 'receive') {
      const body = (bodyOf(config) ?? {}) as { items?: ReceiptLineInput[] }
      const extra = extraKey(body, ['items'])
      if (extra) return invalid(extra, 'Extra inputs are not permitted')
      if (!Array.isArray(body.items) || body.items.length < 1) return invalid('items', 'List should have at least 1 item after validation, not 0')
      if (body.items.length > 50) return invalid('items', `List should have at most 50 items after validation, not ${body.items.length}`)
      rows = body.items.map((r) => ({ ...r, batch_number: String(r.batch_number).trim().toUpperCase() }))
      for (const [i, r] of rows.entries()) {
        const extraOnRow = extraKey(body.items[i], ['po_item_id', 'batch_number', 'expiry_date', 'quantity', 'cost_per_unit'])
        if (extraOnRow) return invalid(`items.${i}.${extraOnRow}`, 'Extra inputs are not permitted')
        // Lengths are measured on the text as sent, before it is trimmed.
        const raw = String(body.items[i].batch_number)
        if (raw.length < 1) return invalid(`items.${i}.batch_number`, 'String should have at least 1 character')
        if (raw.length > 50) return invalid(`items.${i}.batch_number`, 'String should have at most 50 characters')
        if (!/^[A-Z0-9][A-Z0-9_./-]*$/.test(r.batch_number)) {
          return invalid(`items.${i}.batch_number`, 'Value error, Batch number may contain only letters, digits and the characters - _ . /')
        }
        if (!/^\d{4}-\d{2}-\d{2}$/.test(String(r.expiry_date))) return invalid(`items.${i}.expiry_date`, 'Input should be a valid date')
        if (!Number.isInteger(r.quantity) || r.quantity < 1) return invalid(`items.${i}.quantity`, 'Input should be greater than 0')
        if (r.quantity > 1_000_000) return invalid(`items.${i}.quantity`, 'Input should be less than or equal to 1000000')
        if (r.cost_per_unit !== undefined && r.cost_per_unit !== null && !/^\d+(\.\d{1,2})?$/.test(String(r.cost_per_unit))) {
          return invalid(`items.${i}.cost_per_unit`, 'Input should be a valid decimal')
        }
      }
      if (new Set(rows.map((r) => `${r.po_item_id} ${r.batch_number}`)).size !== rows.length) {
        return invalid('items', 'Value error, Each batch may be listed only once per order line.')
      }
    }

    if (!o) return notFound
    if (method === 'get') return ok(structuredClone(o))

    if (step === 'send') {
      if (o.status !== 'draft') return wrongStatus('send', o)
      Object.assign(o, { status: 'sent', ordered_at: '2026-10-05T06:00:00Z' })
    } else if (step === 'cancel') {
      if (o.status !== 'draft' && o.status !== 'sent') return wrongStatus('cancel', o)
      // No reason and no time: the model has neither.
      o.status = 'cancelled'
    } else if (step === 'receive') {
      if (o.status !== 'sent') return wrongStatus('receive', o)
      // The whole receipt is checked before anything is written.
      for (const [i, r] of rows.entries()) {
        const item = o.items.find((x) => x.id === r.po_item_id)
        if (!item) return refused(`items.${i}.po_item_id`, 'Not an item on this order.')
        if (r.expiry_date < TODAY) return refused(`items.${i}.expiry_date`, 'A batch that has already expired cannot be received.')
        const held = stock.find((b) => b.medicine_id === item.medicine_id && b.batch_number === r.batch_number)
        if (held && held.expiry_date !== r.expiry_date) {
          return refused(`items.${i}.expiry_date`, `Batch ${r.batch_number} is already recorded as expiring ${held.expiry_date}.`)
        }
      }
      for (const r of rows) {
        const item = o.items.find((x) => x.id === r.po_item_id)!
        const held = stock.find((b) => b.medicine_id === item.medicine_id && b.batch_number === r.batch_number)
        // A batch already in stock is topped up and keeps its cost.
        if (held) held.quantity += r.quantity
        else {
          stock.push({
            medicine_id: item.medicine_id,
            batch_number: r.batch_number,
            expiry_date: r.expiry_date,
            quantity: r.quantity,
            cost_per_unit: r.cost_per_unit ?? item.unit_price,
          })
        }
      }
      // What arrived is not echoed: the lines still say what was ordered.
      Object.assign(o, { status: 'received', received_at: '2026-10-05T07:00:00Z' })
    }
    return ok(structuredClone(o))
  }

  return pageOf([], config)
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  vendors = [SANJEEVANI, MEDLINE, APEX].map((v) => structuredClone(v))
  medicines = [PARA, AMOX, CETZ, RANI].map((m) => structuredClone(m))
  orders = []
  stock = []
  created = 0
  intercept = () => undefined
  fake = installFakeApi(async (config) => (await intercept(config)) ?? server(config))
})

afterEach(() => {
  fake.restore()
  signOut()
})

function renderAt(path: string, permissions: string[]) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/dashboard" element={<p>Dashboard home</p>} />
          <Route path="/pharmacy/medicines/:medicineId" element={<p>Medicine page</p>} />
          <Route
            path="/pharmacy/vendors"
            element={
              <RequirePermission permission="pharmacy.vendor.read">
                <VendorsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/pharmacy/purchase-orders"
            element={
              <RequirePermission permission="pharmacy.po.read">
                <PurchaseOrdersPage />
              </RequirePermission>
            }
          />
          <Route
            path="/pharmacy/purchase-orders/:orderId"
            element={
              <RequirePermission permission="pharmacy.po.read">
                <PurchaseOrderDetailPage />
              </RequirePermission>
            }
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** Requests to exactly this path (`fake.requests` matches a part, and `/purchase-orders` is part of every step). */
const sent = (method: string, url: string) => fake.sent.filter((c) => c.method === method && c.url === url)
const address = (config: InternalAxiosRequestConfig) => `${config.baseURL}${config.url}`
/** The query as it goes on the wire: a key left undefined is not sent at all. */
const wire = (config: InternalAxiosRequestConfig) => JSON.parse(JSON.stringify(config.params ?? {})) as Record<string, unknown>
const dialog = () => screen.findByRole('dialog')
const header = () => within(document.querySelector('header') as HTMLElement)
const fill = (input: HTMLElement, value: string) => fireEvent.change(input, { target: { value } })
const skeletons = () => document.querySelectorAll('[data-slot="skeleton"]').length
const actionNames = () =>
  screen
    .queryAllByRole('button')
    .map((b) => b.textContent?.trim() ?? '')
    .filter((t) => /^(Mark as sent|Receive|Cancel order)$/.test(t))
const choose = async (user: ReturnType<typeof userEvent.setup>, combobox: HTMLElement, option: string) => {
  await waitFor(() => expect(combobox).toBeEnabled())
  await user.click(combobox)
  await user.click(await screen.findByRole('option', { name: option }))
}
/** Hold the answer to one kind of request until `release()` is called. */
function hold(matches: (config: InternalAxiosRequestConfig) => boolean) {
  let release: () => void = () => {}
  intercept = (c) => (matches(c) ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c)))) : undefined)
  return () => release()
}

const threeLines = () => [line(PARA, 500, '1.20'), line(AMOX, 200, '4.20'), line(CETZ, 100, '1.50')]

// ── Access ──────────────────────────────────────────────────────────────────

describe('procurement access', () => {
  const pages = ['/pharmacy/vendors', '/pharmacy/purchase-orders', '/pharmacy/purchase-orders/o1']

  it.each([
    ['a doctor', DOCTOR],
    ['a nurse', NURSE],
    ['a receptionist', RECEPTIONIST],
  ])('sends %s back to the dashboard from every procurement page, asking the API nothing', async (_who, permissions) => {
    orders = [order('o1', 'sent', threeLines())]
    for (const page of pages) {
      const view = renderAt(page, permissions)
      expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
      view.unmount()
    }
    expect(fake.sent).toHaveLength(0)
  })

  it('offers a pharmacist receiving, and nothing that needs another permission', async () => {
    orders = [order('o1', 'sent', threeLines())]

    let view = renderAt('/pharmacy/vendors', PHARMACIST)
    await screen.findByRole('row', { name: /Sanjeevani Pharma Distributors/ })
    expect(screen.queryByRole('button', { name: /Add vendor/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Edit/ })).not.toBeInTheDocument()
    view.unmount()

    view = renderAt('/pharmacy/purchase-orders', PHARMACIST)
    await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })
    expect(screen.queryByRole('button', { name: /New order/ })).not.toBeInTheDocument()
    view.unmount()

    renderAt('/pharmacy/purchase-orders/o1', PHARMACIST)
    await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })
    expect(actionNames()).toEqual(['Receive'])
    // Nothing was written, and nothing was asked that a pharmacist may not read.
    expect(fake.sent.every((c) => c.method === 'get')).toBe(true)
    expect(fake.sent.map((c) => c.url)).not.toContain('/medicines')
  })

  it.each([
    ['an inventory manager', INVENTORY_MANAGER],
    ['a hospital admin', HOSPITAL_ADMIN],
  ])('offers %s everything', async (_who, permissions) => {
    orders = [order('o1', 'sent', threeLines())]

    let view = renderAt('/pharmacy/vendors', permissions)
    await screen.findByRole('row', { name: /Sanjeevani Pharma Distributors/ })
    expect(screen.getByRole('button', { name: /Add vendor/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit Sanjeevani Pharma Distributors' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit Apex Remedies' })).toBeInTheDocument()
    view.unmount()

    view = renderAt('/pharmacy/purchase-orders', permissions)
    await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })
    expect(screen.getByRole('button', { name: /New order/ })).toBeInTheDocument()
    view.unmount()

    renderAt('/pharmacy/purchase-orders/o1', permissions)
    await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })
    expect(actionNames()).toEqual(['Receive', 'Cancel order'])
  })

  it.each([
    ['an inventory manager', INVENTORY_MANAGER, 'draft', ['Mark as sent', 'Cancel order']],
    ['an inventory manager', INVENTORY_MANAGER, 'sent', ['Receive', 'Cancel order']],
    ['an inventory manager', INVENTORY_MANAGER, 'received', []],
    ['an inventory manager', INVENTORY_MANAGER, 'cancelled', []],
    ['a hospital admin', HOSPITAL_ADMIN, 'draft', ['Mark as sent', 'Cancel order']],
    // A pharmacist can receive, but cannot send or cancel.
    ['a pharmacist', PHARMACIST, 'draft', []],
    ['a pharmacist', PHARMACIST, 'sent', ['Receive']],
    ['a pharmacist', PHARMACIST, 'received', []],
    ['a pharmacist', PHARMACIST, 'cancelled', []],
  ] as const)('offers %s, on an order that is %s, exactly %j', async (_who, permissions, status, expected) => {
    orders = [order('o1', status, threeLines())]
    renderAt('/pharmacy/purchase-orders/o1', [...permissions])

    await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })
    expect(actionNames()).toEqual(expected)
  })

  it('asks for no vendor list and no catalog on behalf of a role that cannot read them', async () => {
    const user = userEvent.setup()
    // A custom role: it may raise orders, but was given neither list.
    renderAt('/pharmacy/purchase-orders', ['pharmacy.po.read', 'pharmacy.po.create'])
    await screen.findByText('No purchase orders yet')
    expect(screen.queryByRole('combobox', { name: 'Filter by vendor' })).not.toBeInTheDocument()

    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    const d = await dialog()
    expect(d).toHaveTextContent("This order can't be drafted from your account")
    // An explanation, not a form: there is nothing to submit, so nothing offers to.
    expect(d.querySelector('form')).toBeNull()
    expect(within(d).queryByRole('button', { name: /Draft order/ })).not.toBeInTheDocument()
    expect(within(d).queryByRole('combobox')).not.toBeInTheDocument()
    expect(within(d).queryByRole('textbox')).not.toBeInTheDocument()

    await user.click(within(d).getByText('Close'))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(sent('get', '/vendors')).toHaveLength(0)
    expect(sent('get', '/medicines')).toHaveLength(0)
    expect(fake.sent.filter((c) => c.method === 'post')).toHaveLength(0)
  })
})

// ── Vendors ─────────────────────────────────────────────────────────────────

describe('vendors', () => {
  it('lists vendors as the API returns them, and offers no search', async () => {
    renderAt('/pharmacy/vendors', PHARMACIST)

    const row = await screen.findByRole('row', { name: /Sanjeevani Pharma Distributors/ })
    expect(within(row).getByText('orders@sanjeevani-pharma.example')).toBeInTheDocument()
    expect(within(row).getByText('14 Market Road, Demo City')).toBeInTheDocument()
    expect(within(row).getByText('29ABCDE1234F1Z5')).toBeInTheDocument()
    expect(within(row).getByText('Active')).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /Apex Remedies/ })).getByText('Inactive')).toBeInTheDocument()
    // Name order is the server's.
    expect(screen.getAllByRole('row').slice(1).map((r) => r.textContent)).toEqual([
      expect.stringContaining('Apex Remedies'),
      expect.stringContaining('Medline Wholesale'),
      expect.stringContaining('Sanjeevani Pharma Distributors'),
    ])
    expect(screen.getByRole('navigation', { name: 'Pharmacy sections' })).toBeInTheDocument()

    const [request] = sent('get', '/vendors')
    expect(address(request)).toBe('/api/v1/vendors')
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    // The API has no search parameter for vendors, so there is no box to type in.
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
  })

  it('filters on is_active, and leaves the parameter out for "all"', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', PHARMACIST)
    await screen.findByRole('row', { name: /Apex Remedies/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Apex Remedies/ })).not.toBeInTheDocument())
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Sanjeevani/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Apex Remedies/ })).toBeInTheDocument()
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active and inactive')
    expect(await screen.findByRole('row', { name: /Sanjeevani/ })).toBeInTheDocument()

    // "All" is the first request, served again from cache: is_active is never sent empty.
    expect(sent('get', '/vendors').map(wire)).toEqual([
      { page: 1, page_size: 25 },
      { is_active: true, page: 1, page_size: 25 },
      { is_active: false, page: 1, page_size: 25 },
    ])
  })

  it("pages through the server's pages", async () => {
    vendors = Array.from({ length: 30 }, (_, n) => vendor(`v${n}`, `Vendor ${String(n).padStart(2, '0')}`))
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', PHARMACIST)
    await screen.findByRole('row', { name: /Vendor 00/ })
    expect(screen.queryByRole('row', { name: /Vendor 29/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() => expect(wire(sent('get', '/vendors').at(-1)!)).toEqual({ page: 2, page_size: 25 }))
    expect(await screen.findByRole('row', { name: /Vendor 29/ })).toBeInTheDocument()
  })

  it('shows skeleton rows while the list loads', async () => {
    const release = hold((c) => c.url === '/vendors')
    renderAt('/pharmacy/vendors', PHARMACIST)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    expect(screen.queryByText('No vendors yet')).not.toBeInTheDocument()
    release()

    expect(await screen.findByRole('row', { name: /Medline Wholesale/ })).toBeInTheDocument()
    expect(skeletons()).toBe(0)
  })

  it('says there are no vendors yet, and offers to add one only to someone who can', async () => {
    vendors = []
    const view = renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    expect(await screen.findByText('No vendors yet')).toBeInTheDocument()
    expect(screen.getByText(/A purchase order needs an active vendor/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Add vendor/ })).toHaveLength(2)
    view.unmount()

    renderAt('/pharmacy/vendors', PHARMACIST)
    expect(await screen.findByText('No vendors yet')).toBeInTheDocument()
    expect(screen.getByText(/Vendors added by someone who manages purchasing/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add vendor/ })).not.toBeInTheDocument()
  })

  it('says so differently when the filter is what leaves the list empty', async () => {
    vendors = [SANJEEVANI, MEDLINE].map((v) => structuredClone(v))
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')

    expect(await screen.findByText('No matching vendors')).toBeInTheDocument()
    expect(screen.getByText('No vendor has been switched off.')).toBeInTheDocument()
    expect(screen.queryByText('No vendors yet')).not.toBeInTheDocument()
  })

  it('offers a retry when the list cannot be loaded, without the server\'s text', async () => {
    intercept = (c) => (c.url === '/vendors' ? fail(500, 'boom: relation "vendors" does not exist') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', PHARMACIST)

    expect(await screen.findByText("Couldn't load vendors")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom|relation/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: /Medline Wholesale/ })).toBeInTheDocument()
  })

  it('adds a vendor with the trimmed fields, and never sends is_active', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })
    await user.click(screen.getByRole('button', { name: /Add vendor/ }))
    const d = await dialog()
    expect(d).toHaveTextContent('switched off later, but not deleted')
    expect(within(d).queryByRole('checkbox')).not.toBeInTheDocument()

    fill(within(d).getByLabelText(/^Name/), '  Zenith Pharma  ')
    fill(within(d).getByLabelText(/^Contact/), ' sales@zenith.example ')
    fill(within(d).getByLabelText(/^Address/), '7 Lake View\nDemo City')
    await user.click(within(d).getByRole('button', { name: 'Add vendor' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Added Zenith Pharma'))
    const [request] = sent('post', '/vendors')
    expect(address(request)).toBe('/api/v1/vendors')
    const body = bodyOf(request)
    expect(body).toEqual({
      name: 'Zenith Pharma',
      contact: 'sales@zenith.example',
      address: '7 Lake View\nDemo City',
      tax_id: null,
    })
    // The request model forbids the key: a new vendor is always active.
    expect(body).not.toHaveProperty('is_active')
    const row = await screen.findByRole('row', { name: /Zenith Pharma/ })
    expect(within(row).getByText('Active')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('refuses a missing name and over-long fields before asking the server', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })
    await user.click(screen.getByRole('button', { name: /Add vendor/ }))
    const d = await dialog()

    fill(within(d).getByLabelText(/^Name/), '   ')
    await user.click(within(d).getByRole('button', { name: 'Add vendor' }))
    expect(await within(d).findByText("Enter the vendor's name")).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Name/)).toBeInvalid()

    fill(within(d).getByLabelText(/^Name/), 'N'.repeat(201))
    fill(within(d).getByLabelText(/^Contact/), 'C'.repeat(201))
    fill(within(d).getByLabelText(/^Address/), 'A'.repeat(1001))
    fill(within(d).getByLabelText(/^Tax ID/), 'T'.repeat(51))
    await user.click(within(d).getByRole('button', { name: 'Add vendor' }))

    expect(await within(d).findByText('Keep the name to 200 characters or fewer')).toBeInTheDocument()
    expect(within(d).getByText('Keep the contact to 200 characters or fewer')).toBeInTheDocument()
    expect(within(d).getByText('Keep the address to 1,000 characters or fewer')).toBeInTheDocument()
    expect(within(d).getByText('Keep the tax ID to 50 characters or fewer')).toBeInTheDocument()
    expect(sent('post', '/vendors')).toHaveLength(0)
  })

  it('accepts a name of exactly 200 characters once the spaces around it are trimmed', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })
    await user.click(screen.getByRole('button', { name: /Add vendor/ }))
    const d = await dialog()

    // The server measures the raw text, so the padding would be a 422 if it were sent.
    fill(within(d).getByLabelText(/^Name/), `  ${'N'.repeat(200)}  `)
    await user.click(within(d).getByRole('button', { name: 'Add vendor' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(sent('post', '/vendors')[0])).toMatchObject({ name: 'N'.repeat(200) })
  })

  it("shows the API's message for a name already in use, even by an inactive vendor", async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })
    await user.click(screen.getByRole('button', { name: /Add vendor/ }))
    const d = await dialog()

    fill(within(d).getByLabelText(/^Name/), 'Apex Remedies')
    await user.click(within(d).getByRole('button', { name: 'Add vendor' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("A vendor named 'Apex Remedies' already exists.")
    expect(toastError).toHaveBeenCalledWith("A vendor named 'Apex Remedies' already exists.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(vendors).toHaveLength(3)
    // The dialog stays open with what was typed, so the name can be corrected.
    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Apex Remedies')
  })

  it('puts a request-validation 422 under the field it names, without the "Value error" prefix', async () => {
    intercept = (c) =>
      c.url === '/vendors' && c.method === 'post'
        ? fail(422, 'Validation failed.', { errors: [{ field: 'name', message: 'Value error, Name must not be blank.' }] })
        : undefined
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })
    await user.click(screen.getByRole('button', { name: /Add vendor/ }))
    const d = await dialog()
    fill(within(d).getByLabelText(/^Name/), 'Zenith Pharma')
    await user.click(within(d).getByRole('button', { name: 'Add vendor' }))

    await waitFor(() => expect(within(d).getByLabelText(/^Name/)).toBeInvalid())
    expect(within(d).getByLabelText(/^Name/)).toHaveAccessibleDescription('Name must not be blank.')
    expect(d).not.toHaveTextContent('Value error')
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
  })

  it.each([
    [403, 'Permission denied. Required: pharmacy.vendor.create.', 'Permission denied. Required: pharmacy.vendor.create.'],
    [500, 'boom: connection reset', "Couldn't add the vendor. Please try again."],
  ])('on a %i shows what the user can act on, never raw server text', async (status, serverMessage, shown) => {
    intercept = (c) => (c.url === '/vendors' && c.method === 'post' ? fail(status, serverMessage) : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })
    await user.click(screen.getByRole('button', { name: /Add vendor/ }))
    const d = await dialog()
    fill(within(d).getByLabelText(/^Name/), 'Zenith Pharma')
    await user.click(within(d).getByRole('button', { name: 'Add vendor' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(document.body.textContent).not.toMatch(/boom/)
  })

  it('adds one vendor however many times the form is submitted', async () => {
    const release = hold((c) => c.url === '/vendors' && c.method === 'post')
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Medline Wholesale/ })
    await user.click(screen.getByRole('button', { name: /Add vendor/ }))
    const d = await dialog()
    fill(within(d).getByLabelText(/^Name/), 'Zenith Pharma')
    const form = within(d).getByRole('button', { name: 'Add vendor' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Adding…' })).toBeDisabled()
    expect(sent('post', '/vendors')).toHaveLength(1)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/vendors')).toHaveLength(1)
    expect(vendors).toHaveLength(4)
  })

  it('edits by sending only what changed, a cleared field as null', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: 'Edit Sanjeevani Pharma Distributors' }))
    const d = await dialog()

    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Sanjeevani Pharma Distributors')
    expect(within(d).getByRole('checkbox', { name: /Active — can be ordered from/ })).toBeChecked()
    expect(d).toHaveTextContent('Switching a vendor off only stops new orders')
    // Nothing to save until something is changed.
    expect(within(d).getByRole('button', { name: 'Save changes' })).toBeDisabled()

    fill(within(d).getByLabelText(/^Contact/), '')
    fill(within(d).getByLabelText(/^Tax ID/), ' 29ZZZZZ9999Z1Z9 ')
    await user.click(within(d).getByRole('checkbox', { name: /Active — can be ordered from/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Sanjeevani Pharma Distributors'))
    const [request] = sent('patch', '/vendors/v1')
    expect(address(request)).toBe('/api/v1/vendors/v1')
    // The name and the address were not touched, so they are not sent.
    expect(bodyOf(request)).toEqual({ contact: null, tax_id: '29ZZZZZ9999Z1Z9', is_active: false })
    const row = await screen.findByRole('row', { name: /Sanjeevani Pharma Distributors/ })
    await waitFor(() => expect(within(row).getByText('Inactive')).toBeInTheDocument())
    expect(within(row).queryByText('orders@sanjeevani-pharma.example')).not.toBeInTheDocument()
  })

  it('switches an inactive vendor back on with is_active alone', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: 'Edit Apex Remedies' }))
    const d = await dialog()
    expect(within(d).getByRole('checkbox', { name: /Active/ })).not.toBeChecked()

    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Apex Remedies'))
    expect(bodyOf(sent('patch', '/vendors/v3')[0])).toEqual({ is_active: true })
  })

  it('sends nothing when the only change disappears on trimming', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: 'Edit Medline Wholesale' }))
    const d = await dialog()

    fill(within(d).getByLabelText(/^Name/), 'Medline Wholesale  ')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Nothing to save — the vendor is unchanged'))
    expect(fake.sent.filter((c) => c.method === 'patch')).toHaveLength(0)
  })

  it("shows the API's message when a rename collides, and when the vendor has gone", async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: 'Edit Medline Wholesale' }))
    const d = await dialog()

    fill(within(d).getByLabelText(/^Name/), 'Sanjeevani Pharma Distributors')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))
    expect(await within(d).findByRole('alert')).toHaveTextContent(
      "A vendor named 'Sanjeevani Pharma Distributors' already exists.",
    )
    expect(bodyOf(sent('patch', '/vendors/v2')[0])).toEqual({ name: 'Sanjeevani Pharma Distributors' })

    vendors = vendors.filter((v) => v.id !== 'v2')
    fill(within(d).getByLabelText(/^Name/), 'Medline Wholesale Ltd')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(toastError).toHaveBeenLastCalledWith('Vendor not found.'))
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it.each([
    [403, 'Permission denied. Required: pharmacy.vendor.update.', 'Permission denied. Required: pharmacy.vendor.update.'],
    [500, 'boom: could not serialize access', "Couldn't save the vendor. Please try again."],
  ])('on a %i from an edit shows what the user can act on, never raw server text', async (status, serverMessage, shown) => {
    intercept = (c) => (c.url === '/vendors/v2' && c.method === 'patch' ? fail(status, serverMessage) : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: 'Edit Medline Wholesale' }))
    const d = await dialog()
    fill(within(d).getByLabelText(/^Name/), 'Medline Wholesale Ltd')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/boom/)
    expect(sent('patch', '/vendors/v2')).toHaveLength(1)
    // Nothing was saved, and the dialog keeps what was typed for another go.
    expect(vendors.find((v) => v.id === 'v2')!.name).toBe('Medline Wholesale')
    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Medline Wholesale Ltd')
  })

  it('keeps a long, unbroken vendor name out of the dialog title, and lets it wrap', async () => {
    const long = 'V'.repeat(200)
    vendors = [vendor('v9', long)]
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: `Edit ${long}` }))
    const d = await dialog()

    // A dialog title does not break; the description can.
    expect(within(d).getByRole('heading')).toHaveTextContent(/^Edit vendor$/)
    expect(within(d).getByText(long)).toHaveClass('[overflow-wrap:anywhere]')
    expect(d).toHaveAccessibleDescription(new RegExp(`^Editing ${long}\\.`))
  })

  it('shows a whole-body 422, which names no field, above the form', async () => {
    intercept = (c) =>
      c.method === 'patch'
        ? fail(422, 'Validation failed.', { errors: [{ field: '', message: 'Value error, Cannot be null: name.' }] })
        : undefined
    const user = userEvent.setup()
    renderAt('/pharmacy/vendors', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: 'Edit Medline Wholesale' }))
    const d = await dialog()
    fill(within(d).getByLabelText(/^Name/), 'Medline Wholesale Ltd')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Cannot be null: name.')
    // The form itself never sends a null name: the body it sent is the rename.
    expect(bodyOf(sent('patch', '/vendors/v2')[0])).toEqual({ name: 'Medline Wholesale Ltd' })
  })
})

// ── Purchase order list ─────────────────────────────────────────────────────

describe('purchase order list', () => {
  const two = () => [
    order('o2', 'sent', threeLines(), { po_number: 'PO-2026-9BD41C07', created_at: '2026-10-04T08:00:00Z' }),
    order('o1', 'draft', [line(PARA, 10, '1.00')], { vendor_id: 'v2', vendor_name: 'Medline Wholesale' }),
  ]

  it('lists orders as the API returns them, with the server\'s total', async () => {
    orders = two()
    // Not what the lines add up to: the figure shown is the one the server sent.
    orders[0].total_amount = '1590.01'
    renderAt('/pharmacy/purchase-orders', PHARMACIST)

    const first = await screen.findByRole('row', { name: /PO-2026-9BD41C07/ })
    expect(within(first).getByRole('link', { name: 'PO-2026-9BD41C07' })).toHaveAttribute('href', '/pharmacy/purchase-orders/o2')
    expect(within(first).getByText('Sanjeevani Pharma Distributors')).toBeInTheDocument()
    expect(within(first).getByText('3 lines')).toBeInTheDocument()
    expect(within(first).getByText('Paracetamol, Amoxicillin +1 more')).toBeInTheDocument()
    expect(within(first).getByText('1,590.01')).toBeInTheDocument()
    expect(within(first).getByText('Sent')).toBeInTheDocument()
    expect(within(first).getByText(formatDate('2026-10-04T08:00:00Z'))).toBeInTheDocument()
    expect(within(first).getByRole('link', { name: 'Open purchase order PO-2026-9BD41C07' })).toHaveAttribute(
      'href',
      '/pharmacy/purchase-orders/o2',
    )
    const second = screen.getByRole('row', { name: /PO-2026-3FA85F64/ })
    expect(within(second).getByText('1 line')).toBeInTheDocument()
    expect(within(second).getByText('Paracetamol')).toBeInTheDocument()
    expect(within(second).getByText('10.00')).toBeInTheDocument()
    expect(within(second).getByText('Draft')).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Pharmacy sections' })).toBeInTheDocument()

    const [request] = sent('get', '/purchase-orders')
    expect(address(request)).toBe('/api/v1/purchase-orders')
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    // The vendor filter's own request: every vendor, active or not.
    expect(sent('get', '/vendors').map(wire)).toEqual([{ page: 1, page_size: 100 }])
  })

  it('filters by status and vendor with the API\'s own names, leaving "all" out', async () => {
    orders = two()
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', PHARMACIST)
    await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Sent')
    await waitFor(() =>
      expect(wire(sent('get', '/purchase-orders').at(-1)!)).toEqual({ status: 'sent', page: 1, page_size: 25 }),
    )
    await waitFor(() => expect(screen.queryByRole('row', { name: /PO-2026-3FA85F64/ })).not.toBeInTheDocument())

    await choose(user, screen.getByRole('combobox', { name: 'Filter by vendor' }), 'Medline Wholesale')
    await waitFor(() =>
      expect(wire(sent('get', '/purchase-orders').at(-1)!)).toEqual({
        status: 'sent',
        vendor_id: 'v2',
        page: 1,
        page_size: 25,
      }),
    )
    expect(await screen.findByText('No matching purchase orders')).toBeInTheDocument()

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'All statuses')
    await waitFor(() =>
      expect(wire(sent('get', '/purchase-orders').at(-1)!)).toEqual({ vendor_id: 'v2', page: 1, page_size: 25 }),
    )
    expect(await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })).toBeInTheDocument()
    // `order_status` is the server's Python name; on the wire it would be silently ignored.
    expect(sent('get', '/purchase-orders').some((c) => 'order_status' in wire(c))).toBe(false)
  })

  it('offers every vendor in the filter, not only the first hundred', async () => {
    orders = two()
    // One page of vendors is 100 at most, and the API has no vendor search.
    vendors = Array.from({ length: 130 }, (_, n) => vendor(`v${n}`, `Vendor ${String(n).padStart(3, '0')}`))
    orders[1].vendor_id = 'v129'
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', PHARMACIST)
    await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by vendor' }), 'Vendor 129')

    expect(sent('get', '/vendors').map(wire)).toEqual([
      { page: 1, page_size: 100 },
      { page: 2, page_size: 100 },
    ])
    await waitFor(() =>
      expect(wire(sent('get', '/purchase-orders').at(-1)!)).toEqual({ vendor_id: 'v129', page: 1, page_size: 25 }),
    )
    await waitFor(() => expect(screen.queryByRole('row', { name: /PO-2026-9BD41C07/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /PO-2026-3FA85F64/ })).toBeInTheDocument()
  })

  it('says so, and offers a retry, when the vendors for the filter cannot be loaded', async () => {
    orders = two()
    intercept = (c) => (c.url === '/vendors' ? fail(500, 'boom: vendors unavailable') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', PHARMACIST)
    // The orders themselves are unaffected.
    await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })

    const retry = await screen.findByRole('button', { name: /Vendors didn’t load — retry/ })
    expect(document.body.textContent).not.toMatch(/boom/)
    intercept = () => undefined
    await user.click(retry)

    await waitFor(() => expect(screen.queryByRole('button', { name: /didn’t load/ })).not.toBeInTheDocument())
    await choose(user, screen.getByRole('combobox', { name: 'Filter by vendor' }), 'Medline Wholesale')
    await waitFor(() => expect(screen.queryByRole('row', { name: /PO-2026-9BD41C07/ })).not.toBeInTheDocument())
    expect(sent('get', '/vendors')).toHaveLength(2)
  })

  it("pages through the server's pages", async () => {
    orders = Array.from({ length: 30 }, (_, n) =>
      order(`o${n}`, 'draft', [line(PARA, 10, '1.00')], { po_number: `PO-2026-${(0xab000000 + n).toString(16).toUpperCase()}` }),
    )
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', PHARMACIST)
    await screen.findByRole('row', { name: /PO-2026-AB000000/ })

    await user.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() => expect(wire(sent('get', '/purchase-orders').at(-1)!)).toEqual({ page: 2, page_size: 25 }))
    expect(await screen.findByRole('row', { name: /PO-2026-AB00001D/ })).toBeInTheDocument()
  })

  it('shows skeleton rows while the list loads', async () => {
    orders = two()
    const release = hold((c) => c.url === '/purchase-orders')
    renderAt('/pharmacy/purchase-orders', PHARMACIST)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    expect(screen.queryByText('No purchase orders yet')).not.toBeInTheDocument()
    release()

    expect(await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })).toBeInTheDocument()
    expect(skeletons()).toBe(0)
  })

  it('explains itself when no order has ever been drafted', async () => {
    const view = renderAt('/pharmacy/purchase-orders', INVENTORY_MANAGER)
    expect(await screen.findByText('No purchase orders yet')).toBeInTheDocument()
    expect(screen.getByText(/receiving is what adds the medicines to stock/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /New order/ })).toHaveLength(2)
    view.unmount()

    // A pharmacist cannot draft one, and is not told to.
    renderAt('/pharmacy/purchase-orders', PHARMACIST)
    expect(await screen.findByText('No purchase orders yet')).toBeInTheDocument()
    expect(screen.getByText(/drafted by someone who manages purchasing/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New order/ })).not.toBeInTheDocument()
  })

  it('offers a retry when the list cannot be loaded, without the server\'s text', async () => {
    intercept = (c) => (c.url === '/purchase-orders' ? fail(500, 'boom: timeout') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', PHARMACIST)

    expect(await screen.findByText("Couldn't load purchase orders")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    orders = two()
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: /PO-2026-3FA85F64/ })).toBeInTheDocument()
  })
})

// ── Drafting an order ───────────────────────────────────────────────────────

describe('drafting a purchase order', () => {
  const lineGroup = (d: HTMLElement, n: number) => within(d).getByRole('group', { name: `Line ${n}` })
  const pick = async (user: ReturnType<typeof userEvent.setup>, group: HTMLElement, name: RegExp) =>
    user.click(await within(group).findByRole('button', { name }))

  /** Open the dialog and fill one complete, valid line. */
  async function startOrder(user: ReturnType<typeof userEvent.setup>) {
    renderAt('/pharmacy/purchase-orders', INVENTORY_MANAGER)
    await screen.findByText('No purchase orders yet')
    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    const d = await dialog()
    await choose(user, within(d).getByRole('combobox', { name: /Vendor/ }), 'Sanjeevani Pharma Distributors')
    await pick(user, lineGroup(d, 1), /^Paracetamol/)
    fill(within(lineGroup(d, 1)).getByLabelText(/^Quantity/), '500')
    fill(within(lineGroup(d, 1)).getByLabelText(/^Purchase price per unit/), '1.20')
    return d
  }

  it('drafts the order with exactly the body the API takes, and opens it', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', INVENTORY_MANAGER)
    await screen.findByText('No purchase orders yet')
    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    const d = await dialog()
    expect(d).toHaveTextContent('A draft cannot be edited afterwards')

    // Only active vendors are asked for, and only they are offered.
    const vendorSelect = within(d).getByRole('combobox', { name: /Vendor/ })
    await waitFor(() => expect(vendorSelect).toBeEnabled())
    expect(sent('get', '/vendors').map(wire)).toContainEqual({ is_active: true, page: 1, page_size: 100 })
    await user.click(vendorSelect)
    expect(await screen.findByRole('option', { name: 'Medline Wholesale' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Apex Remedies' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Sanjeevani Pharma Distributors' }))

    // An order starts with one line, which cannot be removed.
    expect(within(d).queryByRole('button', { name: 'Remove line 1' })).not.toBeInTheDocument()
    // The catalog asked for is the active one; a withdrawn medicine is not offered.
    await pick(user, lineGroup(d, 1), /^Paracetamol/)
    expect(address(sent('get', '/medicines')[0])).toBe('/api/v1/medicines')
    expect(sent('get', '/medicines').map(wire)).toContainEqual({ is_active: true, page: 1, page_size: 8 })
    expect(within(d).queryByText(/Ranitidine/)).not.toBeInTheDocument()
    // The purchase price is the buyer's to enter: the selling price (2.50) is not copied in.
    expect(within(lineGroup(d, 1)).getByLabelText(/^Purchase price per unit/)).toHaveValue('')
    fill(within(lineGroup(d, 1)).getByLabelText(/^Quantity/), '500')
    fill(within(lineGroup(d, 1)).getByLabelText(/^Purchase price per unit/), '1.20')

    await user.click(within(d).getByRole('button', { name: /Add a medicine/ }))
    await pick(user, lineGroup(d, 2), /^Amoxicillin/)
    fill(within(lineGroup(d, 2)).getByLabelText(/^Quantity/), ' 200 ')
    fill(within(lineGroup(d, 2)).getByLabelText(/^Purchase price per unit/), '4.2')
    fill(within(d).getByLabelText(/^Notes/), '  Deliver before noon.  ')
    expect(within(d).getByText(/^Estimated total 1,440\.00 — a preview/)).toBeInTheDocument()

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    // The number is the server's: nothing on the client could have produced it.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Drafted PO-2026-C0FFEE01'))
    const [request] = sent('post', '/purchase-orders')
    expect(address(request)).toBe('/api/v1/purchase-orders')
    expect(bodyOf(request)).toEqual({
      vendor_id: 'v1',
      notes: 'Deliver before noon.',
      items: [
        { medicine_id: 'm-PARA-500', quantity: 500, unit_price: '1.20' },
        { medicine_id: 'm-AMOX-500', quantity: 200, unit_price: '4.2' },
      ],
    })
    // The new order's own page, with the server's figures.
    expect(await screen.findByRole('heading', { level: 1, name: 'PO-2026-C0FFEE01' })).toBeInTheDocument()
    expect(header().getByText('Draft')).toBeInTheDocument()
    expect(screen.getAllByText('1,440.00').length).toBeGreaterThan(0)
    expect(within(screen.getByRole('row', { name: /Amoxicillin/ })).getByText('840.00')).toBeInTheDocument()
  })

  it('can order from a vendor past the first hundred', async () => {
    vendors = [
      ...Array.from({ length: 130 }, (_, n) => vendor(`v${n}`, `Vendor ${String(n).padStart(3, '0')}`)),
      vendor('off', 'Vendor 999 (switched off)', { is_active: false }),
    ]
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', ['pharmacy.po.read', 'pharmacy.po.create', 'pharmacy.vendor.read', 'pharmacy.medicine.read'])
    await screen.findByText('No purchase orders yet')
    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    const d = await dialog()

    await choose(user, within(d).getByRole('combobox', { name: /Vendor/ }), 'Vendor 129')
    // Both pages of the active vendors were asked for, each with the API's own page size.
    expect(sent('get', '/vendors').map(wire).filter((q) => q.is_active === true)).toEqual([
      { is_active: true, page: 1, page_size: 100 },
      { is_active: true, page: 2, page_size: 100 },
    ])
    expect(d).not.toHaveTextContent(/first 100/)
    await pick(user, lineGroup(d, 1), /^Paracetamol/)
    fill(within(lineGroup(d, 1)).getByLabelText(/^Quantity/), '10')
    fill(within(lineGroup(d, 1)).getByLabelText(/^Purchase price per unit/), '1.00')
    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(sent('post', '/purchase-orders')[0])).toEqual({
      vendor_id: 'v129',
      items: [{ medicine_id: 'm-PARA-500', quantity: 10, unit_price: '1.00' }],
    })
  })

  it('says when only some vendors loaded, and fetches the rest on a retry', async () => {
    vendors = Array.from({ length: 130 }, (_, n) => vendor(`v${n}`, `Vendor ${String(n).padStart(3, '0')}`))
    intercept = (c) => (c.url === '/vendors' && c.params?.page === 2 ? fail(500, 'boom: page two') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', INVENTORY_MANAGER)
    await screen.findByText('No purchase orders yet')
    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    const d = await dialog()

    expect(await within(d).findByText('Not every vendor could be loaded: some are missing from this list.')).toBeInTheDocument()
    expect(d).not.toHaveTextContent(/boom/)
    intercept = () => undefined
    await user.click(within(d).getByRole('button', { name: /Retry/ }))

    await waitFor(() => expect(within(d).queryByText(/Not every vendor could be loaded/)).not.toBeInTheDocument())
    await choose(user, within(d).getByRole('combobox', { name: /Vendor/ }), 'Vendor 129')
  })

  it('announces through one live region, the estimated total', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    await user.click(within(d).getByRole('button', { name: /Add a medicine/ }))

    const live = d.querySelectorAll('[aria-live]')
    expect(live).toHaveLength(1)
    expect(live[0]).toHaveTextContent(/^Estimated total 600\.00/)
  })

  it('leaves a blank note out of the body', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    fill(within(d).getByLabelText(/^Notes/), '   ')

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(sent('post', '/purchase-orders')[0])).toEqual({
      vendor_id: 'v1',
      items: [{ medicine_id: 'm-PARA-500', quantity: 500, unit_price: '1.20' }],
    })
  })

  it('accepts a price of zero', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    fill(within(lineGroup(d, 1)).getByLabelText(/^Purchase price per unit/), '0')

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(sent('post', '/purchase-orders')[0])).toMatchObject({
      items: [{ medicine_id: 'm-PARA-500', quantity: 500, unit_price: '0' }],
    })
  })

  it('refuses an incomplete or malformed order before asking the server', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders', INVENTORY_MANAGER)
    await screen.findByText('No purchase orders yet')
    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    const d = await dialog()
    await waitFor(() => expect(within(d).getByRole('combobox', { name: /Vendor/ })).toBeEnabled())

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))
    expect(await within(d).findByText('Choose a vendor')).toBeInTheDocument()
    expect(within(d).getByText('Choose a medicine')).toBeInTheDocument()
    expect(within(d).getByText('Whole units, 1–1,000,000')).toBeInTheDocument()
    expect(within(d).getByText('Enter an amount such as 1.20')).toBeInTheDocument()

    const quantity = within(lineGroup(d, 1)).getByLabelText(/^Quantity/)
    const price = within(lineGroup(d, 1)).getByLabelText(/^Purchase price per unit/)
    for (const bad of ['0', '1.5', '-3', '1000001', 'ten']) {
      fill(quantity, bad)
      await waitFor(() => expect(quantity).toBeInvalid())
    }
    fill(quantity, '1000000')
    await waitFor(() => expect(quantity).toBeValid())
    for (const bad of ['1.234', '-1', '1,20', 'free']) {
      fill(price, bad)
      await waitFor(() => expect(price).toBeInvalid())
    }
    // A line whose total the order could not hold: refused here, because the server answers it with a 500.
    fill(price, '99999999.99')
    await user.click(within(d).getByRole('button', { name: 'Draft order' }))
    expect(await within(d).findByText("This line's total is more than can be recorded")).toBeInTheDocument()

    expect(sent('post', '/purchase-orders')).toHaveLength(0)
  })

  it('takes a medicine once: a second line cannot pick it again', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)

    await user.click(within(d).getByRole('button', { name: /Add a medicine/ }))
    const again = await within(lineGroup(d, 2)).findByRole('button', { name: /^Paracetamol/ })
    expect(again).toBeDisabled()
    expect(again).toHaveTextContent('already added')
    expect(within(lineGroup(d, 2)).getByRole('button', { name: /^Amoxicillin/ })).toBeEnabled()

    // Both lines can go now, and removing one leaves the other without a remove button.
    expect(within(d).getByRole('button', { name: 'Remove line 1' })).toBeInTheDocument()
    await user.click(within(d).getByRole('button', { name: 'Remove line 2' }))
    expect(within(d).queryByRole('button', { name: /^Remove line/ })).not.toBeInTheDocument()
    expect(within(lineGroup(d, 1)).getByText('PARA-500')).toBeInTheDocument()
  })

  it('stops at fifty lines, which is all an order can hold', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    const add = within(d).getByRole('button', { name: /Add a medicine/ })
    expect(within(d).queryByText('An order holds at most 50 lines.')).not.toBeInTheDocument()

    for (let n = 1; n < 50; n += 1) fireEvent.click(add)

    expect(within(d).getAllByRole('group', { name: /^Line \d+$/ })).toHaveLength(50)
    expect(add).toBeDisabled()
    expect(within(d).getByText('An order holds at most 50 lines.')).toBeInTheDocument()
    // The preview counts only the line that is filled in.
    expect(within(d).getByText(/^Estimated total 600\.00 for the 1 line filled in so far — a preview/)).toBeInTheDocument()
  })

  it('shows a duplicate the server finds as a refusal of the whole list', async () => {
    intercept = (c) =>
      c.url === '/purchase-orders' && c.method === 'post'
        ? fail(422, 'Validation failed.', {
            errors: [{ field: 'items', message: 'Value error, Each medicine may appear only once in an order.' }],
          })
        : undefined
    const user = userEvent.setup()
    const d = await startOrder(user)

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    expect(await within(d).findByText('Each medicine may appear only once in an order.')).toBeInTheDocument()
    expect(d).not.toHaveTextContent('Value error')
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('puts the refusal of an inactive vendor under the vendor field', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    // Switched off by someone else after the list was loaded.
    vendors.find((v) => v.id === 'v1')!.is_active = false

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    const vendorSelect = within(d).getByRole('combobox', { name: /Vendor/ })
    await waitFor(() => expect(vendorSelect).toBeInvalid())
    expect(vendorSelect).toHaveAccessibleDescription("Vendor 'Sanjeevani Pharma Distributors' is inactive.")
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(orders).toHaveLength(0)
    // Still open, with the line as typed.
    expect(within(lineGroup(d, 1)).getByLabelText(/^Quantity/)).toHaveValue('500')
  })

  it('puts the refusal of an inactive medicine under that line', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    await user.click(within(d).getByRole('button', { name: /Add a medicine/ }))
    await pick(user, lineGroup(d, 2), /^Amoxicillin/)
    fill(within(lineGroup(d, 2)).getByLabelText(/^Quantity/), '200')
    fill(within(lineGroup(d, 2)).getByLabelText(/^Purchase price per unit/), '4.20')
    medicines.find((m) => m.sku === 'AMOX-500')!.is_active = false

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    // The server names `items.1.medicine_id`: the second line, and only it.
    expect(
      await within(lineGroup(d, 2)).findByText("Medicine 'AMOX-500' is inactive and cannot be ordered."),
    ).toBeInTheDocument()
    expect(within(lineGroup(d, 1)).queryByRole('alert')).not.toBeInTheDocument()
    expect(orders).toHaveLength(0)
    // Focus goes to the refused line, which on a long order may have been out of view.
    expect(within(lineGroup(d, 2)).getByRole('button', { name: 'Change medicine (Amoxicillin)' })).toHaveFocus()
  })

  it('takes focus to a line left without a medicine', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    await user.click(within(d).getByRole('button', { name: /Add a medicine/ }))
    fill(within(lineGroup(d, 2)).getByLabelText(/^Quantity/), '200')
    fill(within(lineGroup(d, 2)).getByLabelText(/^Purchase price per unit/), '4.20')

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    expect(await within(lineGroup(d, 2)).findByText('Choose a medicine')).toBeInTheDocument()
    expect(within(lineGroup(d, 2)).getByLabelText(/^Medicine/)).toHaveFocus()
    expect(sent('post', '/purchase-orders')).toHaveLength(0)
  })

  it.each([
    [403, 'Permission denied. Required: pharmacy.po.create.', 'Permission denied. Required: pharmacy.po.create.'],
    // Not "couldn't draft": after a 500 nobody knows, and a retry could make a second draft.
    [500, 'boom: deadlock detected', "Couldn't confirm that the order was drafted. Check the purchase order list before trying again, so it isn't drafted twice."],
  ])('on a %i shows what the user can act on, never raw server text', async (status, serverMessage, shown) => {
    intercept = (c) => (c.url === '/purchase-orders' && c.method === 'post' ? fail(status, serverMessage) : undefined)
    const user = userEvent.setup()
    const d = await startOrder(user)

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(document.body.textContent).not.toMatch(/boom/)
  })

  it('drafts one order however many times the form is submitted', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    // The API would make a second draft from a second request: it has no key to tell them apart.
    const release = hold((c) => c.url === '/purchase-orders' && c.method === 'post')
    const form = within(d).getByRole('button', { name: 'Draft order' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Drafting…' })).toBeDisabled()
    await waitFor(() => expect(sent('post', '/purchase-orders')).toHaveLength(1))
    fireEvent.submit(form)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/purchase-orders')).toHaveLength(1)
    expect(orders).toHaveLength(1)
  })
})

// ── Order detail ────────────────────────────────────────────────────────────

describe('purchase order detail', () => {
  it('shows the order with the server\'s quantities, prices and totals', async () => {
    orders = [order('o1', 'draft', threeLines(), { notes: 'Deliver before noon.' })]
    // Figures no arithmetic on this page would produce: they are shown as sent.
    orders[0].items[0].total = '600.07'
    orders[0].total_amount = '1590.07'
    renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)

    expect(await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })).toBeInTheDocument()
    expect(header().getByText('Draft')).toBeInTheDocument()
    expect(header().getByText('Purchase order to Sanjeevani Pharma Distributors')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to purchase orders/ })).toHaveAttribute('href', '/pharmacy/purchase-orders')

    const para = screen.getByRole('row', { name: /Paracetamol/ })
    expect(within(para).getByText('PARA-500')).toBeInTheDocument()
    expect(within(para).getByText('500 units')).toBeInTheDocument()
    expect(within(para).getByText('1.20')).toBeInTheDocument()
    expect(within(para).getByText('600.07')).toBeInTheDocument()
    // An inventory manager can read stock, so the medicine opens its page.
    expect(within(para).getByRole('link', { name: 'Paracetamol' })).toHaveAttribute('href', '/pharmacy/medicines/m-PARA-500')
    expect(within(screen.getByRole('row', { name: /Amoxicillin/ })).getByText('840.00')).toBeInTheDocument()
    expect(screen.getAllByText('1,590.07')).toHaveLength(2)
    expect(screen.getByText('Deliver before noon.')).toBeInTheDocument()
    expect(screen.getByText(/An order cannot be edited: to change the vendor/)).toBeInTheDocument()

    const [request] = sent('get', '/purchase-orders/o1')
    expect(address(request)).toBe('/api/v1/purchase-orders/o1')
  })

  it('names a medicine without a link for a role that cannot read stock', async () => {
    orders = [order('o1', 'received', threeLines())]
    renderAt('/pharmacy/purchase-orders/o1', ['pharmacy.po.read'])

    const para = await screen.findByRole('row', { name: /Paracetamol/ })
    expect(within(para).queryByRole('link')).not.toBeInTheDocument()
    expect(screen.getByText(/which your role cannot open/)).toBeInTheDocument()
  })

  it('says what "sent" does not mean', async () => {
    orders = [order('o1', 'sent', threeLines())]
    renderAt('/pharmacy/purchase-orders/o1', PHARMACIST)

    await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })
    const note = screen.getByRole('alert')
    expect(note).toHaveTextContent('“Sent” only records the status and the time')
    expect(note).toHaveTextContent('Aetheris does not transmit anything to the vendor')
    expect(note).toHaveTextContent('an order is received once, in one go')
    // A pharmacist cannot cancel, so is not told to.
    expect(note).toHaveTextContent('someone who manages purchasing has to cancel it and draft another')
  })

  it('says a received order still shows what was ordered, not what arrived', async () => {
    orders = [order('o1', 'received', threeLines())]
    renderAt('/pharmacy/purchase-orders/o1', PHARMACIST)

    await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })
    const note = screen.getByRole('alert')
    expect(note).toHaveTextContent('The lines below still show what was ordered')
    expect(note).toHaveTextContent('What arrived — the batches, their quantities and costs — is not shown on the order')
    expect(note).toHaveTextContent("open each medicine's stock")
    expect(screen.getByRole('columnheader', { name: 'Ordered' })).toBeInTheDocument()
    expect(screen.queryByText(/received quantity|units received/i)).not.toBeInTheDocument()
  })

  it('says a cancelled order carries no reason and no time', async () => {
    orders = [order('o1', 'cancelled', threeLines(), { ordered_at: '2026-10-04T09:00:00Z' })]
    renderAt('/pharmacy/purchase-orders/o1', PHARMACIST)

    await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })
    const note = screen.getByRole('alert')
    expect(note).toHaveTextContent('The order keeps no reason and no time for its cancellation')
    expect(note).toHaveTextContent('It had been marked as sent before it was cancelled')
  })

  it('says an order is not found rather than failing', async () => {
    renderAt('/pharmacy/purchase-orders/missing', PHARMACIST)

    expect(await screen.findByText('Purchase order not found')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to purchase orders/ })).toBeInTheDocument()
  })

  it('treats an id that is not a UUID as an order that does not exist', async () => {
    // What the API answers for a malformed path parameter: nothing a retry could change.
    intercept = (c) =>
      c.url === '/purchase-orders/not-a-uuid' ? invalid('path.order_id', 'Input should be a valid UUID') : undefined
    renderAt('/pharmacy/purchase-orders/not-a-uuid', PHARMACIST)

    expect(await screen.findByText('Purchase order not found')).toBeInTheDocument()
    expect(screen.getByText("This purchase order doesn't exist, or you don't have access to it.")).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/connection|valid UUID|Validation failed/)
    expect(screen.getByRole('link', { name: /Back to purchase orders/ })).toBeInTheDocument()
  })

  it('says a refused read is a matter of access, not of the connection', async () => {
    orders = [order('o1', 'sent', threeLines())]
    // The permission was taken away after the page's guard let the user in.
    intercept = (c) =>
      c.url === '/purchase-orders/o1'
        ? fail(403, 'Permission denied. Required: pharmacy.po.read.', { error_code: 'PERMISSION_DENIED', errors: null })
        : undefined
    renderAt('/pharmacy/purchase-orders/o1', PHARMACIST)

    expect(await screen.findByText("You can't open this purchase order")).toBeInTheDocument()
    expect(screen.getByText(/Your account is not allowed to read purchase orders/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/connection/)
    expect(sent('get', '/purchase-orders/o1')).toHaveLength(1)
  })

  it('shows a loading state, then offers a retry when the order cannot be loaded', async () => {
    orders = [order('o1', 'sent', threeLines())]
    let answer: (outcome: Outcome) => void = () => {}
    intercept = (c) => (c.url === '/purchase-orders/o1' ? new Promise<Outcome>((resolve) => (answer = resolve)) : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders/o1', PHARMACIST)

    expect(await screen.findByLabelText('Loading purchase order')).toHaveAttribute('aria-busy', 'true')
    answer(fail(500, 'boom: upstream'))

    expect(await screen.findByText("Couldn't load this purchase order")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('heading', { level: 1, name: 'PO-2026-3FA85F64' })).toBeInTheDocument()
  })
})

// ── Sending and cancelling ──────────────────────────────────────────────────

describe('sending and cancelling a purchase order', () => {
  it('marks a draft as sent only after confirming, with no body', async () => {
    orders = [order('o1', 'draft', threeLines())]
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: /Mark as sent/ }))
    const d = await dialog()

    expect(d).toHaveTextContent('Records that PO-2026-3FA85F64 has gone to Sanjeevani Pharma Distributors')
    expect(d).toHaveTextContent('Aetheris does not transmit the order — send it to the vendor yourself')
    expect(sent('post', '/purchase-orders/o1/send')).toHaveLength(0)
    await user.click(within(d).getByRole('button', { name: 'Mark as sent' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('PO-2026-3FA85F64 marked as sent'))
    const [request] = sent('post', '/purchase-orders/o1/send')
    expect(address(request)).toBe('/api/v1/purchase-orders/o1/send')
    expect(request.data).toBeUndefined()
    await waitFor(() => expect(header().getByText('Sent')).toBeInTheDocument())
    expect(screen.getByText(/Aetheris does not transmit anything to the vendor/)).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual(['Receive', 'Cancel order']))
  })

  it.each(['draft', 'sent'] as const)('cancels a %s order with no body and no reason', async (status) => {
    orders = [order('o1', status, threeLines())]
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: /Cancel order/ }))
    const d = await dialog()

    expect(d).toHaveTextContent('a cancelled order cannot be reopened, sent or received')
    // The API has nowhere to keep a reason, so none is asked for.
    expect(within(d).queryByRole('textbox')).not.toBeInTheDocument()
    if (status === 'sent') expect(d).toHaveTextContent('Aetheris does not tell the vendor')
    else expect(d).not.toHaveTextContent('Aetheris does not tell the vendor')
    expect(sent('post', '/purchase-orders/o1/cancel')).toHaveLength(0)
    await user.click(within(d).getByRole('button', { name: 'Cancel order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('PO-2026-3FA85F64 cancelled'))
    const [request] = sent('post', '/purchase-orders/o1/cancel')
    expect(address(request)).toBe('/api/v1/purchase-orders/o1/cancel')
    expect(request.data).toBeUndefined()
    await waitFor(() => expect(header().getByText('Cancelled')).toBeInTheDocument())
    expect(screen.getByText(/keeps no reason and no time for its cancellation/)).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual([]))
  })

  it('keeps the order when the cancel dialog is dismissed', async () => {
    orders = [order('o1', 'sent', threeLines())]
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: /Cancel order/ }))

    await user.click(within(await dialog()).getByRole('button', { name: 'Keep order' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(fake.sent.filter((c) => c.method === 'post')).toHaveLength(0)
    expect(orders[0].status).toBe('sent')
  })

  it.each([
    ['draft', 'Mark as sent', 'cancelled', 'Cannot send a purchase order that is cancelled.', 'Cancelled'],
    ['sent', 'Cancel order', 'received', 'Cannot cancel a purchase order that is received.', 'Received'],
  ] as const)(
    'tells the user when a %s order moved on before they confirmed "%s"',
    async (status, step, movedTo, refusal, badge) => {
      orders = [order('o1', status, threeLines())]
      const user = userEvent.setup()
      renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)
      await user.click(await screen.findByRole('button', { name: step }))
      // Changed from another screen meanwhile.
      orders[0].status = movedTo
      await user.click(within(await dialog()).getByRole('button', { name: step }))

      await waitFor(() => expect(toastError).toHaveBeenCalledWith(refusal))
      expect(toastSuccess).not.toHaveBeenCalled()
      // The screen catches up with the server, and stops offering the step.
      await waitFor(() => expect(header().getByText(badge)).toBeInTheDocument())
      await waitFor(() => expect(actionNames()).toEqual([]))
    },
  )

  it('sends one request however many times a step is confirmed', async () => {
    orders = [order('o1', 'draft', threeLines())]
    const release = hold((c) => c.url === '/purchase-orders/o1/send')
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: /Mark as sent/ }))
    const d = await dialog()
    const form = within(d).getByRole('button', { name: 'Mark as sent' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Saving…' })).toBeDisabled()
    expect(sent('post', '/purchase-orders/o1/send')).toHaveLength(1)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/purchase-orders/o1/send')).toHaveLength(1)
  })
})

// ── Receiving ───────────────────────────────────────────────────────────────

describe('receiving a purchase order', () => {
  const batch = (d: HTMLElement, name: string) => within(d).getByRole('group', { name })
  const enter = (group: HTMLElement, values: { number?: string; expiry?: string; quantity?: string; cost?: string }) => {
    if (values.number !== undefined) fill(within(group).getByLabelText(/^Batch number/), values.number)
    if (values.expiry !== undefined) fill(within(group).getByLabelText(/^Expiry date/), values.expiry)
    if (values.quantity !== undefined) fill(within(group).getByLabelText(/^Quantity/), values.quantity)
    if (values.cost !== undefined) fill(within(group).getByLabelText(/^Cost per unit/), values.cost)
  }
  const tick = (d: HTMLElement) =>
    within(d).getByRole('checkbox', { name: 'I understand the rest of this order cannot be received later' })
  const submit = (d: HTMLElement) => within(d).getByRole('button', { name: 'Receive and close order' })
  const receipts = () => sent('post', '/purchase-orders/o1/receive')

  async function openReceipt(user: ReturnType<typeof userEvent.setup>, permissions = PHARMACIST) {
    renderAt('/pharmacy/purchase-orders/o1', permissions)
    await user.click(await screen.findByRole('button', { name: 'Receive' }))
    return dialog()
  }

  it('receives a split line, a line left out and a blank cost as one flat list', async () => {
    orders = [order('o1', 'sent', threeLines())]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    expect(d).toHaveTextContent('it is received once, in one go')
    expect(d).toHaveTextContent('3 of 50 batches entered')
    expect(d).toHaveTextContent('One receipt takes at most 50 batches across the whole order')
    expect(d).toHaveTextContent('topped up and keeps its existing cost')
    // Every order line is listed with what was ordered and at what price.
    expect(d).toHaveTextContent('Ordered 500 units at 1.20 each.')
    expect(d).toHaveTextContent('Ordered 200 units at 4.20 each.')
    expect(d).toHaveTextContent('Ordered 100 units at 1.50 each.')
    // One row per line to start with, its quantity what was ordered.
    const para1 = batch(d, 'Paracetamol (PARA-500), batch 1')
    expect(within(para1).getByLabelText(/^Quantity/)).toHaveValue('500')
    expect(within(para1).getByLabelText(/^Cost per unit/)).toHaveValue('')
    expect(within(para1).getByText('Blank uses the order price, 1.20')).toBeInTheDocument()
    // One column on a phone: two would leave a date input about 150px at 400px wide.
    const grid = within(para1).getByLabelText(/^Expiry date/).parentElement!.parentElement!
    expect(grid).toHaveClass('grid', 'sm:grid-cols-2')
    expect(grid).not.toHaveClass('grid-cols-2')

    enter(para1, { number: 'pa-1', expiry: '2027-08-01', quantity: '300' })
    await user.click(within(d).getByRole('button', { name: 'Add another batch of Paracetamol (PARA-500)' }))
    enter(batch(d, 'Paracetamol (PARA-500), batch 2'), { number: ' pa-2 ', expiry: '2027-11-01', quantity: '200', cost: '1.25' })
    await user.click(within(d).getByRole('button', { name: 'Nothing arrived for Amoxicillin (AMOX-500)' }))
    enter(batch(d, 'Cetirizine (CETZ-10), batch 1'), { number: 'cz-9', expiry: '2028-01-31' })

    expect(d).toHaveTextContent('All 500 units ordered are entered.')
    expect(d).toHaveTextContent('Nothing arrived: this line is left out of the receipt, and its 200 units cannot be received later.')
    expect(d).toHaveTextContent('1 line is short or left out: Amoxicillin.')
    await user.click(tick(d))
    await user.click(submit(d))

    // Counted from what was sent: the response says nothing about what arrived.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('PO-2026-3FA85F64 received — 3 batches added to stock'))
    const [request] = receipts()
    expect(address(request)).toBe('/api/v1/purchase-orders/o1/receive')
    const body = bodyOf(request) as { items: Record<string, unknown>[] }
    // po_item_id is the ORDER LINE's id, never the medicine's; batch numbers go in capitals.
    expect(body).toEqual({
      items: [
        { po_item_id: 'i-PARA-500', batch_number: 'PA-1', expiry_date: '2027-08-01', quantity: 300 },
        { po_item_id: 'i-PARA-500', batch_number: 'PA-2', expiry_date: '2027-11-01', quantity: 200, cost_per_unit: '1.25' },
        { po_item_id: 'i-CETZ-10', batch_number: 'CZ-9', expiry_date: '2028-01-31', quantity: 100 },
      ],
    })
    // A blank cost is absent — not empty, not null — so the server falls back to the order price.
    expect(body.items[0]).not.toHaveProperty('cost_per_unit')
    expect(body.items[2]).not.toHaveProperty('cost_per_unit')
    expect(JSON.stringify(body)).not.toMatch(/acknowledged|m-AMOX-500|i-AMOX-500/)
    expect(stock).toEqual([
      { medicine_id: 'm-PARA-500', batch_number: 'PA-1', expiry_date: '2027-08-01', quantity: 300, cost_per_unit: '1.20' },
      { medicine_id: 'm-PARA-500', batch_number: 'PA-2', expiry_date: '2027-11-01', quantity: 200, cost_per_unit: '1.25' },
      { medicine_id: 'm-CETZ-10', batch_number: 'CZ-9', expiry_date: '2028-01-31', quantity: 100, cost_per_unit: '1.50' },
    ])

    await waitFor(() => expect(header().getByText('Received')).toBeInTheDocument())
    // The order still says 200 Amoxicillin: it shows what was ordered, and says so.
    expect(within(screen.getByRole('row', { name: /Amoxicillin/ })).getByText('200 units')).toBeInTheDocument()
    expect(screen.getByText(/What arrived — the batches, their quantities and costs — is not shown on the order/)).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual([]))
  })

  it('will not close a short receipt until the shortfall as it stands is confirmed', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const row = batch(d, 'Paracetamol (PARA-500), batch 1')
    // A full receipt needs no confirmation.
    expect(d).toHaveTextContent('All 500 units ordered are entered.')
    expect(within(d).queryByRole('checkbox')).not.toBeInTheDocument()

    enter(row, { number: 'PA-1', expiry: '2027-08-01', quantity: '300' })
    expect(await within(d).findByText('This receipt is short of the order')).toBeInTheDocument()
    expect(d).toHaveTextContent('300 of 500 units entered — 200 units fewer than ordered, which cannot be received later.')
    expect(d).toHaveTextContent('what is missing here cannot be received on it afterwards')

    expect(tick(d)).not.toHaveAccessibleDescription()
    await user.click(submit(d))
    expect(await within(d).findByText('Tick the box to confirm before the order is closed')).toBeInTheDocument()
    // The refusal is tied to the box it is about, not only printed near it.
    expect(tick(d)).toBeInvalid()
    expect(tick(d)).toHaveAccessibleDescription('Tick the box to confirm before the order is closed')
    expect(receipts()).toHaveLength(0)

    await user.click(tick(d))
    expect(tick(d)).toBeChecked()
    // A different shortfall is a different thing to agree to: the tick does not carry over.
    enter(row, { quantity: '400' })
    await waitFor(() => expect(tick(d)).not.toBeChecked())
    await user.click(submit(d))
    expect(await within(d).findByText('Tick the box to confirm before the order is closed')).toBeInTheDocument()
    expect(receipts()).toHaveLength(0)

    await user.click(tick(d))
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('PO-2026-3FA85F64 received — 1 batch added to stock'))
    expect(bodyOf(receipts()[0])).toEqual({
      items: [{ po_item_id: 'i-PARA-500', batch_number: 'PA-1', expiry_date: '2027-08-01', quantity: 400 }],
    })
  })

  it('announces through one live region, the shortfall summary, and not once per line', async () => {
    orders = [order('o1', 'sent', threeLines())]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    // There before anything is short, so that what appears in it is announced.
    const live = d.querySelectorAll('[aria-live]')
    expect(live).toHaveLength(1)
    expect(live[0]).toHaveAttribute('aria-live', 'polite')
    expect(live[0]).toBeEmptyDOMElement()
    expect(within(d).queryByRole('alert')).not.toBeInTheDocument()

    enter(batch(d, 'Paracetamol (PARA-500), batch 1'), { quantity: '300' })
    await user.click(within(d).getByRole('button', { name: 'Nothing arrived for Cetirizine (CETZ-10)' }))

    expect(d.querySelectorAll('[aria-live]')).toHaveLength(1)
    expect(live[0]).toHaveTextContent('2 lines are short or left out: Paracetamol, Cetirizine.')
    // Polite: an alert here would interrupt at each keystroke that changes the list.
    expect(within(d).queryByRole('alert')).not.toBeInTheDocument()
    // The per-line verdicts are still on screen, as plain text.
    expect(d).toHaveTextContent('300 of 500 units entered — 200 units fewer than ordered')
    expect(live[0]).toContainElement(tick(d))
  })

  it('lets a long, unbroken vendor name wrap in each step\'s dialog', async () => {
    const long = 'V'.repeat(200)
    orders = [order('o1', 'draft', [line(PARA, 500, '1.20')], { vendor_name: long })]
    const user = userEvent.setup()
    renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)

    for (const [open, dismiss] of [['Mark as sent', 'Not yet'], ['Cancel order', 'Keep order']] as const) {
      await user.click(await screen.findByRole('button', { name: open }))
      const d = await dialog()
      expect(within(d).getByText(long)).toHaveClass('[overflow-wrap:anywhere]')
      await user.click(within(d).getByRole('button', { name: dismiss }))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    }

    await user.click(screen.getByRole('button', { name: 'Mark as sent' }))
    await user.click(within(await dialog()).getByRole('button', { name: 'Mark as sent' }))
    await user.click(await screen.findByRole('button', { name: 'Receive' }))
    expect(within(await dialog()).getByText(long)).toHaveClass('[overflow-wrap:anywhere]')
  })

  it('says when more arrived than was ordered, and asks for no confirmation', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    enter(batch(d, 'Paracetamol (PARA-500), batch 1'), { number: 'PA-1', expiry: '2027-08-01', quantity: '650' })
    expect(await within(d).findByText('Over')).toBeInTheDocument()
    expect(d).toHaveTextContent('650 units entered — 150 units more than the 500 ordered.')
    expect(within(d).queryByRole('checkbox')).not.toBeInTheDocument()
    await user.click(submit(d))

    // The server takes an over receipt as it is.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(receipts()[0])).toMatchObject({ items: [{ quantity: 650 }] })
  })

  it('refuses malformed batches before asking the server', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const row = batch(d, 'Paracetamol (PARA-500), batch 1')

    await user.click(submit(d))
    expect(await within(row).findByText('Enter the batch number')).toBeInTheDocument()
    expect(within(row).getByText('Enter the expiry date')).toBeInTheDocument()

    const number = within(row).getByLabelText(/^Batch number/)
    for (const bad of ['pa 1', '-PA1', 'PA#1', 'P'.repeat(51)]) {
      fill(number, bad)
      await waitFor(() => expect(number).toBeInvalid())
    }
    fill(number, 'pa-1_2.3/4')
    await waitFor(() => expect(number).toBeValid())
    const quantity = within(row).getByLabelText(/^Quantity/)
    // A quantity of 0 is a 422: nothing delivered is said with "Nothing arrived".
    for (const bad of ['0', '', '2.5', '1000001']) {
      fill(quantity, bad)
      await waitFor(() => expect(quantity).toBeInvalid())
    }
    expect(within(row).getByText('Whole units, 1–1,000,000')).toBeInTheDocument()
    const cost = within(row).getByLabelText(/^Cost per unit/)
    fill(cost, '1.234')
    await waitFor(() => expect(cost).toBeInvalid())
    expect(within(row).getByText('Enter an amount such as 1.20, or leave it blank')).toBeInTheDocument()

    expect(receipts()).toHaveLength(0)
  })

  it('needs at least one batch: a receipt of nothing is not a receipt', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    await user.click(within(d).getByRole('button', { name: 'Nothing arrived for Paracetamol (PARA-500)' }))
    expect(d).toHaveTextContent('0 of 50 batches entered')
    await user.click(tick(d))
    await user.click(submit(d))

    expect(
      await within(d).findByText('Enter at least one batch that arrived. An order nothing arrived for cannot be received.'),
    ).toBeInTheDocument()
    expect(receipts()).toHaveLength(0)

    // The line can be brought back, starting again from what was ordered.
    await user.click(within(d).getByRole('button', { name: 'Record a delivery of Paracetamol (PARA-500)' }))
    expect(within(batch(d, 'Paracetamol (PARA-500), batch 1')).getByLabelText(/^Quantity/)).toHaveValue('500')
  })

  it('refuses the same batch number twice on one line, whatever its case', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20'), line(AMOX, 200, '4.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    enter(batch(d, 'Paracetamol (PARA-500), batch 1'), { number: 'b1', expiry: '2027-08-01', quantity: '300' })
    await user.click(within(d).getByRole('button', { name: 'Add another batch of Paracetamol (PARA-500)' }))
    // The server uppercases before comparing, so this is the same batch.
    enter(batch(d, 'Paracetamol (PARA-500), batch 2'), { number: 'B1', expiry: '2027-08-01', quantity: '200' })
    // The same number on another medicine is another batch, and is fine.
    enter(batch(d, 'Amoxicillin (AMOX-500), batch 1'), { number: 'b1', expiry: '2027-09-01' })
    await user.click(submit(d))

    expect(
      await within(batch(d, 'Paracetamol (PARA-500), batch 2')).findByText(
        'This batch is already listed for this medicine — enter it once, with its whole quantity',
      ),
    ).toBeInTheDocument()
    expect(within(batch(d, 'Paracetamol (PARA-500), batch 1')).queryByRole('alert')).not.toBeInTheDocument()
    expect(within(batch(d, 'Amoxicillin (AMOX-500), batch 1')).queryByRole('alert')).not.toBeInTheDocument()
    expect(receipts()).toHaveLength(0)

    // With the second batch renamed the receipt goes through: three batches on a two-line order.
    enter(batch(d, 'Paracetamol (PARA-500), batch 2'), { number: 'b2' })
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('PO-2026-3FA85F64 received — 3 batches added to stock'))
    expect((bodyOf(receipts()[0]) as { items: { batch_number: string }[] }).items.map((i) => i.batch_number)).toEqual(['B1', 'B2', 'B1'])
  })

  it('removes the batch that was asked for, and never a line\'s only one', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20'), line(AMOX, 200, '4.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    // A line's only row has no remove button: leaving it out is "Nothing arrived".
    expect(within(d).queryByRole('button', { name: /^Remove / })).not.toBeInTheDocument()

    enter(batch(d, 'Paracetamol (PARA-500), batch 1'), { number: 'PA-1', expiry: '2027-08-01', quantity: '100' })
    await user.click(within(d).getByRole('button', { name: 'Add another batch of Paracetamol (PARA-500)' }))
    enter(batch(d, 'Paracetamol (PARA-500), batch 2'), { number: 'PA-2', expiry: '2027-09-01', quantity: '150' })
    await user.click(within(d).getByRole('button', { name: 'Add another batch of Paracetamol (PARA-500)' }))
    enter(batch(d, 'Paracetamol (PARA-500), batch 3'), { number: 'PA-3', expiry: '2027-10-01', quantity: '250' })
    enter(batch(d, 'Amoxicillin (AMOX-500), batch 1'), { number: 'AX-1', expiry: '2027-11-01' })
    expect(d).toHaveTextContent('4 of 50 batches entered')
    expect(within(d).queryByRole('button', { name: 'Remove Amoxicillin (AMOX-500), batch 1' })).not.toBeInTheDocument()

    await user.click(within(d).getByRole('button', { name: 'Remove Paracetamol (PARA-500), batch 2' }))

    expect(d).toHaveTextContent('3 of 50 batches entered')
    expect(within(batch(d, 'Paracetamol (PARA-500), batch 1')).getByLabelText(/^Batch number/)).toHaveValue('PA-1')
    expect(within(batch(d, 'Paracetamol (PARA-500), batch 2')).getByLabelText(/^Batch number/)).toHaveValue('PA-3')
    expect(within(d).queryByRole('group', { name: 'Paracetamol (PARA-500), batch 3' })).not.toBeInTheDocument()
    expect(d).toHaveTextContent('350 of 500 units entered — 150 units fewer than ordered')
    await user.click(tick(d))
    await user.click(submit(d))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(receipts()[0])).toEqual({
      items: [
        { po_item_id: 'i-PARA-500', batch_number: 'PA-1', expiry_date: '2027-08-01', quantity: 100 },
        { po_item_id: 'i-PARA-500', batch_number: 'PA-3', expiry_date: '2027-10-01', quantity: 250 },
        { po_item_id: 'i-AMOX-500', batch_number: 'AX-1', expiry_date: '2027-11-01', quantity: 200 },
      ],
    })
  })

  it('stops at fifty batches across the whole receipt, and says so beforehand', async () => {
    const many = Array.from({ length: 49 }, (_, n) => {
      const label = String(n + 1).padStart(2, '0')
      return line(medicine(`SKU-${label}`, `Medicine ${label}`), 10, '1.00')
    })
    orders = [order('o1', 'sent', many)]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    expect(d).toHaveTextContent('49 of 50 batches entered. One receipt takes at most 50 batches across the whole order, and there is no second receipt.')
    expect(d).not.toHaveTextContent('This one is full.')

    await user.click(within(d).getByRole('button', { name: 'Add another batch of Medicine 01 (SKU-01)' }))

    expect(d).toHaveTextContent('50 of 50 batches entered')
    expect(d).toHaveTextContent('This one is full.')
    const addButtons = within(d).getAllByRole('button', { name: /^Add another batch of/ })
    expect(addButtons).toHaveLength(49)
    for (const button of addButtons) expect(button).toBeDisabled()

    // Leaving a line out frees a row.
    await user.click(within(d).getByRole('button', { name: 'Nothing arrived for Medicine 49 (SKU-49)' }))
    expect(d).toHaveTextContent('49 of 50 batches entered')
    expect(within(d).getByRole('button', { name: 'Add another batch of Medicine 02 (SKU-02)' })).toBeEnabled()
  })

  it("puts the server's refusal of an expiry on the row it was sent as, not the row it sits at", async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20'), line(AMOX, 200, '4.20')])]
    // Amoxicillin already has this batch, with a different expiry.
    stock = [{ medicine_id: 'm-AMOX-500', batch_number: 'AX-2503', expiry_date: '2027-08-01', quantity: 200, cost_per_unit: '4.20' }]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    // Paracetamol is left out, so the Amoxicillin rows are items 0 and 1 of what is sent.
    await user.click(within(d).getByRole('button', { name: 'Nothing arrived for Paracetamol (PARA-500)' }))
    enter(batch(d, 'Amoxicillin (AMOX-500), batch 1'), { number: 'ax-1', expiry: '2027-09-01', quantity: '100' })
    await user.click(within(d).getByRole('button', { name: 'Add another batch of Amoxicillin (AMOX-500)' }))
    enter(batch(d, 'Amoxicillin (AMOX-500), batch 2'), { number: 'ax-2503', expiry: '2028-01-01', quantity: '100' })
    await user.click(tick(d))
    await user.click(submit(d))

    // The server named `items.1.expiry_date`.
    const second = within(batch(d, 'Amoxicillin (AMOX-500), batch 2')).getByLabelText(/^Expiry date/)
    await waitFor(() => expect(second).toBeInvalid())
    expect(second).toHaveAccessibleDescription('Batch AX-2503 is already recorded as expiring 2027-08-01.')
    expect(within(batch(d, 'Amoxicillin (AMOX-500), batch 1')).getByLabelText(/^Expiry date/)).toBeValid()
    expect((bodyOf(receipts()[0]) as { items: { batch_number: string }[] }).items.map((i) => i.batch_number)).toEqual(['AX-1', 'AX-2503'])
    // Nothing was received: the order is still sent and the stock untouched.
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(orders[0].status).toBe('sent')
    expect(stock).toHaveLength(1)

    // With the expiry the batch already has, it is topped up.
    fill(second, '2027-08-01')
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('PO-2026-3FA85F64 received — 2 batches added to stock'))
    expect(stock.find((b) => b.batch_number === 'AX-2503')).toMatchObject({ quantity: 300, cost_per_unit: '4.20' })
  })

  it('leaves the expiry to the server, and shows its refusal of a batch already expired', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const row = batch(d, 'Paracetamol (PARA-500), batch 1')

    // Yesterday on the hospital's calendar. The form does not judge it: it asks.
    enter(row, { number: 'PA-1', expiry: '2026-10-04' })
    await user.click(submit(d))

    const expiry = within(row).getByLabelText(/^Expiry date/)
    await waitFor(() => expect(expiry).toBeInvalid())
    expect(expiry).toHaveAccessibleDescription('A batch that has already expired cannot be received.')
    expect(receipts()).toHaveLength(1)

    // Today itself is accepted.
    fill(expiry, '2026-10-05')
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(receipts()).toHaveLength(2)
  })

  it('shows a refusal of the whole list, and one pinned on a row, where they can be read', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const row = batch(d, 'Paracetamol (PARA-500), batch 1')
    enter(row, { number: 'PA-1', expiry: '2027-08-01' })

    intercept = (c) =>
      c.url === '/purchase-orders/o1/receive'
        ? fail(422, 'Validation failed.', {
            errors: [{ field: 'items', message: 'Value error, Each batch may be listed only once per order line.' }],
          })
        : undefined
    await user.click(submit(d))
    expect(await within(d).findByText('Each batch may be listed only once per order line.')).toBeInTheDocument()
    expect(d).not.toHaveTextContent('Value error')

    // The line was removed from the order's side: named by a field no input holds.
    intercept = (c) =>
      c.url === '/purchase-orders/o1/receive'
        ? fail(422, 'Not an item on this order.', {
            errors: { errors: [{ field: 'items.0.po_item_id', message: 'Not an item on this order.' }] },
          })
        : undefined
    await user.click(submit(d))
    expect(await within(row).findByText('Not an item on this order.')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('tells the user when the order was already received, and catches up', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    enter(batch(d, 'Paracetamol (PARA-500), batch 1'), { number: 'PA-1', expiry: '2027-08-01' })
    // Received at another counter while this form was open.
    Object.assign(orders[0], { status: 'received', received_at: '2026-10-05T06:45:00Z' })

    await user.click(submit(d))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Cannot receive a purchase order that is received.'))
    expect(toastSuccess).not.toHaveBeenCalled()
    // Nothing was added a second time, and the screen shows the order as it now is.
    expect(stock).toHaveLength(0)
    await waitFor(() => expect(header().getByText('Received')).toBeInTheDocument())
    await waitFor(() => expect(actionNames()).toEqual([]))
  })

  it('on a 500 shows a message the user can act on, never raw server text', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    enter(batch(d, 'Paracetamol (PARA-500), batch 1'), { number: 'PA-1', expiry: '2027-08-01' })
    intercept = (c) =>
      c.url === '/purchase-orders/o1/receive' ? fail(500, 'boom: duplicate key uq_medicine_batches_medicine_batch') : undefined

    await user.click(submit(d))

    const shown = "Couldn't confirm that the order was received. Try again: an order is received only once, so nothing is added to stock twice."
    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(document.body.textContent).not.toMatch(/boom|uq_medicine/)

    // The retry the message invites goes through.
    intercept = () => undefined
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
  })

  it('receives once however many times the form is submitted', async () => {
    orders = [order('o1', 'sent', [line(PARA, 500, '1.20')])]
    const user = userEvent.setup()
    const d = await openReceipt(user, INVENTORY_MANAGER)
    enter(batch(d, 'Paracetamol (PARA-500), batch 1'), { number: 'PA-1', expiry: '2027-08-01' })
    const release = hold((c) => c.url === '/purchase-orders/o1/receive')
    const form = submit(d).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Receiving…' })).toBeDisabled()
    await waitFor(() => expect(receipts()).toHaveLength(1))
    // The rows cannot be rearranged while the request they were sent in is in flight.
    expect(within(d).getByRole('button', { name: 'Add another batch of Paracetamol (PARA-500)' })).toBeDisabled()
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(receipts()).toHaveLength(1)
    expect(stock).toEqual([
      { medicine_id: 'm-PARA-500', batch_number: 'PA-1', expiry_date: '2027-08-01', quantity: 500, cost_per_unit: '1.20' },
    ])
  })
})

// ── Refusals of a step ──────────────────────────────────────────────────────

describe('a step on a purchase order that the server refuses', () => {
  const STEPS = {
    send: {
      from: 'draft',
      open: 'Mark as sent',
      confirm: 'Mark as sent',
      needs: 'pharmacy.po.update',
      fallback: "Couldn't mark the order as sent. Please try again.",
    },
    cancel: {
      from: 'sent',
      open: 'Cancel order',
      confirm: 'Cancel order',
      needs: 'pharmacy.po.update',
      fallback: "Couldn't cancel the order. Please try again.",
    },
    receive: {
      from: 'sent',
      open: 'Receive',
      confirm: 'Receive and close order',
      needs: 'pharmacy.po.receive',
      fallback:
        "Couldn't confirm that the order was received. Try again: an order is received only once, so nothing is added to stock twice.",
    },
  } as const
  type Step = keyof typeof STEPS
  const NAMES = Object.keys(STEPS) as Step[]

  /** Open the step's dialog on order o1, ready to confirm. */
  async function ready(user: ReturnType<typeof userEvent.setup>, step: Step) {
    orders = [order('o1', STEPS[step].from, [line(PARA, 500, '1.20')])]
    renderAt('/pharmacy/purchase-orders/o1', INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: STEPS[step].open }))
    const d = await dialog()
    if (step === 'receive') {
      fill(within(d).getByLabelText(/^Batch number/), 'PA-1')
      fill(within(d).getByLabelText(/^Expiry date/), '2027-08-01')
    }
    return d
  }

  it.each(NAMES)('shows the API\'s words when "%s" is not permitted, and changes nothing', async (step) => {
    const { from, confirm, needs } = STEPS[step]
    const message = `Permission denied. Required: ${needs}.`
    const user = userEvent.setup()
    const d = await ready(user, step)
    // The role lost the permission after the page was drawn.
    intercept = (c) =>
      c.url === `/purchase-orders/o1/${step}`
        ? fail(403, message, { error_code: 'PERMISSION_DENIED', errors: null })
        : undefined

    await user.click(within(d).getByRole('button', { name: confirm }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(message)
    expect(toastError).toHaveBeenCalledWith(message)
    expect(toastSuccess).not.toHaveBeenCalled()
    const requests = sent('post', `/purchase-orders/o1/${step}`)
    expect(requests).toHaveLength(1)
    expect(address(requests[0])).toBe(`/api/v1/purchase-orders/o1/${step}`)
    expect(orders[0].status).toBe(from)
    expect(stock).toHaveLength(0)
    // Still open: the order is as it was, and is still shown as it was.
    expect(within(d).getByRole('button', { name: confirm })).toBeEnabled()
    expect(header().getByText(from === 'draft' ? 'Draft' : 'Sent')).toBeInTheDocument()
  })

  it.each(NAMES)('ends on "not found" when the order is gone by the time "%s" is confirmed', async (step) => {
    const user = userEvent.setup()
    const d = await ready(user, step)
    // Not something the API offers, but a row can vanish under a screen all the same.
    orders = []

    await user.click(within(d).getByRole('button', { name: STEPS[step].confirm }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Purchase order not found.'))
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(sent('post', `/purchase-orders/o1/${step}`)).toHaveLength(1)
    // The page asks again, and stops showing an order that is not there.
    expect(await screen.findByText('Purchase order not found')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(actionNames()).toEqual([])
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(stock).toHaveLength(0)
  })

  it.each(NAMES)('on a 500 from "%s" shows a message the user can act on, never raw server text', async (step) => {
    const { from, confirm, fallback } = STEPS[step]
    const user = userEvent.setup()
    const d = await ready(user, step)
    intercept = (c) =>
      c.url === `/purchase-orders/o1/${step}` ? fail(500, 'boom: lock wait timeout exceeded') : undefined

    await user.click(within(d).getByRole('button', { name: confirm }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(fallback)
    expect(toastError).toHaveBeenCalledWith(fallback)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/boom|lock wait/)
    expect(orders[0].status).toBe(from)

    // The retry the message invites goes through.
    intercept = () => undefined
    await user.click(within(d).getByRole('button', { name: confirm }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', `/purchase-orders/o1/${step}`)).toHaveLength(2)
  })
})
