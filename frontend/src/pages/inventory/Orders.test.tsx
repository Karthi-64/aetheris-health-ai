import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type {
  InventoryItem,
  InventoryLocation,
  InventoryOrder,
  InventoryOrderItem,
  InventoryOrderStatus,
} from '@/api/inventory'
import type { Vendor } from '@/api/pharmacy'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { formatDate } from '@/lib/format'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import InventoryOrderDetailPage from './InventoryOrderDetailPage'
import InventoryOrdersPage from './InventoryOrdersPage'
import { toReceiptBody } from './inventoryOrderForm'

/**
 * Inventory purchase orders, against the merged backend contract
 * (docs/18-API_CONTRACTS.md §10.7; `backend/app/api/v1/inventory.py`,
 * `schemas/inventory.py`, `services/inventory_po_service.py`). The real hooks,
 * permission check, `http` wrapper and Axios instance run; only the network
 * adapter is replaced by an in-memory "server" that keeps the vendors, items,
 * locations, orders and stock, and — like the real one — refuses what the
 * contract refuses: an inactive vendor or item, a step from the wrong status,
 * an unknown or inactive location, a batch number that does not suit its item,
 * an expiry that has passed or disagrees with the batch already held.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → permissions as seeded in backend/app/seeds/seed.py.
const INVENTORY_CODES = [
  'inventory.item.read',
  'inventory.item.create',
  'inventory.item.update',
  'inventory.location.read',
  'inventory.location.create',
  'inventory.location.update',
  'inventory.stock.read',
  'inventory.consume',
  'inventory.transfer',
  'inventory.adjust',
  'inventory.po.read',
  'inventory.po.create',
  'inventory.po.update',
  'inventory.po.receive',
  'inventory.forecast.read',
]
const HOSPITAL_ADMIN = [...INVENTORY_CODES, 'pharmacy.vendor.read', 'notification.read.own']
const INVENTORY_MANAGER = [
  ...INVENTORY_CODES,
  'pharmacy.vendor.read',
  'pharmacy.vendor.create',
  'pharmacy.vendor.update',
  'pharmacy.po.read',
  'notification.read.own',
]
// A nurse records what the ward uses and nothing else.
const NURSE = [
  'inventory.item.read',
  'inventory.location.read',
  'inventory.stock.read',
  'inventory.consume',
  'patient.read',
  'appointment.read',
  'lab.order.read',
  'notification.read.own',
]
// Read-only in inventory; reads vendors through Pharmacy.
const PHARMACIST = [
  'inventory.item.read',
  'inventory.location.read',
  'inventory.stock.read',
  'pharmacy.vendor.read',
  'pharmacy.po.read',
  'notification.read.own',
]
// None of these holds an inventory code at all.
const DOCTOR = ['patient.read', 'appointment.read', 'notification.read.own']
const RECEPTIONIST = ['patient.read', 'appointment.read', 'invoice.read', 'notification.read.own']
const BILLING_STAFF = ['invoice.read', 'patient.read', 'notification.read.own']
const LAB_TECHNICIAN = ['lab.order.read', 'patient.read', 'notification.read.own']

// ── The in-memory server ────────────────────────────────────────────────────

/** A two-place decimal the way the server keeps it: whole hundredths in, a decimal string out. */
const cents = (amount: string) => {
  const [whole, fraction = ''] = amount.split('.')
  return Number(whole) * 100 + Number(fraction.padEnd(2, '0'))
}
const decimal = (hundredths: number) => (hundredths / 100).toFixed(2)
const DECIMAL = /^\d+(\.\d{1,2})?$/

function vendor(id: string, name: string, extra: Partial<Vendor> = {}): Vendor {
  return { id, name, contact: null, address: null, tax_id: null, is_active: true, created_at: '2026-10-01T00:00:00Z', ...extra }
}

function item(sku: string, name: string, extra: Partial<InventoryItem> = {}): InventoryItem {
  return {
    id: `it-${sku}`,
    sku,
    name,
    category: 'Disposables',
    unit_of_measure: 'piece',
    is_batch_tracked: false,
    reorder_point: null,
    target_stock: null,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...extra,
  }
}

function location(id: string, code: string, name: string, extra: Partial<InventoryLocation> = {}): InventoryLocation {
  return { id, code, name, kind: 'store', is_active: true, ...extra }
}

function line(i: InventoryItem, quantity: string, unitPrice: string): InventoryOrderItem {
  return {
    id: `i-${i.sku}`,
    item_id: i.id,
    item_sku: i.sku,
    item_name: i.name,
    quantity: decimal(cents(quantity)),
    unit_price: decimal(cents(unitPrice)),
    total: decimal(Math.round((cents(unitPrice) * cents(quantity)) / 100)),
  }
}

function order(
  id: string,
  status: InventoryOrderStatus,
  items: InventoryOrderItem[],
  extra: Partial<InventoryOrder> = {},
): InventoryOrder {
  return {
    id,
    po_number: 'IPO-2026-3FA85F64',
    vendor_id: 'v1',
    vendor_name: 'Sanjeevani Pharma Distributors',
    status,
    notes: null,
    ordered_at: status === 'sent' || status === 'received' ? '2026-10-04T09:00:00Z' : null,
    received_at: status === 'received' ? '2026-10-05T06:30:00Z' : null,
    received_location_id: status === 'received' ? 'loc-main' : null,
    total_amount: decimal(items.reduce((sum, i) => sum + cents(i.total), 0)),
    created_at: '2026-10-03T08:00:00Z',
    items,
    ...extra,
  }
}

const SANJEEVANI = vendor('v1', 'Sanjeevani Pharma Distributors')
const MEDLINE = vendor('v2', 'Medline Wholesale')
const APEX = vendor('v3', 'Apex Remedies', { is_active: false })

const GLOVE = item('GLOVE-M', 'Nitrile gloves, medium', { unit_of_measure: 'box of 100' })
const CANNULA = item('CANN-20G', 'IV cannula 20G', { is_batch_tracked: true })
const GAUZE = item('GAUZE-R', 'Sterile gauze roll', { unit_of_measure: 'roll' })
const OLD_MASK = item('MASK-OLD', 'Surgical mask (withdrawn)', { is_active: false })

const MAIN = location('loc-main', 'MAIN', 'Main Store')
const ICU = location('loc-icu', 'ICU', 'ICU Store', { kind: 'icu' })
const ANNEXE = location('loc-annexe', 'ANNEXE', 'Old Annexe', { is_active: false })

/** A stock row, as far as receiving cares: a batch at a location, whose number fixes its expiry. */
interface Shelf {
  item_id: string
  location_id: string
  batch_number: string | null
  expiry_date: string | null
  quantity: string
}

/** The hospital's local date, which is what the server judges an expiry against. */
const TODAY = '2026-10-05'

let vendors: Vendor[]
let items: InventoryItem[]
let locations: InventoryLocation[]
let orders: InventoryOrder[]
let stock: Shelf[]
/** Return an outcome to answer a request yourself; nothing to let the server answer. */
let intercept: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome> | undefined
let fake: FakeApi
let created = 0

const pageOf = (rows: unknown[], config: InternalAxiosRequestConfig): Outcome => {
  const { page = 1, page_size: size = 25 } = (config.params ?? {}) as { page?: number; page_size?: number }
  return {
    status: 200,
    data: {
      success: true,
      message: 'ok',
      data: rows.slice((page - 1) * size, page * size).map((x) => structuredClone(x)),
      metadata: {
        pagination: { page, page_size: size, total_records: rows.length, total_pages: Math.max(1, Math.ceil(rows.length / size)) },
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

const wrongStatus = (verb: string, o: InventoryOrder) =>
  fail(400, `Cannot ${verb} a purchase order that is ${o.status}.`, {
    error_code: 'BUSINESS_RULE_VIOLATION',
    errors: { status: o.status },
  })

const denied = (code: string) =>
  fail(403, `Permission denied. Required: ${code}.`, { error_code: 'PERMISSION_DENIED', errors: null })

const extraKey = (body: object, allowed: string[]) => Object.keys(body).find((k) => !allowed.includes(k))

interface ReceiptRow {
  po_item_id: string
  quantity: string
  batch_number?: string | null
  expiry_date?: string | null
}

function server(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const method = config.method ?? 'get'
  const params = (config.params ?? {}) as Record<string, unknown>

  // An empty query value is not "no filter": it fails to parse.
  const empty = Object.keys(params).find((key) => params[key] === '')
  if (empty) return invalid(`query.${empty}`, 'Input should be a valid value')

  // ── Vendors (Pharmacy's) ──
  if (url === '/vendors' && method === 'get') {
    return pageOf(
      vendors
        .filter((v) => params.is_active === undefined || v.is_active === params.is_active)
        .sort((a, b) => a.name.localeCompare(b.name)),
      config,
    )
  }

  // ── Items and locations ──
  if (url === '/inventory/items' && method === 'get') {
    const q = typeof params.q === 'string' ? params.q.toLowerCase() : ''
    return pageOf(
      items.filter(
        (i) =>
          (!q || i.name.toLowerCase().startsWith(q) || i.sku.toLowerCase() === q) &&
          (params.is_active === undefined || i.is_active === params.is_active),
      ),
      config,
    )
  }
  const itemPath = /^\/inventory\/items\/([^/]+)$/.exec(url)
  if (itemPath && method === 'get') {
    const found = items.find((i) => i.id === itemPath[1])
    return found
      ? ok(structuredClone(found))
      : fail(404, 'Inventory item not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { item_id: itemPath[1] } })
  }
  // A plain list: locations are not paginated.
  if (url === '/inventory/locations' && method === 'get') {
    return ok(
      locations
        .filter((l) => params.is_active === undefined || l.is_active === params.is_active)
        .map((l) => structuredClone(l)),
    )
  }

  // ── Purchase orders ──
  if (url === '/inventory/purchase-orders' && method === 'get') {
    return pageOf(
      orders.filter(
        (o) =>
          (params.status === undefined || o.status === params.status) &&
          (params.vendor_id === undefined || o.vendor_id === params.vendor_id),
      ),
      config,
    )
  }
  if (url === '/inventory/purchase-orders' && method === 'post') {
    const body = bodyOf(config) as {
      vendor_id?: string
      notes?: string | null
      items?: { item_id: string; quantity: string; unit_price: string }[]
    }
    const extra = extraKey(body, ['vendor_id', 'notes', 'items'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    if (typeof body.vendor_id !== 'string') return invalid('vendor_id', 'Field required')
    if (typeof body.notes === 'string' && body.notes.length > 2000) return invalid('notes', 'String should have at most 2000 characters')
    if (!Array.isArray(body.items) || body.items.length < 1) return invalid('items', 'List should have at least 1 item after validation, not 0')
    if (body.items.length > 50) return invalid('items', `List should have at most 50 items after validation, not ${body.items.length}`)
    for (const [i, row] of body.items.entries()) {
      const extraOnLine = extraKey(row, ['item_id', 'quantity', 'unit_price'])
      if (extraOnLine) return invalid(`items.${i}.${extraOnLine}`, 'Extra inputs are not permitted')
      // Decimal strings in both directions; a JSON number would also parse, but the client sends text.
      if (typeof row.quantity !== 'string' || !DECIMAL.test(row.quantity)) return invalid(`items.${i}.quantity`, 'Input should be a valid decimal')
      if (cents(row.quantity) <= 0) return invalid(`items.${i}.quantity`, 'Input should be greater than 0')
      if (typeof row.unit_price !== 'string' || !DECIMAL.test(row.unit_price)) return invalid(`items.${i}.unit_price`, 'Input should be a valid decimal')
    }
    if (new Set(body.items.map((i) => i.item_id)).size !== body.items.length) {
      return invalid('items', 'Value error, Each item may appear only once in an order.')
    }
    // Service rules: the vendor first, then the lines in order; only the first failure is reported.
    const v = vendors.find((x) => x.id === body.vendor_id)
    if (!v) return refused('vendor_id', 'Vendor not found in this hospital.')
    if (!v.is_active) return refused('vendor_id', `Vendor '${v.name}' is inactive.`)
    const lines: InventoryOrderItem[] = []
    for (const [i, row] of body.items.entries()) {
      const found = items.find((x) => x.id === row.item_id)
      if (!found) return refused(`items.${i}.item_id`, 'Inventory item not found in this hospital.')
      if (!found.is_active) return refused(`items.${i}.item_id`, `Item '${found.sku}' is inactive and cannot be ordered.`)
      lines.push(line(found, row.quantity, row.unit_price))
    }
    created += 1
    const drafted = order(`new${created}`, 'draft', lines, {
      // Random on the real server; a client cannot know it before the answer.
      po_number: `IPO-2026-${(0xc0ffee00 + created).toString(16).toUpperCase()}`,
      vendor_id: v.id,
      vendor_name: v.name,
      notes: typeof body.notes === 'string' && body.notes.trim() ? body.notes.trim() : null,
      created_at: '2026-10-05T05:00:00Z',
    })
    orders.unshift(drafted)
    return ok(structuredClone(drafted), 201)
  }

  const orderPath = /^\/inventory\/purchase-orders\/([^/]+)(?:\/(send|cancel|receive))?$/.exec(url)
  if (orderPath) {
    const [, id, step] = orderPath
    const notFound = fail(404, 'Purchase order not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { purchase_order_id: id } })
    const o = orders.find((x) => x.id === id)

    // Request validation runs before the order is even looked up.
    let rows: ReceiptRow[] = []
    let locationId = ''
    if (step === 'receive') {
      const body = (bodyOf(config) ?? {}) as { location_id?: string; items?: ReceiptRow[] }
      const extra = extraKey(body, ['location_id', 'items'])
      if (extra) return invalid(extra, 'Extra inputs are not permitted')
      if (typeof body.location_id !== 'string' || !body.location_id) return invalid('location_id', 'Field required')
      locationId = body.location_id
      if (!Array.isArray(body.items) || body.items.length < 1) return invalid('items', 'List should have at least 1 item after validation, not 0')
      if (body.items.length > 50) return invalid('items', `List should have at most 50 items after validation, not ${body.items.length}`)
      rows = []
      for (const [i, r] of body.items.entries()) {
        const extraOnRow = extraKey(r, ['po_item_id', 'quantity', 'batch_number', 'expiry_date'])
        if (extraOnRow) return invalid(`items.${i}.${extraOnRow}`, 'Extra inputs are not permitted')
        if (typeof r.quantity !== 'string' || !DECIMAL.test(r.quantity)) return invalid(`items.${i}.quantity`, 'Input should be a valid decimal')
        if (cents(r.quantity) <= 0) return invalid(`items.${i}.quantity`, 'Input should be greater than 0')
        let batch: string | null = null
        if (r.batch_number !== undefined && r.batch_number !== null) {
          // The length is measured on the text as sent, before it is trimmed.
          if (r.batch_number.length > 50) return invalid(`items.${i}.batch_number`, 'String should have at most 50 characters')
          batch = r.batch_number.trim().toUpperCase()
          if (!/^[A-Z0-9][A-Z0-9_./-]*$/.test(batch)) {
            return invalid(`items.${i}.batch_number`, 'Value error, Batch number may contain only letters, digits and the characters - _ . /')
          }
        }
        if (r.expiry_date !== undefined && r.expiry_date !== null && !/^\d{4}-\d{2}-\d{2}$/.test(r.expiry_date)) {
          return invalid(`items.${i}.expiry_date`, 'Input should be a valid date')
        }
        rows.push({ po_item_id: r.po_item_id, quantity: r.quantity, batch_number: batch, expiry_date: r.expiry_date ?? null })
      }
      if (new Set(rows.map((r) => `${r.po_item_id} ${r.batch_number}`)).size !== rows.length) {
        return invalid('items', 'Value error, Each batch may be listed only once per order line.')
      }
    }

    if (!o) return notFound
    if (method === 'get') return ok(structuredClone(o))
    // The step endpoints take no body at all.
    if (step !== 'receive' && config.data !== undefined) return invalid('', 'Extra inputs are not permitted')

    if (step === 'send') {
      if (o.status !== 'draft') return wrongStatus('send', o)
      Object.assign(o, { status: 'sent', ordered_at: '2026-10-05T06:00:00Z' })
    } else if (step === 'cancel') {
      if (o.status !== 'draft' && o.status !== 'sent') return wrongStatus('cancel', o)
      // No reason and no time: the model has neither.
      o.status = 'cancelled'
    } else if (step === 'receive') {
      if (o.status !== 'sent') return wrongStatus('receive', o)
      const into = locations.find((l) => l.id === locationId)
      if (!into) return refused('location_id', 'Inventory location not found in this hospital.')
      if (!into.is_active) {
        return fail(400, `Location '${into.code}' is inactive and cannot receive stock.`, { error_code: 'BUSINESS_RULE_VIOLATION', errors: null })
      }
      const held = (itemId: string, batch: string | null) =>
        stock.find((s) => s.item_id === itemId && s.location_id === into.id && (s.batch_number ?? '') === (batch ?? ''))
      // The whole receipt is checked before anything is written.
      for (const [i, r] of rows.entries()) {
        const ordered = o.items.find((x) => x.id === r.po_item_id)
        if (!ordered) return refused(`items.${i}.po_item_id`, 'Not an item on this order.')
        const what = items.find((x) => x.id === ordered.item_id)!
        if (what.is_batch_tracked && r.batch_number === null) {
          return refused(`items.${i}.batch_number`, `${what.name} is batch-tracked: give a batch number.`)
        }
        if (!what.is_batch_tracked && r.batch_number !== null) {
          return refused(`items.${i}.batch_number`, `${what.name} is not batch-tracked.`)
        }
        if (r.expiry_date && r.expiry_date < TODAY) {
          return refused(`items.${i}.expiry_date`, 'A batch that has already expired cannot be received.')
        }
      }
      for (const [i, r] of rows.entries()) {
        const ordered = o.items.find((x) => x.id === r.po_item_id)!
        const row = held(ordered.item_id, r.batch_number ?? null)
        if (row && r.batch_number && r.expiry_date && row.expiry_date !== r.expiry_date) {
          return refused(`items.${i}.expiry_date`, `Batch ${row.batch_number} is already recorded as expiring ${row.expiry_date ?? 'never'}.`)
        }
      }
      for (const r of rows) {
        const ordered = o.items.find((x) => x.id === r.po_item_id)!
        const row = held(ordered.item_id, r.batch_number ?? null)
        // A batch already held is topped up and keeps its expiry.
        if (row) row.quantity = decimal(cents(row.quantity) + cents(r.quantity))
        else {
          stock.push({
            item_id: ordered.item_id,
            location_id: into.id,
            batch_number: r.batch_number ?? null,
            expiry_date: r.expiry_date ?? null,
            quantity: decimal(cents(r.quantity)),
          })
        }
      }
      // What arrived is not echoed: the lines still say what was ordered. Only the location is.
      Object.assign(o, { status: 'received', received_at: '2026-10-05T07:00:00Z', received_location_id: into.id })
    }
    return ok(structuredClone(o))
  }

  return fail(404, 'Not Found')
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  vendors = [SANJEEVANI, MEDLINE, APEX].map((v) => structuredClone(v))
  items = [GLOVE, CANNULA, GAUZE, OLD_MASK].map((i) => structuredClone(i))
  locations = [MAIN, ICU, ANNEXE].map((l) => structuredClone(l))
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
          <Route
            path="/inventory/purchase-orders"
            element={
              <RequirePermission permission="inventory.po.read">
                <InventoryOrdersPage />
              </RequirePermission>
            }
          />
          <Route
            path="/inventory/purchase-orders/:orderId"
            element={
              <RequirePermission permission="inventory.po.read">
                <InventoryOrderDetailPage />
              </RequirePermission>
            }
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const LIST = '/inventory/purchase-orders'
const ORDER = '/inventory/purchase-orders/o1'
const NUMBER = 'IPO-2026-3FA85F64'

/** Requests to exactly this path (`fake.requests` matches a part, and the list's path is part of every step's). */
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

/** Gloves (untracked), cannulas (batch-tracked) and gauze (untracked, a fractional quantity). */
const threeLines = () => [line(GLOVE, '40', '210.00'), line(CANNULA, '250', '18.50'), line(GAUZE, '12.5', '30.00')]

// ── Access ──────────────────────────────────────────────────────────────────

describe('inventory purchase order access', () => {
  it.each([
    // Neither has inventory.po.read, though both read items, locations and stock.
    ['a nurse', NURSE],
    ['a pharmacist', PHARMACIST],
    ['a doctor', DOCTOR],
    ['a receptionist', RECEPTIONIST],
    ['billing staff', BILLING_STAFF],
    ['a lab technician', LAB_TECHNICIAN],
  ])('sends %s back to the dashboard from both pages, asking the API nothing', async (_who, permissions) => {
    orders = [order('o1', 'sent', threeLines())]
    for (const page of [LIST, ORDER]) {
      const view = renderAt(page, permissions)
      expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
      view.unmount()
    }
    expect(fake.sent).toHaveLength(0)
  })

  it.each([
    ['an inventory manager', INVENTORY_MANAGER],
    ['a hospital admin', HOSPITAL_ADMIN],
  ])('offers %s everything', async (_who, permissions) => {
    orders = [order('o1', 'sent', threeLines())]

    const view = renderAt(LIST, permissions)
    await screen.findByRole('row', { name: new RegExp(NUMBER) })
    expect(screen.getByRole('button', { name: /New order/ })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Filter by vendor' })).toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    const tabs = within(screen.getByRole('navigation', { name: 'Inventory sections' }))
    expect(tabs.getByRole('link', { name: 'Purchase orders' })).toHaveAttribute('href', LIST)
    view.unmount()

    renderAt(ORDER, permissions)
    await screen.findByRole('heading', { level: 1, name: NUMBER })
    expect(actionNames()).toEqual(['Receive', 'Cancel order'])
  })

  // Each step needs the code its endpoint requires (`backend/app/api/v1/inventory.py`).
  const READ = ['inventory.po.read']
  const RECEIVER = ['inventory.po.read', 'inventory.po.receive', 'inventory.location.read']
  const SENDER = ['inventory.po.read', 'inventory.po.update']
  it.each([
    ['an inventory manager', INVENTORY_MANAGER, 'draft', ['Mark as sent', 'Cancel order']],
    ['an inventory manager', INVENTORY_MANAGER, 'sent', ['Receive', 'Cancel order']],
    ['an inventory manager', INVENTORY_MANAGER, 'received', []],
    ['an inventory manager', INVENTORY_MANAGER, 'cancelled', []],
    ['a hospital admin', HOSPITAL_ADMIN, 'draft', ['Mark as sent', 'Cancel order']],
    ['someone who can only read orders', READ, 'draft', []],
    ['someone who can only read orders', READ, 'sent', []],
    ['someone who can receive but not send or cancel', RECEIVER, 'draft', []],
    ['someone who can receive but not send or cancel', RECEIVER, 'sent', ['Receive']],
    ['someone who can send and cancel but not receive', SENDER, 'draft', ['Mark as sent', 'Cancel order']],
    ['someone who can send and cancel but not receive', SENDER, 'sent', ['Cancel order']],
    ['someone who can send and cancel but not receive', SENDER, 'received', []],
  ] as const)('offers %s, on an order that is %s, exactly %j', async (_who, permissions, status, expected) => {
    orders = [order('o1', status, threeLines())]
    renderAt(ORDER, [...permissions])

    await screen.findByRole('heading', { level: 1, name: NUMBER })
    expect(actionNames()).toEqual(expected)
    // Opening the page writes nothing.
    expect(fake.sent.every((c) => c.method === 'get')).toBe(true)
  })

  it('does not offer Receive to someone who cannot read locations, says why, and asks for none', async () => {
    orders = [order('o1', 'sent', threeLines())]
    renderAt(ORDER, ['inventory.po.read', 'inventory.po.receive'])

    await screen.findByRole('heading', { level: 1, name: NUMBER })
    expect(actionNames()).toEqual([])
    expect(header().getByText(/your role cannot read the location list/)).toBeInTheDocument()
    expect(sent('get', '/inventory/locations')).toHaveLength(0)
    expect(fake.sent.map((c) => c.url)).toEqual([ORDER])
  })

  it('asks for no vendor list on behalf of a role that cannot read it, and says why no order can be drafted', async () => {
    orders = [order('o1', 'draft', threeLines())]
    // A custom role: it may raise orders, but was not given Pharmacy's vendor list.
    renderAt(LIST, ['inventory.po.read', 'inventory.po.create', 'inventory.item.read'])

    await screen.findByRole('row', { name: new RegExp(NUMBER) })
    expect(screen.queryByRole('combobox', { name: 'Filter by vendor' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New order/ })).not.toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(
      'drafting one needs the vendor list, which belongs to Pharmacy, and your role cannot read it.',
    )
    expect(screen.getByRole('note')).not.toHaveTextContent('the item list')
    // The status filter needs nothing but the order list itself.
    expect(screen.getByRole('combobox', { name: 'Filter by status' })).toBeInTheDocument()
    expect(sent('get', '/vendors')).toHaveLength(0)
    expect(sent('get', '/inventory/items')).toHaveLength(0)
    expect(fake.sent.map((c) => c.url)).toEqual([LIST])
  })

  it('likewise offers no new order without the item list, and asks for no items', async () => {
    renderAt(LIST, ['inventory.po.read', 'inventory.po.create', 'pharmacy.vendor.read'])

    expect(await screen.findByText('No purchase orders yet')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New order/ })).not.toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('drafting one needs the item list, and your role cannot read it.')
    expect(screen.getByRole('note')).not.toHaveTextContent('the vendor list')
    expect(sent('get', '/inventory/items')).toHaveLength(0)
  })

  it('names both lists when a role that may draft can read neither, and asks for neither', async () => {
    renderAt(LIST, ['inventory.po.read', 'inventory.po.create'])

    expect(await screen.findByText('No purchase orders yet')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New order/ })).not.toBeInTheDocument()
    // Being granted one must not uncover the other, unannounced.
    expect(screen.getByRole('note')).toHaveTextContent(
      'drafting one needs the vendor list, which belongs to Pharmacy, and the item list, and your role can read neither.',
    )
    expect(fake.sent.map((c) => c.url)).toEqual([LIST])
  })
})

// ── Order list ──────────────────────────────────────────────────────────────

describe('inventory purchase order list', () => {
  const two = () => [
    order('o2', 'sent', threeLines(), { po_number: 'IPO-2026-9BD41C07', created_at: '2026-10-04T08:00:00Z' }),
    order('o1', 'draft', [line(GLOVE, '10', '1.00')], { vendor_id: 'v2', vendor_name: 'Medline Wholesale' }),
  ]

  it("lists orders as the API returns them, with the server's total", async () => {
    orders = two()
    // Not what the lines add up to: the figure shown is the one the server sent.
    orders[0].total_amount = '13400.01'
    renderAt(LIST, INVENTORY_MANAGER)

    const first = await screen.findByRole('row', { name: /IPO-2026-9BD41C07/ })
    expect(within(first).getByRole('link', { name: 'IPO-2026-9BD41C07' })).toHaveAttribute('href', '/inventory/purchase-orders/o2')
    expect(within(first).getByText('Sanjeevani Pharma Distributors')).toBeInTheDocument()
    expect(within(first).getByText('3 lines')).toBeInTheDocument()
    expect(within(first).getByText('Nitrile gloves, medium, IV cannula 20G +1 more')).toBeInTheDocument()
    expect(within(first).getByText('13,400.01')).toBeInTheDocument()
    expect(within(first).getByText('Sent')).toBeInTheDocument()
    expect(within(first).getByText(formatDate('2026-10-04T08:00:00Z'))).toBeInTheDocument()
    expect(within(first).getByRole('link', { name: 'Open purchase order IPO-2026-9BD41C07' })).toHaveAttribute(
      'href',
      '/inventory/purchase-orders/o2',
    )
    const second = screen.getByRole('row', { name: new RegExp(NUMBER) })
    expect(within(second).getByText('1 line')).toBeInTheDocument()
    expect(within(second).getByText('10.00')).toBeInTheDocument()
    expect(within(second).getByText('Draft')).toBeInTheDocument()
    // Newest first is the server's order; no column offers to change it.
    expect(screen.queryByRole('button', { name: /sort/i })).not.toBeInTheDocument()

    const [request] = sent('get', LIST)
    expect(address(request)).toBe('/api/v1/inventory/purchase-orders')
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    // The vendor filter's own request, to Pharmacy's endpoint: every vendor, active or not.
    expect(sent('get', '/vendors').map(address)).toEqual(['/api/v1/vendors'])
    expect(sent('get', '/vendors').map(wire)).toEqual([{ page: 1, page_size: 100 }])
  })

  it('filters by status and vendor with the API\'s own names, leaving "all" out', async () => {
    orders = two()
    const user = userEvent.setup()
    renderAt(LIST, INVENTORY_MANAGER)
    await screen.findByRole('row', { name: new RegExp(NUMBER) })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Sent')
    await waitFor(() => expect(wire(sent('get', LIST).at(-1)!)).toEqual({ status: 'sent', page: 1, page_size: 25 }))
    await waitFor(() => expect(screen.queryByRole('row', { name: new RegExp(NUMBER) })).not.toBeInTheDocument())

    // Inactive vendors are offered too: their past orders are still there to find.
    await user.click(screen.getByRole('combobox', { name: 'Filter by vendor' }))
    expect(await screen.findByRole('option', { name: 'Apex Remedies' })).toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Medline Wholesale' }))
    await waitFor(() =>
      expect(wire(sent('get', LIST).at(-1)!)).toEqual({ status: 'sent', vendor_id: 'v2', page: 1, page_size: 25 }),
    )
    // Filters that leave nothing say so, differently from a list that has never had an order.
    expect(await screen.findByText('No matching purchase orders')).toBeInTheDocument()
    expect(screen.queryByText('No purchase orders yet')).not.toBeInTheDocument()

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'All statuses')
    await waitFor(() => expect(wire(sent('get', LIST).at(-1)!)).toEqual({ vendor_id: 'v2', page: 1, page_size: 25 }))
    expect(await screen.findByRole('row', { name: new RegExp(NUMBER) })).toBeInTheDocument()
    // `order_status` is the server's Python name; on the wire it would be silently ignored.
    expect(sent('get', LIST).some((c) => 'order_status' in wire(c))).toBe(false)
    // No filter was ever sent empty.
    expect(sent('get', LIST).every((c) => !Object.values(wire(c)).includes(''))).toBe(true)
  })

  it('offers every vendor in the filter, not only the first hundred', async () => {
    vendors = [
      ...Array.from({ length: 100 }, (_, n) => vendor(`a${n}`, `Alpha Supplier ${String(n).padStart(3, '0')}`)),
      vendor('z1', 'Zenith Surgicals'),
    ]
    const user = userEvent.setup()
    renderAt(LIST, INVENTORY_MANAGER)
    await screen.findByText('No purchase orders yet')

    await choose(user, screen.getByRole('combobox', { name: 'Filter by vendor' }), 'Zenith Surgicals')

    await waitFor(() => expect(wire(sent('get', LIST).at(-1)!)).toEqual({ vendor_id: 'z1', page: 1, page_size: 25 }))
    expect(sent('get', '/vendors').map(wire)).toEqual([
      { page: 1, page_size: 100 },
      { page: 2, page_size: 100 },
    ])
  })

  it('says so, and offers a retry, when the vendors for the filter cannot be loaded', async () => {
    orders = two()
    intercept = (c) => (c.url === '/vendors' ? fail(500, 'boom: vendors') : undefined)
    const user = userEvent.setup()
    renderAt(LIST, INVENTORY_MANAGER)

    // The orders themselves are unaffected.
    expect(await screen.findByRole('row', { name: new RegExp(NUMBER) })).toBeInTheDocument()
    const retry = await screen.findByRole('button', { name: /Vendors didn’t load — retry/ })
    expect(document.body.textContent).not.toMatch(/boom/)
    intercept = () => undefined
    await user.click(retry)

    await waitFor(() => expect(screen.queryByRole('button', { name: /didn’t load/ })).not.toBeInTheDocument())
    expect(sent('get', '/vendors')).toHaveLength(2)
  })

  it("pages through the server's pages", async () => {
    orders = Array.from({ length: 30 }, (_, n) =>
      order(`o${n}`, 'draft', [line(GLOVE, '10', '1.00')], { po_number: `IPO-2026-${(0xab000000 + n).toString(16).toUpperCase()}` }),
    )
    const user = userEvent.setup()
    renderAt(LIST, INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /IPO-2026-AB000000/ })
    expect(screen.queryByRole('row', { name: /IPO-2026-AB00001D/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() => expect(wire(sent('get', LIST).at(-1)!)).toEqual({ page: 2, page_size: 25 }))
    expect(await screen.findByRole('row', { name: /IPO-2026-AB00001D/ })).toBeInTheDocument()
  })

  it('goes back to the first page when a filter changes', async () => {
    orders = Array.from({ length: 30 }, (_, n) =>
      order(`o${n}`, 'draft', [line(GLOVE, '10', '1.00')], { po_number: `IPO-2026-${(0xab000000 + n).toString(16).toUpperCase()}` }),
    )
    const user = userEvent.setup()
    renderAt(LIST, INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /IPO-2026-AB000000/ })
    await user.click(screen.getByRole('button', { name: 'Next' }))
    await screen.findByRole('row', { name: /IPO-2026-AB00001D/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Draft')

    await waitFor(() => expect(wire(sent('get', LIST).at(-1)!)).toEqual({ status: 'draft', page: 1, page_size: 25 }))
  })

  it('shows skeleton rows while the list loads', async () => {
    orders = two()
    const release = hold((c) => c.url === LIST)
    renderAt(LIST, INVENTORY_MANAGER)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    expect(screen.queryByText('No purchase orders yet')).not.toBeInTheDocument()
    release()

    expect(await screen.findByRole('row', { name: new RegExp(NUMBER) })).toBeInTheDocument()
    expect(skeletons()).toBe(0)
  })

  it('explains itself when no order has ever been drafted', async () => {
    const view = renderAt(LIST, INVENTORY_MANAGER)
    expect(await screen.findByText('No purchase orders yet')).toBeInTheDocument()
    expect(screen.getByText(/receiving is what adds the items to stock/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /New order/ })).toHaveLength(2)
    view.unmount()

    // Someone who can only read orders cannot draft one, and is not told to.
    renderAt(LIST, ['inventory.po.read'])
    expect(await screen.findByText('No purchase orders yet')).toBeInTheDocument()
    expect(screen.getByText(/drafted by someone who manages purchasing/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New order/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it("offers a retry when the list cannot be loaded, without the server's text", async () => {
    intercept = (c) => (c.url === LIST ? fail(500, 'boom: timeout') : undefined)
    const user = userEvent.setup()
    renderAt(LIST, INVENTORY_MANAGER)

    expect(await screen.findByText("Couldn't load purchase orders")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    orders = two()
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: new RegExp(NUMBER) })).toBeInTheDocument()
  })
})

// ── Drafting an order ───────────────────────────────────────────────────────

describe('drafting an inventory purchase order', () => {
  const lineGroup = (d: HTMLElement, n: number) => within(d).getByRole('group', { name: `Line ${n}` })
  const pick = async (user: ReturnType<typeof userEvent.setup>, group: HTMLElement, name: RegExp) =>
    user.click(await within(group).findByRole('button', { name }))
  const quantity = (d: HTMLElement, n: number) => within(lineGroup(d, n)).getByLabelText(/^Quantity/)
  const price = (d: HTMLElement, n: number) => within(lineGroup(d, n)).getByLabelText(/^Purchase price per unit/)
  const drafts = () => sent('post', LIST)

  async function openDraft(user: ReturnType<typeof userEvent.setup>) {
    renderAt(LIST, INVENTORY_MANAGER)
    await screen.findByText('No purchase orders yet')
    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    return dialog()
  }

  /** Open the dialog and fill one complete, valid line. */
  async function startOrder(user: ReturnType<typeof userEvent.setup>) {
    const d = await openDraft(user)
    await choose(user, within(d).getByRole('combobox', { name: /Vendor/ }), 'Sanjeevani Pharma Distributors')
    await pick(user, lineGroup(d, 1), /^Nitrile gloves/)
    fill(quantity(d, 1), '40')
    fill(price(d, 1), '210.00')
    return d
  }

  it('drafts the order with exactly the body the API takes, and opens it', async () => {
    const user = userEvent.setup()
    const d = await openDraft(user)
    expect(d).toHaveTextContent('A draft cannot be edited afterwards — to change it, cancel it and draft another — so check')

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
    // The items asked for are the active ones; a withdrawn item is not offered.
    await pick(user, lineGroup(d, 1), /^Nitrile gloves/)
    expect(address(sent('get', '/inventory/items')[0])).toBe('/api/v1/inventory/items')
    expect(sent('get', '/inventory/items').map(wire)).toContainEqual({ is_active: true, page: 1, page_size: 8 })
    expect(within(d).queryByText(/Surgical mask/)).not.toBeInTheDocument()
    // Nothing is prefilled: an item has no price to copy.
    expect(price(d, 1)).toHaveValue('')
    fill(quantity(d, 1), '40')
    fill(price(d, 1), '210.00')

    await user.click(within(d).getByRole('button', { name: /Add an item/ }))
    await pick(user, lineGroup(d, 2), /^Sterile gauze roll/)
    // A fraction of a unit is a real quantity here.
    fill(quantity(d, 2), ' 12.5 ')
    fill(price(d, 2), '30')
    fill(within(d).getByLabelText(/^Notes/), '  Deliver before noon.  ')
    // No total is worked out in the browser.
    expect(d).toHaveTextContent("The order's totals are worked out by the server")
    expect(d).not.toHaveTextContent(/8,775|8775/)

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    // The number is the server's: nothing on the client could have produced it.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Drafted IPO-2026-C0FFEE01'))
    const [request] = drafts()
    expect(address(request)).toBe('/api/v1/inventory/purchase-orders')
    // Quantities and prices travel as the decimal text that was typed, never as numbers.
    expect(bodyOf(request)).toEqual({
      vendor_id: 'v1',
      notes: 'Deliver before noon.',
      items: [
        { item_id: 'it-GLOVE-M', quantity: '40', unit_price: '210.00' },
        { item_id: 'it-GAUZE-R', quantity: '12.5', unit_price: '30' },
      ],
    })
    // The new order's own page, with the server's figures.
    expect(await screen.findByRole('heading', { level: 1, name: 'IPO-2026-C0FFEE01' })).toBeInTheDocument()
    expect(header().getByText('Draft')).toBeInTheDocument()
    expect(screen.getAllByText('8,775.00').length).toBeGreaterThan(0)
    const gauze = screen.getByRole('row', { name: /Sterile gauze roll/ })
    expect(within(gauze).getByText('12.5')).toBeInTheDocument()
    expect(within(gauze).getByText('375.00')).toBeInTheDocument()
  })

  it('does not tell a drafter who cannot cancel to cancel the draft', async () => {
    const user = userEvent.setup()
    // A custom role: cancelling needs inventory.po.update, which drafting does not.
    renderAt(LIST, ['inventory.po.read', 'inventory.po.create', 'inventory.item.read', 'pharmacy.vendor.read'])
    await screen.findByText('No purchase orders yet')
    await user.click(screen.getAllByRole('button', { name: /New order/ })[0])
    const d = await dialog()

    expect(d).toHaveTextContent(
      'A draft cannot be edited afterwards — to change it, someone who manages purchasing has to cancel it, and another is drafted — so check',
    )
    expect(d).not.toHaveTextContent('cancel it and draft another')
  })

  it('leaves a blank note out of the body, and accepts a price of zero', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    fill(price(d, 1), '0')
    fill(within(d).getByLabelText(/^Notes/), '   ')

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    const body = bodyOf(drafts()[0])
    expect(body).toEqual({ vendor_id: 'v1', items: [{ item_id: 'it-GLOVE-M', quantity: '40', unit_price: '0' }] })
    expect(body).not.toHaveProperty('notes')
  })

  it('refuses an incomplete or malformed order before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openDraft(user)
    await waitFor(() => expect(within(d).getByRole('combobox', { name: /Vendor/ })).toBeEnabled())

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    expect(await within(d).findByText('Choose a vendor')).toBeInTheDocument()
    expect(within(d).getByText('Choose an item')).toBeInTheDocument()
    expect(within(d).getByText('More than 0, with at most two decimal places')).toBeInTheDocument()
    expect(within(d).getByText('Enter an amount such as 1.20')).toBeInTheDocument()

    // Zero, a third decimal, a negative, a thirteenth digit, and a price with three decimals.
    for (const bad of ['0', '0.00', '2.555', '-3', '10000000000', 'ten']) {
      fill(quantity(d, 1), bad)
      await user.click(within(d).getByRole('button', { name: 'Draft order' }))
      await waitFor(() => expect(quantity(d, 1)).toBeInvalid())
    }
    fill(quantity(d, 1), '9999999999.99')
    fill(price(d, 1), '1.999')
    await user.click(within(d).getByRole('button', { name: 'Draft order' }))
    await waitFor(() => expect(quantity(d, 1)).toBeValid())
    expect(price(d, 1)).toBeInvalid()

    fill(within(d).getByLabelText(/^Notes/), 'x'.repeat(2001))
    await user.click(within(d).getByRole('button', { name: 'Draft order' }))
    expect(await within(d).findByText('Keep the notes to 2,000 characters or fewer')).toBeInTheDocument()
    expect(drafts()).toHaveLength(0)
  })

  it('refuses a line whose total would not fit, which the server would answer with a bare 500', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    fill(quantity(d, 1), '9999999999')
    fill(price(d, 1), '9999999999999')

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    expect(await within(d).findByText("This line's total is more than can be recorded")).toBeInTheDocument()
    expect(drafts()).toHaveLength(0)
  })

  it('takes an item once: a second line cannot pick it again', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    await user.click(within(d).getByRole('button', { name: /Add an item/ }))

    // `CreateInventoryPurchaseOrderRequest` refuses an item listed twice.
    const again = await within(lineGroup(d, 2)).findByRole('button', { name: /^Nitrile gloves/ })
    expect(again).toBeDisabled()
    expect(again).toHaveTextContent('already added')
    await user.click(again)
    expect(within(lineGroup(d, 2)).queryByRole('button', { name: /Change item/ })).not.toBeInTheDocument()

    // A line can be removed again once there is more than one.
    await user.click(within(d).getByRole('button', { name: 'Remove line 2' }))
    expect(within(d).queryByRole('group', { name: 'Line 2' })).not.toBeInTheDocument()
    expect(within(d).queryByRole('button', { name: 'Remove line 1' })).not.toBeInTheDocument()
  })

  it('stops at fifty lines, which is all an order can hold', async () => {
    const user = userEvent.setup()
    const d = await openDraft(user)
    const add = within(d).getByRole('button', { name: /Add an item/ })
    for (let n = 1; n < 50; n += 1) fireEvent.click(add)

    await waitFor(() => expect(within(d).getAllByRole('group', { name: /^Line \d+$/ })).toHaveLength(50))
    expect(add).toBeDisabled()
    expect(d).toHaveTextContent('An order holds at most 50 lines.')
    await user.click(add)
    expect(within(d).getAllByRole('group', { name: /^Line \d+$/ })).toHaveLength(50)
  })

  it('shows a duplicate the server finds as a refusal of the whole list', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    // The request model's own rule, as it would answer if two lines named one item.
    intercept = (c) =>
      c.url === LIST && c.method === 'post'
        ? invalid('items', 'Value error, Each item may appear only once in an order.')
        : undefined

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    // Pydantic's "Value error, " prefix is not shown.
    expect(await within(d).findByText('Each item may appear only once in an order.')).toBeInTheDocument()
    expect(d).not.toHaveTextContent('Value error')
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('puts the refusal of an inactive vendor under the vendor field', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    // Switched off in Pharmacy while this form was open.
    vendors.find((v) => v.id === 'v1')!.is_active = false

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    const vendorSelect = within(d).getByRole('combobox', { name: /Vendor/ })
    await waitFor(() => expect(vendorSelect).toBeInvalid())
    expect(vendorSelect).toHaveAccessibleDescription("Vendor 'Sanjeevani Pharma Distributors' is inactive.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(orders).toHaveLength(0)
  })

  it('puts the refusal of an inactive item under that line', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    await user.click(within(d).getByRole('button', { name: /Add an item/ }))
    await pick(user, lineGroup(d, 2), /^Sterile gauze roll/)
    fill(quantity(d, 2), '5')
    fill(price(d, 2), '30.00')
    // Deactivated in the catalog after it was picked.
    items.find((i) => i.sku === 'GAUZE-R')!.is_active = false

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    // The service named `items.1.item_id`.
    expect(
      await within(lineGroup(d, 2)).findByText("Item 'GAUZE-R' is inactive and cannot be ordered."),
    ).toBeInTheDocument()
    expect(within(lineGroup(d, 1)).queryByText(/is inactive/)).not.toBeInTheDocument()
    expect(orders).toHaveLength(0)
  })

  it('puts a request-validation 422 under the field it names', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    intercept = (c) =>
      c.url === LIST && c.method === 'post' ? invalid('items.0.unit_price', 'Input should be greater than or equal to 0') : undefined

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    await waitFor(() => expect(price(d, 1)).toBeInvalid())
    expect(price(d, 1)).toHaveAccessibleDescription('Input should be greater than or equal to 0')
    expect(quantity(d, 1)).toBeValid()
  })

  it("shows the API's words when drafting is not permitted, and for a conflict", async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    intercept = (c) => (c.url === LIST && c.method === 'post' ? denied('inventory.po.create') : undefined)

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Permission denied. Required: inventory.po.create.')
    expect(toastError).toHaveBeenCalledWith('Permission denied. Required: inventory.po.create.')

    // Not an answer this endpoint gives today; if it ever does, its words are shown as they are.
    intercept = (c) =>
      c.url === LIST && c.method === 'post'
        ? fail(409, 'A purchase order with this number already exists.', { error_code: 'RESOURCE_CONFLICT', errors: null })
        : undefined
    await user.click(within(d).getByRole('button', { name: 'Draft order' }))
    await waitFor(() => expect(toastError).toHaveBeenCalledWith('A purchase order with this number already exists.'))
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('on a 500 says the draft is unconfirmed, never raw server text', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    intercept = (c) =>
      c.url === LIST && c.method === 'post' ? fail(500, 'boom: duplicate key uq_inventory_po_number') : undefined

    await user.click(within(d).getByRole('button', { name: 'Draft order' }))

    const shown =
      "Couldn't confirm that the order was drafted. Check the purchase order list before trying again, so it isn't drafted twice."
    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(document.body.textContent).not.toMatch(/boom|uq_inventory/)
  })

  it('drafts one order however many times the form is submitted', async () => {
    const user = userEvent.setup()
    const d = await startOrder(user)
    // The API would make a second draft from a second request: it has no key to tell them apart.
    const release = hold((c) => c.url === LIST && c.method === 'post')
    const form = within(d).getByRole('button', { name: 'Draft order' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Drafting…' })).toBeDisabled()
    await waitFor(() => expect(drafts()).toHaveLength(1))
    // The lines cannot be rearranged while the request they were sent in is in flight.
    expect(within(d).getByRole('button', { name: /Add an item/ })).toBeDisabled()
    fireEvent.submit(form)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(drafts()).toHaveLength(1)
    expect(orders).toHaveLength(1)
  })
})

// ── Order detail ────────────────────────────────────────────────────────────

describe('inventory purchase order detail', () => {
  it("shows the order with the server's quantities, prices and totals", async () => {
    orders = [order('o1', 'draft', threeLines(), { notes: 'Deliver before noon.' })]
    // Not what the lines add up to: the figure shown is the one the server sent.
    orders[0].total_amount = '13400.01'
    renderAt(ORDER, INVENTORY_MANAGER)

    await screen.findByRole('heading', { level: 1, name: NUMBER })
    expect(address(sent('get', ORDER)[0])).toBe('/api/v1/inventory/purchase-orders/o1')
    expect(header().getByText('Draft')).toBeInTheDocument()
    expect(header().getByText('Inventory purchase order to Sanjeevani Pharma Distributors')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to purchase orders/ })).toHaveAttribute('href', LIST)
    expect(screen.getByText('Deliver before noon.')).toBeInTheDocument()
    expect(screen.getAllByText('13,400.01')).toHaveLength(2)

    // Quantities lose the wire's padding and gain no precision; the line carries no unit.
    const gloves = screen.getByRole('row', { name: /Nitrile gloves, medium/ })
    expect(within(gloves).getByText('GLOVE-M')).toBeInTheDocument()
    expect(within(gloves).getByText('40')).toBeInTheDocument()
    expect(within(gloves).getByText('210.00')).toBeInTheDocument()
    expect(within(gloves).getByText('8,400.00')).toBeInTheDocument()
    const gauze = screen.getByRole('row', { name: /Sterile gauze roll/ })
    expect(within(gauze).getByText('12.5')).toBeInTheDocument()
    expect(within(gauze).getByText('375.00')).toBeInTheDocument()
    // Where the item's stock and the ledger can be read. The ledger link does not
    // claim to open on the item's own entries: that is the ledger page's to do.
    expect(within(gloves).getByRole('link', { name: 'Stock of Nitrile gloves, medium' })).toHaveAttribute(
      'href',
      '/inventory/stock?item_id=it-GLOVE-M',
    )
    const ledger = within(gloves).getByRole('link', { name: 'Movement ledger, to look up Nitrile gloves, medium' })
    expect(ledger).toHaveAttribute('href', '/inventory/movements?item_id=it-GLOVE-M')
    expect(ledger).toHaveTextContent('Movement ledger')
    expect(screen.queryByRole('link', { name: /^Movements of/ })).not.toBeInTheDocument()
    // A draft says it cannot be edited, and nothing offers to edit or delete it.
    expect(screen.getByRole('alert')).toHaveTextContent('An order cannot be edited')
    expect(screen.queryByRole('button', { name: /edit|delete/i })).not.toBeInTheDocument()
    // Nothing has been received, so no location is asked for.
    expect(sent('get', '/inventory/locations')).toHaveLength(0)
    expect(sent('get', '/vendors')).toHaveLength(0)
  })

  it('names an item without links for a role that cannot read stock', async () => {
    orders = [order('o1', 'received', threeLines())]
    renderAt(ORDER, ['inventory.po.read'])

    await screen.findByRole('heading', { level: 1, name: NUMBER })
    expect(screen.getByRole('row', { name: /Nitrile gloves, medium/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^Stock of/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^Movement/ })).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent("each item's stock, which your role cannot open")
    // The location's id is all the order carries, and this role cannot list locations.
    expect(screen.getByText('Not shown: your role cannot read locations')).toBeInTheDocument()
    expect(fake.sent.map((c) => c.url)).toEqual([ORDER])
  })

  it('says what "sent" does not mean', async () => {
    orders = [order('o1', 'sent', threeLines())]
    renderAt(ORDER, INVENTORY_MANAGER)

    await screen.findByRole('heading', { level: 1, name: NUMBER })
    const note = screen.getByRole('alert')
    expect(note).toHaveTextContent('“Sent” only records the status and the time')
    expect(note).toHaveTextContent('Aetheris does not transmit anything to the vendor')
    expect(note).toHaveTextContent('An order cannot be edited')
    expect(note).toHaveTextContent('an order is received once, in one go')
  })

  it('says a received order shows what was ordered, and names where it was received', async () => {
    orders = [order('o1', 'received', threeLines(), { received_location_id: 'loc-annexe' })]
    renderAt(ORDER, INVENTORY_MANAGER)

    await screen.findByRole('heading', { level: 1, name: NUMBER })
    const note = screen.getByRole('alert')
    expect(note).toHaveTextContent('The lines below still show what was ordered')
    expect(note).toHaveTextContent('What arrived — the quantities, the batches and their expiry dates — is not shown on the order')
    // The ledger is not promised already narrowed to the item: the user is told to choose it there.
    expect(note).toHaveTextContent(
      "What was added is in each item's stock and in the movement ledger: each line links to both. In the ledger, choose the item to see only its entries.",
    )
    expect(note).not.toHaveTextContent('use the links on each line')
    // The location has since been switched off; it is still named, so every location is asked for.
    expect(await screen.findByText('Old Annexe (ANNEXE)')).toBeInTheDocument()
    expect(sent('get', '/inventory/locations').map(address)).toEqual(['/api/v1/inventory/locations'])
    expect(sent('get', '/inventory/locations').map(wire)).toEqual([{}])
    // No "received" column is invented: the order returns only what was ordered.
    expect(screen.queryByRole('columnheader', { name: /received|delivered|arrived/i })).not.toBeInTheDocument()
  })

  it('says a cancelled order carries no reason and no time', async () => {
    orders = [order('o1', 'cancelled', threeLines(), { ordered_at: '2026-10-04T09:00:00Z' })]
    renderAt(ORDER, INVENTORY_MANAGER)

    await screen.findByRole('heading', { level: 1, name: NUMBER })
    const note = screen.getByRole('alert')
    expect(note).toHaveTextContent('The order keeps no reason and no time for its cancellation')
    expect(note).toHaveTextContent('It had been marked as sent before it was cancelled')
  })

  it('says an order is not found rather than failing', async () => {
    renderAt('/inventory/purchase-orders/missing', INVENTORY_MANAGER)

    expect(await screen.findByText('Purchase order not found')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to purchase orders/ })).toBeInTheDocument()
  })

  it('treats an id that is not a UUID as an order that does not exist', async () => {
    // What the API answers for a malformed path parameter: nothing a retry could change.
    intercept = (c) =>
      c.url === '/inventory/purchase-orders/not-a-uuid' ? invalid('path.order_id', 'Input should be a valid UUID') : undefined
    renderAt('/inventory/purchase-orders/not-a-uuid', INVENTORY_MANAGER)

    expect(await screen.findByText('Purchase order not found')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/connection|valid UUID|Validation failed/)
  })

  it('says a refused read is a matter of access, not of the connection', async () => {
    orders = [order('o1', 'sent', threeLines())]
    // The permission was taken away after the page's guard let the user in.
    intercept = (c) => (c.url === ORDER ? denied('inventory.po.read') : undefined)
    renderAt(ORDER, INVENTORY_MANAGER)

    expect(await screen.findByText("You can't open this purchase order")).toBeInTheDocument()
    expect(screen.getByText(/Your account is not allowed to read inventory purchase orders/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/connection/)
    expect(sent('get', ORDER)).toHaveLength(1)
  })

  it('shows a loading state, then offers a retry when the order cannot be loaded', async () => {
    orders = [order('o1', 'sent', threeLines())]
    let answer: (outcome: Outcome) => void = () => {}
    intercept = (c) => (c.url === ORDER ? new Promise<Outcome>((resolve) => (answer = resolve)) : undefined)
    const user = userEvent.setup()
    renderAt(ORDER, INVENTORY_MANAGER)

    expect(await screen.findByLabelText('Loading purchase order')).toHaveAttribute('aria-busy', 'true')
    answer(fail(500, 'boom: upstream'))

    expect(await screen.findByText("Couldn't load this purchase order")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('heading', { level: 1, name: NUMBER })).toBeInTheDocument()
  })
})

// ── Sending and cancelling ──────────────────────────────────────────────────

describe('sending and cancelling an inventory purchase order', () => {
  it('marks a draft as sent only after confirming, with no body', async () => {
    orders = [order('o1', 'draft', threeLines())]
    const user = userEvent.setup()
    renderAt(ORDER, INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: /Mark as sent/ }))
    const d = await dialog()

    expect(d).toHaveTextContent(`Records that ${NUMBER} has gone to Sanjeevani Pharma Distributors`)
    expect(d).toHaveTextContent('Aetheris does not transmit the order — send it to the vendor yourself')
    // Opening the dialog sends nothing.
    expect(fake.sent.filter((c) => c.method === 'post')).toHaveLength(0)

    await user.click(within(d).getByRole('button', { name: 'Mark as sent' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`${NUMBER} marked as sent`))
    const [request] = sent('post', `${ORDER}/send`)
    expect(address(request)).toBe('/api/v1/inventory/purchase-orders/o1/send')
    expect(request.data).toBeUndefined()
    expect(orders[0].status).toBe('sent')
    // The page shows the order the server answered with, and the steps it now offers.
    await waitFor(() => expect(header().getByText('Sent')).toBeInTheDocument())
    await waitFor(() => expect(actionNames()).toEqual(['Receive', 'Cancel order']))
  })

  it('keeps the order when the cancel dialog is dismissed, and cancels it for good when confirmed', async () => {
    orders = [order('o1', 'sent', threeLines())]
    const user = userEvent.setup()
    renderAt(ORDER, INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: /Cancel order/ }))
    let d = await dialog()
    expect(d).toHaveTextContent('a cancelled order cannot be reopened, sent or received')
    expect(d).toHaveTextContent('No reason is asked for')
    expect(d).toHaveTextContent('Aetheris does not tell the vendor')
    expect(within(d).queryByRole('textbox')).not.toBeInTheDocument()

    await user.click(within(d).getByRole('button', { name: 'Keep order' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(sent('post', `${ORDER}/cancel`)).toHaveLength(0)
    expect(orders[0].status).toBe('sent')

    await user.click(screen.getByRole('button', { name: /Cancel order/ }))
    d = await dialog()
    await user.click(within(d).getByRole('button', { name: 'Cancel order' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`${NUMBER} cancelled`))
    const [request] = sent('post', `${ORDER}/cancel`)
    expect(address(request)).toBe('/api/v1/inventory/purchase-orders/o1/cancel')
    expect(request.data).toBeUndefined()
    await waitFor(() => expect(header().getByText('Cancelled')).toBeInTheDocument())
    await waitFor(() => expect(actionNames()).toEqual([]))
    expect(stock).toHaveLength(0)
  })

  it.each([
    ['send', 'draft', 'Mark as sent', 'Mark as sent', 'Saving…'],
    ['cancel', 'draft', 'Cancel order', 'Cancel order', 'Cancelling…'],
  ] as const)('sends one "%s" however many times it is confirmed', async (step, from, open, confirm, pending) => {
    orders = [order('o1', from, threeLines())]
    const release = hold((c) => c.url === `${ORDER}/${step}`)
    const user = userEvent.setup()
    renderAt(ORDER, INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: open }))
    const d = await dialog()
    const form = within(d).getByRole('button', { name: confirm }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: pending })).toBeDisabled()
    expect(sent('post', `${ORDER}/${step}`)).toHaveLength(1)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', `${ORDER}/${step}`)).toHaveLength(1)
  })
})

// ── Receiving ───────────────────────────────────────────────────────────────

describe('receiving an inventory purchase order', () => {
  const GLOVES = 'Nitrile gloves, medium (GLOVE-M)'
  const CANNULAS = 'IV cannula 20G (CANN-20G)'
  const GAUZES = 'Sterile gauze roll (GAUZE-R)'
  const row = (d: HTMLElement, name: string) => within(d).getByRole('group', { name })
  const findRow = (d: HTMLElement, name: string) => within(d).findByRole('group', { name })
  const enter = (group: HTMLElement, values: { number?: string; expiry?: string; quantity?: string }) => {
    if (values.number !== undefined) fill(within(group).getByLabelText(/^Batch number/), values.number)
    if (values.expiry !== undefined) fill(within(group).getByLabelText(/^Expiry date/), values.expiry)
    if (values.quantity !== undefined) fill(within(group).getByLabelText(/^Quantity received/), values.quantity)
  }
  const tick = (d: HTMLElement) =>
    within(d).getByRole('checkbox', { name: 'I understand the rest of this order cannot be received later' })
  const submit = (d: HTMLElement) => within(d).getByRole('button', { name: 'Receive and close order' })
  const into = (d: HTMLElement) => within(d).getByRole('combobox', { name: /Receive into/ })
  const receipts = () => sent('post', `${ORDER}/receive`)

  async function openReceipt(user: ReturnType<typeof userEvent.setup>, permissions = INVENTORY_MANAGER) {
    renderAt(ORDER, permissions)
    await user.click(await screen.findByRole('button', { name: 'Receive' }))
    return dialog()
  }

  it('receives a split batch-tracked line, an untracked line and a line left out as one flat list', async () => {
    orders = [order('o1', 'sent', threeLines())]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    expect(d).toHaveTextContent('it is received once, in one go, and a receipt cannot be undone')
    expect(d).toHaveTextContent('3 of 50 rows entered')
    expect(d).toHaveTextContent('there is no second receipt')
    // Only active locations can receive, so only they are asked for and offered.
    await waitFor(() => expect(into(d)).toBeEnabled())
    expect(sent('get', '/inventory/locations').map(address)).toEqual(['/api/v1/inventory/locations'])
    expect(sent('get', '/inventory/locations').map(wire)).toEqual([{ is_active: true }])
    await user.click(into(d))
    expect(await screen.findByRole('option', { name: 'ICU Store (ICU)' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Old Annexe/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Main Store (MAIN)' }))

    // The order line does not say whether its item is batch-tracked or what its unit is: each item is read.
    const cannula1 = await findRow(d, `${CANNULAS}, batch 1`)
    expect(sent('get', '/inventory/items/it-CANN-20G').map(address)).toEqual(['/api/v1/inventory/items/it-CANN-20G'])
    expect(d).toHaveTextContent('Ordered 250 piece at 18.50 each. Batch-tracked')
    expect(d).toHaveTextContent('Ordered 40 box of 100 at 210.00 each. Not batch-tracked')
    expect(d).toHaveTextContent('Ordered 12.5 roll at 30.00 each. Not batch-tracked')
    // One row per line to start with, its quantity what was ordered.
    expect(within(cannula1).getByLabelText(/^Quantity received/)).toHaveValue('250')
    expect(within(row(d, `${GLOVES}, received`)).getByLabelText(/^Quantity received/)).toHaveValue('40')
    expect(within(row(d, `${GAUZES}, received`)).getByLabelText(/^Quantity received/)).toHaveValue('12.5')
    // An untracked item has no batch or expiry to enter, and cannot be split.
    expect(within(row(d, `${GLOVES}, received`)).queryByLabelText(/^Batch number/)).not.toBeInTheDocument()
    expect(within(row(d, `${GLOVES}, received`)).queryByLabelText(/^Expiry date/)).not.toBeInTheDocument()
    expect(within(d).queryByRole('button', { name: `Add another batch of ${GLOVES}` })).not.toBeInTheDocument()
    // One column on a phone: two would leave a date input about 150px at 400px wide.
    const grid = within(cannula1).getByLabelText(/^Expiry date/).parentElement!.parentElement!
    expect(grid).toHaveClass('grid', 'sm:grid-cols-2')
    expect(grid).not.toHaveClass('grid-cols-2')

    enter(cannula1, { number: 'cn-1', expiry: '2027-11-01', quantity: '200' })
    await user.click(within(d).getByRole('button', { name: `Add another batch of ${CANNULAS}` }))
    // The expiry is optional, as on the server.
    enter(row(d, `${CANNULAS}, batch 2`), { number: ' cn-2 ', quantity: '50' })
    await user.click(within(d).getByRole('button', { name: `Nothing arrived for ${GAUZES}` }))

    expect(d).toHaveTextContent('All 250 piece ordered are entered.')
    expect(d).toHaveTextContent('All 40 box of 100 ordered are entered.')
    expect(d).toHaveTextContent('Nothing arrived: this line is left out of the receipt, and the 12.5 roll ordered cannot be received later.')
    expect(d).toHaveTextContent('1 line is short or left out: Sterile gauze roll.')
    await user.click(tick(d))
    await user.click(submit(d))

    // The location is named from the order the server answered with.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`${NUMBER} received into Main Store`))
    const [request] = receipts()
    expect(address(request)).toBe('/api/v1/inventory/purchase-orders/o1/receive')
    const body = bodyOf(request) as { items: Record<string, unknown>[] }
    // po_item_id is the ORDER LINE's id, never the item's; quantities are text; batch numbers go in capitals.
    expect(body).toEqual({
      location_id: 'loc-main',
      items: [
        { po_item_id: 'i-GLOVE-M', quantity: '40' },
        { po_item_id: 'i-CANN-20G', quantity: '200', batch_number: 'CN-1', expiry_date: '2027-11-01' },
        { po_item_id: 'i-CANN-20G', quantity: '50', batch_number: 'CN-2' },
      ],
    })
    // Absent — not empty, not null: an untracked line with a batch number is a 422.
    expect(body.items[0]).not.toHaveProperty('batch_number')
    expect(body.items[0]).not.toHaveProperty('expiry_date')
    expect(body.items[2]).not.toHaveProperty('expiry_date')
    expect(JSON.stringify(body)).not.toMatch(/acknowledged|it-GAUZE-R|i-GAUZE-R/)
    expect(stock).toEqual([
      { item_id: 'it-GLOVE-M', location_id: 'loc-main', batch_number: null, expiry_date: null, quantity: '40.00' },
      { item_id: 'it-CANN-20G', location_id: 'loc-main', batch_number: 'CN-1', expiry_date: '2027-11-01', quantity: '200.00' },
      { item_id: 'it-CANN-20G', location_id: 'loc-main', batch_number: 'CN-2', expiry_date: null, quantity: '50.00' },
    ])

    await waitFor(() => expect(header().getByText('Received')).toBeInTheDocument())
    expect(await screen.findByText('Main Store (MAIN)')).toBeInTheDocument()
    // The order still says 12.5 gauze: it shows what was ordered, and says so.
    expect(within(screen.getByRole('row', { name: /Sterile gauze roll/ })).getByText('12.5')).toBeInTheDocument()
    expect(screen.getByText(/is not shown on the order/)).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual([]))
  })

  it('needs a location before anything is sent', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await findRow(d, `${GLOVES}, received`)
    await waitFor(() => expect(into(d)).toBeEnabled())

    await user.click(submit(d))

    await waitFor(() => expect(into(d)).toBeInvalid())
    expect(into(d)).toHaveAccessibleDescription('Choose where the goods are received into')
    expect(receipts()).toHaveLength(0)

    await choose(user, into(d), 'ICU Store (ICU)')
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`${NUMBER} received into ICU Store`))
    expect(bodyOf(receipts()[0])).toEqual({ location_id: 'loc-icu', items: [{ po_item_id: 'i-GLOVE-M', quantity: '40' }] })
  })

  it('says so when there is no active location to receive into', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    locations = [structuredClone(ANNEXE)]
    const user = userEvent.setup()
    const d = await openReceipt(user)

    expect(await within(d).findByText(/There is no active location to receive into/)).toBeInTheDocument()
    expect(within(d).getByRole('link', { name: 'Locations' })).toHaveAttribute('href', '/inventory/locations')
    expect(into(d)).toBeDisabled()
  })

  it('offers a retry when the locations cannot be loaded, without the server\'s text', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    intercept = (c) => (c.url === '/inventory/locations' ? fail(500, 'boom: locations') : undefined)
    const user = userEvent.setup()
    const d = await openReceipt(user)

    expect(await within(d).findByText("The location list couldn't be loaded.")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    intercept = () => undefined
    await user.click(within(d).getByRole('button', { name: /Retry/ }))

    await waitFor(() => expect(into(d)).toBeEnabled())
  })

  it('requires a batch number on every row of a batch-tracked item, before asking the server', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const batch1 = await findRow(d, `${CANNULAS}, batch 1`)
    await choose(user, into(d), 'Main Store (MAIN)')

    await user.click(submit(d))

    const number = within(batch1).getByLabelText(/^Batch number/)
    await waitFor(() => expect(number).toBeInvalid())
    expect(number).toHaveAccessibleDescription('Enter the batch number: this item is batch-tracked')
    // Marked as required, in words a screen reader gets too.
    expect(within(batch1).getByLabelText('Batch number*')).toBe(number)
    // The expiry is not required: the service accepts a batch without one.
    expect(within(batch1).getByLabelText(/^Expiry date/)).toBeValid()
    // "Never expires" is not true of a batch the location already holds with an expiry.
    expect(
      within(batch1).getByText(
        'Optional. A batch received without one is never treated as expired, unless this location already holds the batch with an expiry, which it keeps.',
      ),
    ).toBeInTheDocument()
    expect(receipts()).toHaveLength(0)

    enter(batch1, { number: 'cn-7' })
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(receipts()[0])).toEqual({
      location_id: 'loc-main',
      items: [{ po_item_id: 'i-CANN-20G', quantity: '250', batch_number: 'CN-7' }],
    })
  })

  it('will not close a short receipt until the shortfall as it stands is confirmed', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const gloves = await findRow(d, `${GLOVES}, received`)
    await choose(user, into(d), 'Main Store (MAIN)')
    // A complete receipt asks for no confirmation.
    expect(within(d).queryByRole('checkbox')).not.toBeInTheDocument()

    enter(gloves, { quantity: '30.5' })
    expect(d).toHaveTextContent('30.5 of 40 box of 100 entered — 9.5 box of 100 fewer than ordered, which cannot be received later.')
    expect(d).toHaveTextContent('1 line is short or left out: Nitrile gloves, medium.')
    await user.click(submit(d))

    expect(await within(d).findByText('Tick the box to confirm before the order is closed')).toBeInTheDocument()
    expect(receipts()).toHaveLength(0)

    // A tick is for the shortfall it was given for: change what is short and it is asked again.
    await user.click(tick(d))
    enter(gloves, { quantity: '20' })
    expect(tick(d)).not.toBeChecked()
    await user.click(submit(d))
    expect(await within(d).findByText('Tick the box to confirm before the order is closed')).toBeInTheDocument()
    expect(receipts()).toHaveLength(0)

    await user.click(tick(d))
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(receipts()[0])).toEqual({ location_id: 'loc-main', items: [{ po_item_id: 'i-GLOVE-M', quantity: '20' }] })
    // The order closes on the short receipt: the server does not compare the two.
    expect(orders[0].status).toBe('received')
  })

  it('says when more arrived than was ordered, and asks for no confirmation', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const gloves = await findRow(d, `${GLOVES}, received`)
    await choose(user, into(d), 'Main Store (MAIN)')

    enter(gloves, { quantity: '42.25' })

    expect(d).toHaveTextContent('42.25 box of 100 entered — 2.25 box of 100 more than the 40 ordered.')
    expect(within(d).queryByRole('checkbox')).not.toBeInTheDocument()
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(stock[0].quantity).toBe('42.25')
  })

  it('refuses malformed rows before asking the server', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const batch1 = await findRow(d, `${CANNULAS}, batch 1`)
    await choose(user, into(d), 'Main Store (MAIN)')
    const quantity = within(batch1).getByLabelText(/^Quantity received/)
    const number = within(batch1).getByLabelText(/^Batch number/)

    for (const bad of ['', '0', '1.234', '-5', 'ten']) {
      enter(batch1, { number: 'CN-1', quantity: bad })
      await user.click(submit(d))
      await waitFor(() => expect(quantity).toBeInvalid())
    }
    expect(quantity).toHaveAccessibleDescription('More than 0, with at most two decimal places')

    enter(batch1, { number: 'cn 1', quantity: '250' })
    await user.click(submit(d))
    await waitFor(() => expect(number).toBeInvalid())
    expect(number).toHaveAccessibleDescription('Letters, digits and - _ . / only, starting with a letter or digit')
    expect(quantity).toBeValid()

    enter(batch1, { number: 'C'.repeat(51) })
    await user.click(submit(d))
    await waitFor(() => expect(number).toHaveAccessibleDescription('Keep the batch number to 50 characters or fewer'))
    expect(receipts()).toHaveLength(0)
  })

  it('needs at least one row: a receipt of nothing is not a receipt', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await findRow(d, `${GLOVES}, received`)
    await choose(user, into(d), 'Main Store (MAIN)')

    await user.click(within(d).getByRole('button', { name: `Nothing arrived for ${GLOVES}` }))
    expect(d).toHaveTextContent('0 of 50 rows entered')
    await user.click(tick(d))
    await user.click(submit(d))

    expect(
      await within(d).findByText('Enter at least one line that arrived. An order nothing arrived for cannot be received.'),
    ).toBeInTheDocument()
    expect(receipts()).toHaveLength(0)

    // The line can be brought back, starting again from what was ordered.
    await user.click(within(d).getByRole('button', { name: `Record a delivery of ${GLOVES}` }))
    expect(within(await findRow(d, `${GLOVES}, received`)).getByLabelText(/^Quantity received/)).toHaveValue('40')
  })

  it('refuses the same batch number twice on one line, whatever its case', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const batch1 = await findRow(d, `${CANNULAS}, batch 1`)
    await choose(user, into(d), 'Main Store (MAIN)')
    enter(batch1, { number: 'cn-1', quantity: '100' })
    await user.click(within(d).getByRole('button', { name: `Add another batch of ${CANNULAS}` }))
    enter(row(d, `${CANNULAS}, batch 2`), { number: 'CN-1', quantity: '150' })

    await user.click(submit(d))

    const second = within(row(d, `${CANNULAS}, batch 2`)).getByLabelText(/^Batch number/)
    await waitFor(() => expect(second).toBeInvalid())
    expect(second).toHaveAccessibleDescription(
      'This batch is already listed for this item — enter it once, with its whole quantity',
    )
    expect(receipts()).toHaveLength(0)

    // The extra batch can be removed; a line's only row cannot.
    await user.click(within(d).getByRole('button', { name: `Remove ${CANNULAS}, batch 2` }))
    expect(within(d).queryByRole('group', { name: `${CANNULAS}, batch 2` })).not.toBeInTheDocument()
    expect(within(d).queryByRole('button', { name: `Remove ${CANNULAS}, batch 1` })).not.toBeInTheDocument()
  })

  it("puts the server's refusal of an expiry on the row it was sent as, not the row it sits at", async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00'), line(CANNULA, '250', '18.50')])]
    // The store already holds this batch, with a different expiry.
    stock = [{ item_id: 'it-CANN-20G', location_id: 'loc-main', batch_number: 'CN-9', expiry_date: '2027-08-01', quantity: '80.00' }]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const batch1 = await findRow(d, `${CANNULAS}, batch 1`)
    await choose(user, into(d), 'Main Store (MAIN)')

    // The gloves are left out, so the cannula rows are items 0 and 1 of what is sent.
    await user.click(within(d).getByRole('button', { name: `Nothing arrived for ${GLOVES}` }))
    enter(batch1, { number: 'cn-1', expiry: '2027-09-01', quantity: '100' })
    await user.click(within(d).getByRole('button', { name: `Add another batch of ${CANNULAS}` }))
    enter(row(d, `${CANNULAS}, batch 2`), { number: 'cn-9', expiry: '2028-01-01', quantity: '150' })
    await user.click(tick(d))
    await user.click(submit(d))

    // The service named `items.1.expiry_date`.
    const second = within(row(d, `${CANNULAS}, batch 2`)).getByLabelText(/^Expiry date/)
    await waitFor(() => expect(second).toBeInvalid())
    expect(second).toHaveAccessibleDescription('Batch CN-9 is already recorded as expiring 2027-08-01.')
    expect(within(row(d, `${CANNULAS}, batch 1`)).getByLabelText(/^Expiry date/)).toBeValid()
    expect((bodyOf(receipts()[0]) as { items: { batch_number: string }[] }).items.map((i) => i.batch_number)).toEqual(['CN-1', 'CN-9'])
    // Nothing was received: the order is still sent and the stock untouched.
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(orders[0].status).toBe('sent')
    expect(stock).toHaveLength(1)

    // With the expiry the batch already has, it is topped up.
    fill(second, '2027-08-01')
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`${NUMBER} received into Main Store`))
    expect(stock.find((s) => s.batch_number === 'CN-9')).toMatchObject({ quantity: '230.00', expiry_date: '2027-08-01' })
  })

  it('leaves the expiry to the server, and shows its refusal of a batch already expired', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const batch1 = await findRow(d, `${CANNULAS}, batch 1`)
    await choose(user, into(d), 'Main Store (MAIN)')
    // Yesterday on the hospital's calendar; the browser does not judge it.
    enter(batch1, { number: 'CN-1', expiry: '2026-10-04' })

    await user.click(submit(d))

    const expiry = within(batch1).getByLabelText(/^Expiry date/)
    await waitFor(() => expect(expiry).toBeInvalid())
    expect(expiry).toHaveAccessibleDescription('A batch that has already expired cannot be received.')
    expect(receipts()).toHaveLength(1)
    expect(stock).toHaveLength(0)
  })

  it('shows request-validation refusals of a row, of the row itself and of the whole list where they can be read', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    const batch1 = await findRow(d, `${CANNULAS}, batch 1`)
    await choose(user, into(d), 'Main Store (MAIN)')
    enter(batch1, { number: 'CN-1' })

    // A list at `errors` (Pydantic), naming one input.
    intercept = (c) =>
      c.url === `${ORDER}/receive` ? invalid('items.0.quantity', 'Input should be greater than 0') : undefined
    await user.click(submit(d))
    const quantity = within(batch1).getByLabelText(/^Quantity received/)
    await waitFor(() => expect(quantity).toHaveAccessibleDescription('Input should be greater than 0'))

    // The whole list.
    intercept = (c) =>
      c.url === `${ORDER}/receive` ? invalid('items', 'Value error, Each batch may be listed only once per order line.') : undefined
    await user.click(submit(d))
    expect(await within(d).findByText('Each batch may be listed only once per order line.')).toBeInTheDocument()

    // An object holding the list (the service), naming the row rather than an input.
    intercept = (c) => (c.url === `${ORDER}/receive` ? refused('items.0.po_item_id', 'Not an item on this order.') : undefined)
    await user.click(submit(d))
    expect(await within(batch1).findByText('Not an item on this order.')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(orders[0].status).toBe('sent')
  })

  it("puts the refusal of a location that has gone under the location field, and asks for the list again", async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await findRow(d, `${GLOVES}, received`)
    await choose(user, into(d), 'ICU Store (ICU)')
    locations = locations.filter((l) => l.id !== 'loc-icu')

    await user.click(submit(d))

    await waitFor(() => expect(into(d)).toBeInvalid())
    expect(into(d)).toHaveAccessibleDescription('Inventory location not found in this hospital.')
    await waitFor(() => expect(sent('get', '/inventory/locations')).toHaveLength(2))
    expect(stock).toHaveLength(0)
    // The location is gone from the list, so the choice is cleared — under the server's own reason.
    await waitFor(() => expect(into(d)).toHaveTextContent('Select a location'))
    expect(into(d)).toHaveAccessibleDescription('Inventory location not found in this hospital.')
    await user.click(submit(d))
    await waitFor(() => expect(into(d)).toHaveAccessibleDescription('Choose where the goods are received into'))
    expect(receipts()).toHaveLength(1)
  })

  it("shows the API's words when the location was switched off, and changes nothing", async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await findRow(d, `${GLOVES}, received`)
    await choose(user, into(d), 'ICU Store (ICU)')
    locations.find((l) => l.id === 'loc-icu')!.is_active = false

    await user.click(submit(d))

    const message = "Location 'ICU' is inactive and cannot receive stock."
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(message))
    // In the dialog's notice; the location field gains an alert of its own once the list is back.
    expect(await within(d).findByText(message)).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(orders[0].status).toBe('sent')
    expect(stock).toHaveLength(0)
    // The list of places to receive into is asked for again: it was stale.
    await waitFor(() => expect(sent('get', '/inventory/locations')).toHaveLength(2))
    expect(header().getByText('Sent')).toBeInTheDocument()

    // The refused location is no longer offered, so the form must not go on holding it:
    // the choice is cleared, the field says why, and submitting again sends nothing.
    await waitFor(() => expect(into(d)).toBeInvalid())
    expect(into(d)).toHaveAccessibleDescription('The location chosen can no longer receive stock. Choose another.')
    expect(into(d)).toHaveTextContent('Select a location')
    await user.click(submit(d))
    await waitFor(() => expect(into(d)).toHaveAccessibleDescription('Choose where the goods are received into'))
    expect(receipts()).toHaveLength(1)

    await choose(user, into(d), 'Main Store (MAIN)')
    expect(into(d)).toBeValid()
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith(`${NUMBER} received into Main Store`))
    expect(bodyOf(receipts()[1])).toEqual({ location_id: 'loc-main', items: [{ po_item_id: 'i-GLOVE-M', quantity: '40' }] })
  })

  it('tells the user when the order was already received, and catches up', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await findRow(d, `${GLOVES}, received`)
    await choose(user, into(d), 'Main Store (MAIN)')
    // Received at another desk while this form was open.
    Object.assign(orders[0], { status: 'received', received_at: '2026-10-05T06:45:00Z', received_location_id: 'loc-icu' })

    await user.click(submit(d))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Cannot receive a purchase order that is received.'))
    expect(toastSuccess).not.toHaveBeenCalled()
    // Nothing was added a second time, and the screen shows the order as it now is.
    expect(stock).toHaveLength(0)
    await waitFor(() => expect(header().getByText('Received')).toBeInTheDocument())
    expect(await screen.findByText('ICU Store (ICU)')).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual([]))
    expect(receipts()).toHaveLength(1)
  })

  it('leaves the batch rule to the server for someone who cannot read items, and asks for none', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00'), line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    // A custom role: it may receive, and may list locations, but not items.
    const d = await openReceipt(user, ['inventory.po.read', 'inventory.po.receive', 'inventory.location.read'])
    await choose(user, into(d), 'Main Store (MAIN)')

    expect(d).toHaveTextContent('Your role cannot read the item list, so whether this item is batch-tracked is not shown')
    // No unit either: the order line does not carry one.
    expect(d).toHaveTextContent('Ordered 250 at 18.50 each.')
    const cannula = row(d, `${CANNULAS}, batch 1`)
    // Not marked as required: this screen cannot know that it is.
    expect(within(cannula).getByLabelText('Batch number')).toBeInTheDocument()

    await user.click(submit(d))

    // The service named `items.1.batch_number`: the second row sent, which is the cannulas.
    const number = within(cannula).getByLabelText(/^Batch number/)
    await waitFor(() => expect(number).toBeInvalid())
    expect(number).toHaveAccessibleDescription('IV cannula 20G is batch-tracked: give a batch number.')
    expect(within(row(d, `${GLOVES}, batch 1`)).getByLabelText(/^Batch number/)).toBeValid()
    expect(stock).toHaveLength(0)

    enter(cannula, { number: 'cn-3' })
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(receipts()[1])).toEqual({
      location_id: 'loc-main',
      items: [
        { po_item_id: 'i-GLOVE-M', quantity: '40' },
        { po_item_id: 'i-CANN-20G', quantity: '250', batch_number: 'CN-3' },
      ],
    })
    expect(fake.sent.some((c) => (c.url ?? '').startsWith('/inventory/items'))).toBe(false)
  })

  it('says when an item could not be checked, and checks again on request', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    intercept = (c) => (c.url === '/inventory/items/it-CANN-20G' ? fail(500, 'boom: items') : undefined)
    const user = userEvent.setup()
    const d = await openReceipt(user)

    expect(await within(d).findByText(/Couldn't check whether this item is batch-tracked/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    expect(within(row(d, `${CANNULAS}, batch 1`)).getByLabelText('Batch number')).toBeInTheDocument()
    intercept = () => undefined
    await user.click(within(d).getByRole('button', { name: `Check ${CANNULAS} again` }))

    await waitFor(() => expect(d).toHaveTextContent('Batch-tracked: each batch that arrived is entered with its number.'))
    expect(within(row(d, `${CANNULAS}, batch 1`)).getByLabelText('Batch number*')).toBeInTheDocument()
  })

  it('does not send a batch typed for an item that turns out not to be batch-tracked', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    intercept = (c) => (c.url === '/inventory/items/it-GLOVE-M' ? fail(500, 'boom: items') : undefined)
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await within(d).findByText(/Couldn't check whether this item is batch-tracked/)
    await choose(user, into(d), 'Main Store (MAIN)')
    // Entered while the screen could not tell.
    enter(row(d, `${GLOVES}, batch 1`), { number: 'gl-1', expiry: '2027-01-01' })
    intercept = () => undefined
    await user.click(within(d).getByRole('button', { name: `Check ${GLOVES} again` }))

    // Now known: the inputs go, and what they held must neither be sent nor hold the receipt up.
    const gloves = await findRow(d, `${GLOVES}, received`)
    expect(within(gloves).queryByLabelText(/^Batch number/)).not.toBeInTheDocument()
    await user.click(submit(d))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(receipts()[0])).toEqual({ location_id: 'loc-main', items: [{ po_item_id: 'i-GLOVE-M', quantity: '40' }] })
    expect(stock).toEqual([
      { item_id: 'it-GLOVE-M', location_id: 'loc-main', batch_number: null, expiry_date: null, quantity: '40.00' },
    ])
  })

  it('never sends an expiry without a batch number when it cannot tell whether the item is batch-tracked', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    const user = userEvent.setup()
    // Cannot read items: the gloves are in fact untracked, and the server would stamp a lone expiry on their one stock row.
    const d = await openReceipt(user, ['inventory.po.read', 'inventory.po.receive', 'inventory.location.read'])
    await choose(user, into(d), 'Main Store (MAIN)')
    const gloves = row(d, `${GLOVES}, batch 1`)
    const expiry = within(gloves).getByLabelText(/^Expiry date/)
    expect(expiry).toHaveAccessibleDescription(
      /Recorded only with a batch number: an item that is not batch-tracked keeps no expiry\.$/,
    )
    enter(gloves, { expiry: '2027-01-01' })

    await user.click(submit(d))

    await waitFor(() => expect(expiry).toBeInvalid())
    expect(expiry).toHaveAccessibleDescription(
      'An expiry date is recorded only with a batch number: enter the batch number, or clear the date',
    )
    expect(receipts()).toHaveLength(0)

    enter(gloves, { expiry: '' })
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(receipts()[0])).toEqual({ location_id: 'loc-main', items: [{ po_item_id: 'i-GLOVE-M', quantity: '40' }] })
    expect(stock).toEqual([
      { item_id: 'it-GLOVE-M', location_id: 'loc-main', batch_number: null, expiry_date: null, quantity: '40.00' },
    ])
  })

  it('builds a body in which an expiry goes only with a batch number, whatever is known of the item', () => {
    const lines = [{ id: 'l1', item_id: 'it-1', quantity: '10.00' }]
    const values = (batch_number: string) => ({
      location_id: 'loc-main',
      items: [{ po_item_id: 'l1', quantity: '10', batch_number, expiry_date: '2027-01-01' }],
      acknowledged: '',
    })
    for (const tracked of [undefined, true]) {
      expect(toReceiptBody(values(''), lines, () => tracked).items).toEqual([{ po_item_id: 'l1', quantity: '10' }])
      expect(toReceiptBody(values('b1'), lines, () => tracked).items).toEqual([
        { po_item_id: 'l1', quantity: '10', batch_number: 'B1', expiry_date: '2027-01-01' },
      ])
    }
    expect(toReceiptBody(values('b1'), lines, () => false).items).toEqual([{ po_item_id: 'l1', quantity: '10' }])
  })

  it('refuses a second row of an item that turns out not to be batch-tracked, on the row, before asking the server', async () => {
    orders = [order('o1', 'sent', [line(GLOVE, '40', '210.00')])]
    intercept = (c) => (c.url === '/inventory/items/it-GLOVE-M' ? fail(500, 'boom: items') : undefined)
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await within(d).findByText(/Couldn't check whether this item is batch-tracked/)
    await choose(user, into(d), 'Main Store (MAIN)')
    // Split while the screen could not tell whether it may be.
    await user.click(within(d).getByRole('button', { name: `Add another batch of ${GLOVES}` }))
    enter(row(d, `${GLOVES}, batch 2`), { quantity: '5' })
    intercept = () => undefined
    await user.click(within(d).getByRole('button', { name: `Check ${GLOVES} again` }))

    // Now known to be untracked: two rows would be the same (line, no batch) twice — a 422 on the whole list.
    const second = await findRow(d, `${GLOVES}, row 2`)
    expect(within(d).queryByRole('button', { name: `Add another batch of ${GLOVES}` })).not.toBeInTheDocument()
    await user.click(submit(d))

    const quantity = within(second).getByLabelText(/^Quantity received/)
    await waitFor(() => expect(quantity).toBeInvalid())
    expect(quantity).toHaveAccessibleDescription(
      'This item is not batch-tracked: enter the whole quantity in one row, and remove this one',
    )
    expect(within(row(d, `${GLOVES}, row 1`)).getByLabelText(/^Quantity received/)).toBeValid()
    expect(receipts()).toHaveLength(0)

    await user.click(within(d).getByRole('button', { name: `Remove ${GLOVES}, row 2` }))
    await user.click(submit(d))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(receipts()).toHaveLength(1)
    expect(bodyOf(receipts()[0])).toEqual({ location_id: 'loc-main', items: [{ po_item_id: 'i-GLOVE-M', quantity: '40' }] })
  })

  it('stops at fifty rows across the whole receipt, and says so beforehand', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    await findRow(d, `${CANNULAS}, batch 1`)
    const add = within(d).getByRole('button', { name: `Add another batch of ${CANNULAS}` })
    for (let n = 1; n < 50; n += 1) fireEvent.click(add)

    await waitFor(() => expect(d).toHaveTextContent('50 of 50 rows entered'))
    expect(d).toHaveTextContent('This one is full.')
    expect(add).toBeDisabled()
    await user.click(add)
    expect(within(d).getAllByRole('group', { name: /, batch \d+$/ })).toHaveLength(50)
  })

  it('receives once however many times the form is submitted', async () => {
    orders = [order('o1', 'sent', [line(CANNULA, '250', '18.50')])]
    const user = userEvent.setup()
    const d = await openReceipt(user)
    enter(await findRow(d, `${CANNULAS}, batch 1`), { number: 'CN-1', expiry: '2027-08-01' })
    await choose(user, into(d), 'Main Store (MAIN)')
    // The API would put the delivery on the shelf twice if it were asked twice: it has no key to tell them apart.
    const release = hold((c) => c.url === `${ORDER}/receive`)
    const form = submit(d).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Receiving…' })).toBeDisabled()
    await waitFor(() => expect(receipts()).toHaveLength(1))
    // The rows cannot be rearranged while the request they were sent in is in flight.
    expect(within(d).getByRole('button', { name: `Add another batch of ${CANNULAS}` })).toBeDisabled()
    fireEvent.submit(form)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(receipts()).toHaveLength(1)
    expect(stock).toEqual([
      { item_id: 'it-CANN-20G', location_id: 'loc-main', batch_number: 'CN-1', expiry_date: '2027-08-01', quantity: '250.00' },
    ])
  })
})

// ── Refusals of a step ──────────────────────────────────────────────────────

describe('a step on an inventory purchase order that the server refuses', () => {
  const STEPS = {
    send: {
      from: 'draft',
      open: 'Mark as sent',
      confirm: 'Mark as sent',
      needs: 'inventory.po.update',
      fallback: "Couldn't mark the order as sent. Please try again.",
    },
    cancel: {
      from: 'sent',
      open: 'Cancel order',
      confirm: 'Cancel order',
      needs: 'inventory.po.update',
      fallback: "Couldn't cancel the order. Please try again.",
    },
    receive: {
      from: 'sent',
      open: 'Receive',
      confirm: 'Receive and close order',
      needs: 'inventory.po.receive',
      fallback:
        "Couldn't confirm that the order was received. Try again: an order is received only once, so nothing is added to stock twice.",
    },
  } as const
  type Step = keyof typeof STEPS
  const NAMES = Object.keys(STEPS) as Step[]

  /** Open the step's dialog on order o1, ready to confirm. */
  async function ready(user: ReturnType<typeof userEvent.setup>, step: Step) {
    orders = [order('o1', STEPS[step].from, [line(GLOVE, '40', '210.00')])]
    renderAt(ORDER, INVENTORY_MANAGER)
    await user.click(await screen.findByRole('button', { name: STEPS[step].open }))
    const d = await dialog()
    if (step === 'receive') {
      await within(d).findByRole('group', { name: 'Nitrile gloves, medium (GLOVE-M), received' })
      await choose(user, within(d).getByRole('combobox', { name: /Receive into/ }), 'Main Store (MAIN)')
    }
    return d
  }

  it.each(NAMES)('shows the API\'s words when "%s" is not permitted, and changes nothing', async (step) => {
    const { from, confirm, needs } = STEPS[step]
    const message = `Permission denied. Required: ${needs}.`
    const user = userEvent.setup()
    const d = await ready(user, step)
    // The role lost the permission after the page was drawn.
    intercept = (c) => (c.url === `${ORDER}/${step}` ? denied(needs) : undefined)

    await user.click(within(d).getByRole('button', { name: confirm }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(message)
    expect(toastError).toHaveBeenCalledWith(message)
    expect(toastSuccess).not.toHaveBeenCalled()
    const requests = sent('post', `${ORDER}/${step}`)
    expect(requests).toHaveLength(1)
    expect(address(requests[0])).toBe(`/api/v1/inventory/purchase-orders/o1/${step}`)
    expect(orders[0].status).toBe(from)
    expect(stock).toHaveLength(0)
    // Still open: the order is as it was, and is still shown as it was.
    expect(within(d).getByRole('button', { name: confirm })).toBeEnabled()
    expect(header().getByText(from === 'draft' ? 'Draft' : 'Sent')).toBeInTheDocument()
  })

  it.each(NAMES)('says so when the order had moved on before "%s" was confirmed, and shows it as it now is', async (step) => {
    const user = userEvent.setup()
    const d = await ready(user, step)
    // Cancelled from another screen while this dialog was open.
    orders[0].status = 'cancelled'

    await user.click(within(d).getByRole('button', { name: STEPS[step].confirm }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith(`Cannot ${step} a purchase order that is cancelled.`))
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(sent('post', `${ORDER}/${step}`)).toHaveLength(1)
    await waitFor(() => expect(header().getByText('Cancelled')).toBeInTheDocument())
    await waitFor(() => expect(actionNames()).toEqual([]))
    expect(stock).toHaveLength(0)
  })

  it.each(NAMES)('ends on "not found" when the order is gone by the time "%s" is confirmed', async (step) => {
    const user = userEvent.setup()
    const d = await ready(user, step)
    // Not something the API offers, but a row can vanish under a screen all the same.
    orders = []

    await user.click(within(d).getByRole('button', { name: STEPS[step].confirm }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Purchase order not found.'))
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(sent('post', `${ORDER}/${step}`)).toHaveLength(1)
    // The page asks again, and stops showing an order that is not there.
    expect(await screen.findByText('Purchase order not found')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(actionNames()).toEqual([])
    expect(stock).toHaveLength(0)
  })

  it.each(NAMES)('on a 500 from "%s" shows a message the user can act on, never raw server text', async (step) => {
    const { from, confirm, fallback } = STEPS[step]
    const user = userEvent.setup()
    const d = await ready(user, step)
    intercept = (c) => (c.url === `${ORDER}/${step}` ? fail(500, 'boom: lock wait timeout exceeded') : undefined)

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
    expect(sent('post', `${ORDER}/${step}`)).toHaveLength(2)
  })
})
