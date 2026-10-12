import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { InventoryItem, InventoryLocation, InventoryLocationKind } from '@/api/inventory'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import ItemsPage from './ItemsPage'
import LocationsPage from './LocationsPage'

/**
 * The item catalog and the stock locations, against the merged backend
 * contract (docs/18-API_CONTRACTS.md §10.3; `backend/app/api/v1/inventory.py`,
 * `schemas/inventory.py`, `services/inventory_service.py`). The real hooks,
 * permission check, `http` wrapper and Axios instance run; only the network
 * adapter is replaced by an in-memory "server" that keeps the items and the
 * locations and — like the real one — refuses what the contract refuses: a
 * call the caller's permissions do not cover, a SKU or a code in use, an
 * unknown or immutable key, a target below the reorder point, a record that
 * has gone.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → permissions as seeded in backend/app/seeds/seed.py (§10.1).
const EVERY_INVENTORY_CODE = [
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
const HOSPITAL_ADMIN = [...EVERY_INVENTORY_CODE, 'pharmacy.vendor.read', 'notification.read.own']
const SUPER_ADMIN = [...EVERY_INVENTORY_CODE, 'pharmacy.vendor.read', 'notification.read.own']
const INVENTORY_MANAGER = [...EVERY_INVENTORY_CODE, 'pharmacy.vendor.read', 'notification.read.own']
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
// Read-only.
const PHARMACIST = [
  'inventory.item.read',
  'inventory.location.read',
  'inventory.stock.read',
  'pharmacy.vendor.read',
  'notification.read.own',
]
// None of these holds an inventory code at all.
const DOCTOR = ['patient.read', 'appointment.read', 'notification.read.own']
const RECEPTIONIST = ['patient.read', 'appointment.read', 'invoice.read', 'notification.read.own']
const BILLING_STAFF = ['invoice.read', 'patient.read', 'notification.read.own']
const LAB_TECHNICIAN = ['lab.order.read', 'patient.read', 'notification.read.own']

// ── The in-memory server ────────────────────────────────────────────────────

function item(sku: string, name: string, extra: Partial<InventoryItem> = {}): InventoryItem {
  return {
    id: `i-${sku}`,
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

function location(code: string, name: string, kind: InventoryLocationKind, extra: Partial<InventoryLocation> = {}): InventoryLocation {
  return { id: `l-${code}`, name, code, kind, is_active: true, ...extra }
}

let items: InventoryItem[]
let locations: InventoryLocation[]
/** What the signed-in user holds, as the server sees it. */
let granted: string[]
/** Return an outcome to answer a request yourself; nothing to let the server answer. */
let intercept: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome> | undefined
let fake: FakeApi

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

const lacks = (code: string) =>
  granted.includes(code) ? undefined : fail(403, `Permission denied. Required: ${code}.`, { error_code: 'PERMISSION_DENIED' })

const CODE = /^[A-Z0-9][A-Z0-9_./-]*$/
const extraKey = (body: object, allowed: string[]) => Object.keys(body).find((k) => !allowed.includes(k))
const blankToNull = (value: unknown) => (typeof value === 'string' && value.trim() ? value.trim() : null)

/** A SKU or location code as `_code` treats one: length measured raw, then trimmed, uppercased and matched. */
function codeOf(body: Record<string, unknown>, field: 'sku' | 'code', label: string): string | Outcome {
  const value = body[field]
  if (typeof value !== 'string') return invalid(field, 'Field required')
  if (value.length < 1) return invalid(field, 'String should have at least 1 character')
  if (value.length > 50) return invalid(field, 'String should have at most 50 characters')
  const code = value.trim().toUpperCase()
  if (!CODE.test(code)) {
    return invalid(field, `Value error, ${label} may contain only letters, digits and the characters - _ . /`)
  }
  return code
}

/** Required text (`_strip_required`): length measured raw, blank refused. Undefined when the key is absent or fine. */
function textError(body: Record<string, unknown>, field: string, max: number, label: string): Outcome | undefined {
  const value = body[field]
  if (value === undefined) return undefined
  if (typeof value !== 'string') return invalid(field, 'Input should be a valid string')
  if (value.length < 1) return invalid(field, 'String should have at least 1 character')
  if (value.length > max) return invalid(field, `String should have at most ${max} characters`)
  if (!value.trim()) return invalid(field, `Value error, ${label} must not be blank.`)
  return undefined
}

/** `reorder_point` / `target_stock`: an integer from 0 to ten million, or null. */
function levelError(body: Record<string, unknown>, field: string): Outcome | undefined {
  const value = body[field]
  if (value === undefined || value === null) return undefined
  if (typeof value !== 'number' || !Number.isInteger(value)) return invalid(field, 'Input should be a valid integer')
  if (value < 0) return invalid(field, 'Input should be greater than or equal to 0')
  if (value > 10_000_000) return invalid(field, 'Input should be less than or equal to 10000000')
  return undefined
}

const KINDS = ['ward', 'ot', 'icu', 'store']

function server(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const method = config.method ?? 'get'
  const params = (config.params ?? {}) as Record<string, unknown>

  // An empty query value is not "no filter": a boolean fails to parse, and an
  // empty category is matched literally.
  const empty = Object.keys(params).find((key) => params[key] === '')
  if (empty) return invalid(`query.${empty}`, 'Input should be a valid value')

  // ── Items ──
  if (url === '/inventory/items' && method === 'get') {
    const denied = lacks('inventory.item.read')
    if (denied) return denied
    // A prefix of the name, or the whole SKU; neither `q` nor `category` is trimmed.
    const q = typeof params.q === 'string' ? params.q : undefined
    if (q !== undefined && q.length > 200) return invalid('query.q', 'String should have at most 200 characters')
    return pageOf(
      items
        .filter((i) => !q || i.name.toLowerCase().startsWith(q.toLowerCase()) || i.sku === q.toUpperCase())
        .filter((i) => params.category === undefined || i.category === params.category)
        .filter((i) => params.is_active === undefined || i.is_active === params.is_active)
        .sort((a, b) => a.name.localeCompare(b.name)),
      config,
    )
  }
  if (url === '/inventory/items' && method === 'post') {
    const denied = lacks('inventory.item.create')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const extra = extraKey(body, ['sku', 'name', 'category', 'unit_of_measure', 'is_batch_tracked', 'reorder_point', 'target_stock'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    const sku = codeOf(body, 'sku', 'SKU')
    if (typeof sku !== 'string') return sku
    if (typeof body.name !== 'string') return invalid('name', 'Field required')
    const wrong =
      textError(body, 'name', 200, 'Name and unit of measure') ??
      textError(body, 'unit_of_measure', 30, 'Name and unit of measure') ??
      levelError(body, 'reorder_point') ??
      levelError(body, 'target_stock')
    if (wrong) return wrong
    if (typeof body.category === 'string' && body.category.length > 100) {
      return invalid('category', 'String should have at most 100 characters')
    }
    const reorder = (body.reorder_point ?? null) as number | null
    const target = (body.target_stock ?? null) as number | null
    // A rule of the whole request model: it names no field.
    if (reorder !== null && target !== null && target < reorder) {
      return invalid('', 'Value error, target_stock must not be less than reorder_point.')
    }
    if (items.some((i) => i.sku === sku)) {
      return fail(409, `An item with SKU '${sku}' already exists.`, { error_code: 'RESOURCE_CONFLICT', errors: { sku } })
    }
    const added = item(sku, body.name.trim(), {
      category: blankToNull(body.category),
      unit_of_measure: typeof body.unit_of_measure === 'string' ? body.unit_of_measure.trim() : 'unit',
      is_batch_tracked: body.is_batch_tracked === true,
      reorder_point: reorder,
      target_stock: target,
    })
    items.push(added)
    return ok(structuredClone(added), 201)
  }
  const itemPath = /^\/inventory\/items\/([^/]+)$/.exec(url)
  if (itemPath && method === 'patch') {
    const denied = lacks('inventory.item.update')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    // `sku` and `is_batch_tracked` are not in the request model at all.
    const extra = extraKey(body, ['name', 'category', 'unit_of_measure', 'reorder_point', 'target_stock', 'is_active'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    const wrong =
      textError(body, 'name', 200, 'Name and unit of measure') ??
      textError(body, 'unit_of_measure', 30, 'Name and unit of measure') ??
      levelError(body, 'reorder_point') ??
      levelError(body, 'target_stock')
    if (wrong) return wrong
    const nulled = ['name', 'unit_of_measure', 'is_active'].filter((k) => body[k] === null).sort()
    if (nulled.length > 0) return invalid('', `Value error, Cannot be null: ${nulled.join(', ')}.`)
    const found = items.find((i) => i.id === itemPath[1])
    if (!found) {
      return fail(404, 'Inventory item not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { item_id: itemPath[1] } })
    }
    // Judged against what is saved, not what the form was opened with.
    const reorder = 'reorder_point' in body ? (body.reorder_point as number | null) : found.reorder_point
    const target = 'target_stock' in body ? (body.target_stock as number | null) : found.target_stock
    if (reorder !== null && target !== null && target < reorder) {
      return refused('target_stock', 'target_stock must not be less than reorder_point.')
    }
    if ('name' in body) found.name = (body.name as string).trim()
    if ('category' in body) found.category = blankToNull(body.category)
    if ('unit_of_measure' in body) found.unit_of_measure = (body.unit_of_measure as string).trim()
    if ('reorder_point' in body) found.reorder_point = reorder
    if ('target_stock' in body) found.target_stock = target
    if ('is_active' in body) found.is_active = body.is_active as boolean
    return ok(structuredClone(found))
  }

  // ── Locations ──
  if (url === '/inventory/locations' && method === 'get') {
    const denied = lacks('inventory.location.read')
    if (denied) return denied
    // A plain list: no page, no metadata.
    return ok(
      locations
        .filter((l) => params.is_active === undefined || l.is_active === params.is_active)
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((l) => structuredClone(l)),
    )
  }
  if (url === '/inventory/locations' && method === 'post') {
    const denied = lacks('inventory.location.create')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const extra = extraKey(body, ['name', 'code', 'kind'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    if (typeof body.name !== 'string') return invalid('name', 'Field required')
    const wrong = textError(body, 'name', 200, 'Name')
    if (wrong) return wrong
    const code = codeOf(body, 'code', 'Code')
    if (typeof code !== 'string') return code
    if (body.kind !== undefined && !KINDS.includes(body.kind as string)) {
      return invalid('kind', "Input should be 'ward', 'ot', 'icu' or 'store'")
    }
    if (locations.some((l) => l.code === code)) {
      return fail(409, `A location with code '${code}' already exists.`, { error_code: 'RESOURCE_CONFLICT', errors: { code } })
    }
    const added = location(code, body.name.trim(), (body.kind as InventoryLocationKind | undefined) ?? 'store')
    locations.push(added)
    return ok(structuredClone(added), 201)
  }
  const locationPath = /^\/inventory\/locations\/([^/]+)$/.exec(url)
  if (locationPath && method === 'patch') {
    const denied = lacks('inventory.location.update')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    // `code` is not in the request model at all.
    const extra = extraKey(body, ['name', 'kind', 'is_active'])
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    const wrong = textError(body, 'name', 200, 'Name')
    if (wrong) return wrong
    if (body.kind !== undefined && body.kind !== null && !KINDS.includes(body.kind as string)) {
      return invalid('kind', "Input should be 'ward', 'ot', 'icu' or 'store'")
    }
    const nulled = ['name', 'kind', 'is_active'].filter((k) => body[k] === null).sort()
    if (nulled.length > 0) return invalid('', `Value error, Cannot be null: ${nulled.join(', ')}.`)
    const found = locations.find((l) => l.id === locationPath[1])
    if (!found) {
      return fail(404, 'Inventory location not found.', {
        error_code: 'RESOURCE_NOT_FOUND',
        errors: { location_id: locationPath[1] },
      })
    }
    // Whatever the location holds: the service has no check on its stock.
    if ('name' in body) found.name = (body.name as string).trim()
    if ('kind' in body) found.kind = body.kind as InventoryLocationKind
    if ('is_active' in body) found.is_active = body.is_active as boolean
    return ok(structuredClone(found))
  }

  return fail(404, 'Not Found')
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  // The demo catalog (backend/app/seeds/demo_inventory.py).
  items = [
    item('GLOVE-M', 'Nitrile gloves, medium', { unit_of_measure: 'box of 100', reorder_point: 20, target_stock: 80 }),
    item('SYR-5', 'Syringe 5 mL', { unit_of_measure: 'box of 50', reorder_point: 20, target_stock: 60 }),
    item('MASK-3P', 'Surgical mask, 3-ply', { unit_of_measure: 'box of 50', reorder_point: 30, target_stock: 100 }),
    item('CANN-20G', 'IV cannula 20G', { category: 'Sterile', is_batch_tracked: true, reorder_point: 100, target_stock: 400 }),
    item('GAUZE-ST', 'Sterile gauze swab', { category: 'Sterile', unit_of_measure: 'pack of 10', is_batch_tracked: true, reorder_point: 50, target_stock: 200 }),
    item('O2-B', 'Oxygen cylinder, B type', { category: null, unit_of_measure: 'cylinder' }),
    item('SHEET-S', 'Bed sheet, single', { category: 'Linen', reorder_point: 40, target_stock: 120 }),
    item('GLOVE-LX', 'Latex gloves (withdrawn)', { unit_of_measure: 'box of 100', is_active: false }),
  ]
  locations = [
    location('STORE', 'General store', 'store'),
    location('WARD-A', 'Ward A', 'ward'),
    location('ICU', 'Intensive care unit', 'icu'),
    location('OT-1', 'Operating theatre 1', 'ot'),
    location('WARD-Z', 'Ward Z (closed)', 'ward', { is_active: false }),
  ]
  granted = []
  intercept = () => undefined
  fake = installFakeApi(async (config) => (await intercept(config)) ?? server(config))
})

afterEach(() => {
  fake.restore()
  signOut()
})

function renderAt(path: string, permissions: string[]) {
  granted = [...permissions]
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/dashboard" element={<p>Dashboard home</p>} />
          <Route path="/inventory/stock" element={<p>Stock page</p>} />
          <Route
            path="/inventory/items"
            element={
              <RequirePermission permission="inventory.item.read">
                <ItemsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/inventory/locations"
            element={
              <RequirePermission permission="inventory.location.read">
                <LocationsPage />
              </RequirePermission>
            }
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const ITEMS = '/inventory/items'
const LOCATIONS = '/inventory/locations'
/** Requests to exactly this path. */
const sent = (method: string, url: string) => fake.sent.filter((c) => c.method === method && c.url === url)
const writes = () => fake.sent.filter((c) => c.method !== 'get')
const address = (config: InternalAxiosRequestConfig) => `${config.baseURL}${config.url}`
/** The query as it goes on the wire: a key left undefined is not sent at all. */
const wire = (config: InternalAxiosRequestConfig) => JSON.parse(JSON.stringify(config.params ?? {})) as Record<string, unknown>
const dialog = () => screen.findByRole('dialog')
const fill = (input: HTMLElement, value: string) => fireEvent.change(input, { target: { value } })
const skeletons = () => document.querySelectorAll('[data-slot="skeleton"]').length
const bodyRows = () => screen.getAllByRole('row').slice(1)
const cellsOf = (row: HTMLElement) => within(row).getAllByRole('cell').map((cell) => cell.textContent)
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
const SEARCH = 'Search by start of name or whole SKU…'
const searchBox = () => screen.getByRole('textbox', { name: SEARCH })
const categoryBox = () => screen.getByRole('textbox', { name: 'Filter by exact category' })

async function openAddItem(user: ReturnType<typeof userEvent.setup>, permissions = INVENTORY_MANAGER) {
  renderAt(ITEMS, permissions)
  await screen.findByRole('row', { name: /Syringe 5 mL/ })
  await user.click(screen.getByRole('button', { name: /Add item/ }))
  return dialog()
}

async function openEditItem(user: ReturnType<typeof userEvent.setup>, name: string) {
  renderAt(ITEMS, INVENTORY_MANAGER)
  await user.click(await screen.findByRole('button', { name: `Edit ${name}` }))
  return dialog()
}

async function openAddLocation(user: ReturnType<typeof userEvent.setup>) {
  renderAt(LOCATIONS, INVENTORY_MANAGER)
  await screen.findByRole('row', { name: /General store/ })
  await user.click(screen.getByRole('button', { name: /Add location/ }))
  return dialog()
}

async function openEditLocation(user: ReturnType<typeof userEvent.setup>, name: string) {
  renderAt(LOCATIONS, INVENTORY_MANAGER)
  await user.click(await screen.findByRole('button', { name: `Edit ${name}` }))
  return dialog()
}

// ── Access ──────────────────────────────────────────────────────────────────

describe('catalog access', () => {
  it.each([
    ['a doctor', DOCTOR],
    ['a receptionist', RECEPTIONIST],
    ['billing staff', BILLING_STAFF],
    ['a lab technician', LAB_TECHNICIAN],
  ])('sends %s away from both lists without asking the API anything', async (_role, permissions) => {
    const view = renderAt(ITEMS, permissions)
    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    view.unmount()

    renderAt(LOCATIONS, permissions)
    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    expect(fake.sent).toHaveLength(0)
  })

  it.each([
    ['a nurse', NURSE],
    ['a pharmacist', PHARMACIST],
  ])('shows %s the item catalog read-only, with the way to its stock', async (_role, permissions) => {
    renderAt(ITEMS, permissions)

    const row = await screen.findByRole('row', { name: /Nitrile gloves, medium/ })
    expect(within(row).getByRole('link', { name: 'Stock of Nitrile gloves, medium' })).toHaveAttribute(
      'href',
      '/inventory/stock?item_id=i-GLOVE-M',
    )
    expect(screen.queryByRole('button', { name: /Add item/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Edit / })).not.toBeInTheDocument()
    // One read of the list, and nothing the role's permissions do not cover.
    expect(fake.sent.map((c) => `${c.method} ${c.url}`)).toEqual(['get /inventory/items'])
  })

  it.each([
    ['a nurse', NURSE],
    ['a pharmacist', PHARMACIST],
  ])('shows %s the locations read-only, with the way to their stock', async (_role, permissions) => {
    renderAt(LOCATIONS, permissions)

    const row = await screen.findByRole('row', { name: /Ward A/ })
    expect(within(row).getByRole('link', { name: 'Stock at Ward A' })).toHaveAttribute(
      'href',
      '/inventory/stock?location_id=l-WARD-A',
    )
    expect(screen.queryByRole('button', { name: /Add location/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Edit / })).not.toBeInTheDocument()
    expect(fake.sent.map((c) => `${c.method} ${c.url}`)).toEqual(['get /inventory/locations'])
  })

  it.each([
    ['an inventory manager', INVENTORY_MANAGER],
    ['a hospital admin', HOSPITAL_ADMIN],
    ['a super admin', SUPER_ADMIN],
  ])('offers %s adding and editing on both lists', async (_role, permissions) => {
    const view = renderAt(ITEMS, permissions)
    await screen.findByRole('row', { name: /Nitrile gloves, medium/ })
    expect(screen.getByRole('button', { name: /Add item/ })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Edit / })).toHaveLength(8)
    expect(screen.getAllByRole('link', { name: /^Stock of / })).toHaveLength(8)
    view.unmount()

    renderAt(LOCATIONS, permissions)
    await screen.findByRole('row', { name: /Ward A/ })
    expect(screen.getByRole('button', { name: /Add location/ })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Edit / })).toHaveLength(5)
    expect(screen.getAllByRole('link', { name: /^Stock at / })).toHaveLength(5)
    // Opening the lists writes nothing.
    expect(writes()).toHaveLength(0)
  })

  it('decides each action by its own permission, not by another the user happens to hold', async () => {
    // Not a seeded role: reads the catalog, may add to it, may not edit it or read stock.
    const view = renderAt(ITEMS, ['inventory.item.read', 'inventory.item.create', 'inventory.location.read', 'inventory.location.update'])
    await screen.findByRole('row', { name: /Nitrile gloves, medium/ })
    expect(screen.getByRole('button', { name: /Add item/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Edit / })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^Stock of / })).not.toBeInTheDocument()
    view.unmount()

    renderAt(LOCATIONS, ['inventory.item.read', 'inventory.item.create', 'inventory.location.read', 'inventory.location.update'])
    await screen.findByRole('row', { name: /Ward A/ })
    expect(screen.queryByRole('button', { name: /Add location/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^Edit / })).toHaveLength(5)
    expect(screen.queryByRole('link', { name: /^Stock at / })).not.toBeInTheDocument()
  })

  it('lists the sections a nurse may open, and not purchase orders', async () => {
    renderAt(ITEMS, NURSE)
    await screen.findByRole('row', { name: /Nitrile gloves, medium/ })

    const tabs = within(screen.getByRole('navigation', { name: 'Inventory sections' }))
    expect(tabs.getAllByRole('link').map((a) => a.textContent)).toEqual(['Overview', 'Stock', 'Movements', 'Items', 'Locations'])
  })
})

// ── Items ───────────────────────────────────────────────────────────────────

describe('items', () => {
  it('lists items as the API returns them, their levels as settings and no stock figure', async () => {
    renderAt(ITEMS, INVENTORY_MANAGER)

    const row = await screen.findByRole('row', { name: /Nitrile gloves, medium/ })
    expect(cellsOf(row).slice(0, 8)).toEqual(['Nitrile gloves, medium', 'GLOVE-M', 'Disposables', 'box of 100', 'No', '20', '80', 'Active'])
    expect(within(row).getByText('GLOVE-M')).toHaveClass('font-mono')
    // Batch-tracked; and an item with no category, reorder point or target.
    expect(cellsOf(screen.getByRole('row', { name: /IV cannula 20G/ })).slice(0, 8)).toEqual([
      'IV cannula 20G', 'CANN-20G', 'Sterile', 'piece', 'Yes', '100', '400', 'Active',
    ])
    expect(cellsOf(screen.getByRole('row', { name: /Oxygen cylinder/ })).slice(0, 8)).toEqual([
      'Oxygen cylinder, B type', 'O2-B', '—', 'cylinder', 'No', '—', '—', 'Active',
    ])
    expect(within(screen.getByRole('row', { name: /Latex gloves/ })).getByText('Inactive')).toBeInTheDocument()
    // Name order is the server's.
    expect(bodyRows().map((r) => cellsOf(r)[1])).toEqual([
      'SHEET-S', 'CANN-20G', 'GLOVE-LX', 'GLOVE-M', 'O2-B', 'GAUZE-ST', 'MASK-3P', 'SYR-5',
    ])
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Item', 'SKU', 'Category', 'Unit', 'Batch tracked', 'Reorder point (setting)', 'Target stock (setting)', 'Status', '',
    ])
    // No column a reader could take for stock on hand, and no per-row sorting the API cannot do.
    expect(screen.queryByRole('columnheader', { name: /on hand|in stock|low/i })).not.toBeInTheDocument()
    expect(within(screen.getAllByRole('row')[0]).queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Inventory sections' })).toBeInTheDocument()

    const [request] = sent('get', ITEMS)
    expect(address(request)).toBe('/api/v1/inventory/items')
    // No empty filter is sent, and nothing but the list is read: no stock per row.
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    expect(fake.sent).toHaveLength(1)
  })

  it('sends the search trimmed, once the typing stops', async () => {
    let searchedAt = 0
    intercept = (c) => {
      if (c.url === ITEMS && 'q' in wire(c)) searchedAt = performance.now()
      return undefined
    }
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Syringe 5 mL/ })

    const typedAt = performance.now()
    fill(searchBox(), '  nit')
    fill(searchBox(), '  nitr  ')
    expect(sent('get', ITEMS)).toHaveLength(1)

    // The server does not trim: sent as typed, this would match nothing.
    await waitFor(() => expect(screen.queryByRole('row', { name: /Syringe 5 mL/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Nitrile gloves, medium/ })).toBeInTheDocument()
    // One search, for what was left in the box, and not before the pause.
    expect(sent('get', ITEMS).map(wire)).toEqual([
      { page: 1, page_size: 25 },
      { q: 'nitr', page: 1, page_size: 25 },
    ])
    expect(searchedAt - typedAt).toBeGreaterThanOrEqual(250)
  })

  it('finds by whole SKU, says why part of one finds nothing, and keeps q within 200 characters', async () => {
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Syringe 5 mL/ })

    fill(searchBox(), 'cann-20g')
    await waitFor(() => expect(bodyRows()).toHaveLength(1))
    expect(screen.getByRole('row', { name: /IV cannula 20G/ })).toBeInTheDocument()

    fill(searchBox(), 'cann')
    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(screen.getByText(/not a word in the middle, and not part of a SKU/)).toBeInTheDocument()
    expect(screen.queryByText('No items in the catalog')).not.toBeInTheDocument()

    // A longer one is a 422, which would show as a failure to load.
    fill(searchBox(), 'x'.repeat(250))
    await waitFor(() => expect(wire(sent('get', ITEMS).at(-1)!).q).toBe('x'.repeat(200)))
    expect(screen.queryByText("Couldn't load the item catalog")).not.toBeInTheDocument()
    // The box stops where the search does: what is searched is what is shown.
    expect(searchBox()).toHaveValue('x'.repeat(200))
  })

  it('stops the category box at the 100 characters the API takes, so it shows what is searched', async () => {
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Syringe 5 mL/ })
    expect(categoryBox()).toHaveAttribute('maxlength', '100')

    // Pasted or set by script, where the attribute alone does not hold.
    fill(categoryBox(), 'c'.repeat(150))

    expect(categoryBox()).toHaveValue('c'.repeat(100))
    await waitFor(() => expect(wire(sent('get', ITEMS).at(-1)!)).toEqual({ category: 'c'.repeat(100), page: 1, page_size: 25 }))
    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load the item catalog")).not.toBeInTheDocument()
  })

  it('filters by the exact category, trimmed, and leaves a blank one out', async () => {
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Syringe 5 mL/ })

    fill(categoryBox(), '  Sterile ')
    await waitFor(() => expect(bodyRows()).toHaveLength(2))
    expect(screen.getByRole('row', { name: /IV cannula 20G/ })).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /Sterile gauze swab/ })).toBeInTheDocument()

    // Exact means exact: the API does not fold case.
    fill(categoryBox(), 'sterile')
    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(screen.getByText(/written exactly as on the item, capitals included/)).toBeInTheDocument()

    fill(categoryBox(), '   ')
    expect(await screen.findByRole('row', { name: /Syringe 5 mL/ })).toBeInTheDocument()
    // Blank again is the first request, served from cache: category is never sent empty.
    expect(sent('get', ITEMS).map(wire)).toEqual([
      { page: 1, page_size: 25 },
      { category: 'Sterile', page: 1, page_size: 25 },
      { category: 'sterile', page: 1, page_size: 25 },
    ])
  })

  it('filters on is_active, and leaves the parameter out for "all"', async () => {
    const user = userEvent.setup()
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Latex gloves/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Latex gloves/ })).not.toBeInTheDocument())
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Syringe 5 mL/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Latex gloves/ })).toBeInTheDocument()
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active and inactive')
    expect(await screen.findByRole('row', { name: /Syringe 5 mL/ })).toBeInTheDocument()

    expect(sent('get', ITEMS).map(wire)).toEqual([
      { page: 1, page_size: 25 },
      { is_active: true, page: 1, page_size: 25 },
      { is_active: false, page: 1, page_size: 25 },
    ])
  })

  it('sends every filter together, and says when a status is what leaves the list empty', async () => {
    items = items.filter((i) => i.is_active)
    const user = userEvent.setup()
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Syringe 5 mL/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')
    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(screen.getByText('No item in the catalog has this status.')).toBeInTheDocument()

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    fill(searchBox(), 'sterile')
    fill(categoryBox(), 'Sterile')
    await waitFor(() =>
      expect(wire(sent('get', ITEMS).at(-1)!)).toEqual({ q: 'sterile', category: 'Sterile', is_active: true, page: 1, page_size: 25 }),
    )
    await waitFor(() => expect(bodyRows()).toHaveLength(1))
    expect(screen.getByRole('row', { name: /Sterile gauze swab/ })).toBeInTheDocument()
  })

  it('names every filter in force when several together leave the list empty', async () => {
    const user = userEvent.setup()
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Syringe 5 mL/ })

    // Each of these finds something alone; it is the category that does not fit the other two.
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    fill(searchBox(), 'syringe')
    fill(categoryBox(), 'Sterile')

    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(wire(sent('get', ITEMS).at(-1)!)).toEqual({ q: 'syringe', category: 'Sterile', is_active: true, page: 1, page_size: 25 })
    expect(
      screen.getByText(
        'Search matches the start of a name, or a whole SKU — not a word in the middle, and not part of a SKU. ' +
          'A category matches only when it is written exactly as on the item, capitals included. ' +
          'Only active items are being shown.',
      ),
    ).toBeInTheDocument()

    // With the search cleared, the two that remain are the two explained.
    fill(searchBox(), '')
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')
    expect(
      await screen.findByText(
        'A category matches only when it is written exactly as on the item, capitals included. Only inactive items are being shown.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(/Search matches the start of a name/)).not.toBeInTheDocument()
  })

  it("pages through the server's pages, and a new search starts again at the first", async () => {
    items = Array.from({ length: 30 }, (_, n) => item(`IT-${String(n).padStart(2, '0')}`, `Item ${String(n).padStart(2, '0')}`))
    const user = userEvent.setup()
    renderAt(ITEMS, PHARMACIST)
    await screen.findByRole('row', { name: /Item 00/ })
    expect(screen.queryByRole('row', { name: /Item 29/ })).not.toBeInTheDocument()
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() => expect(wire(sent('get', ITEMS).at(-1)!)).toEqual({ page: 2, page_size: 25 }))
    expect(await screen.findByRole('row', { name: /Item 29/ })).toBeInTheDocument()

    fill(searchBox(), 'item 0')
    await waitFor(() => expect(wire(sent('get', ITEMS).at(-1)!)).toEqual({ q: 'item 0', page: 1, page_size: 25 }))
    await waitFor(() => expect(bodyRows()).toHaveLength(10))
  })

  it('goes back to the last page there is when the page being read empties', async () => {
    // 26 active items: the second page of "Active only" holds one.
    items = Array.from({ length: 26 }, (_, n) => item(`IT-${String(n).padStart(2, '0')}`, `Item ${String(n).padStart(2, '0')}`))
    const user = userEvent.setup()
    renderAt(ITEMS, INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /Item 00/ })
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    await waitFor(() => expect(wire(sent('get', ITEMS).at(-1)!)).toEqual({ is_active: true, page: 1, page_size: 25 }))
    await user.click(await screen.findByRole('button', { name: 'Next' }))
    await screen.findByRole('row', { name: /Item 25/ })
    expect(bodyRows()).toHaveLength(1)
    const before = sent('get', ITEMS).length

    // Switched off, it leaves the filtered list, and page 2 no longer exists.
    await user.click(screen.getByRole('button', { name: 'Edit Item 25' }))
    const d = await dialog()
    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Item 25'))

    // Not "nothing matches": the 25 that still do are shown.
    await waitFor(() => expect(bodyRows()).toHaveLength(25))
    expect(screen.getByRole('row', { name: /Item 00/ })).toBeInTheDocument()
    expect(screen.queryByText('No matching items')).not.toBeInTheDocument()
    // Page 2 was read again, came back empty, and page 1 was read in its place.
    expect(sent('get', ITEMS).slice(before).map(wire)).toEqual([
      { is_active: true, page: 2, page_size: 25 },
      { is_active: true, page: 1, page_size: 25 },
    ])
  })

  it('shows skeleton rows while the list loads', async () => {
    const release = hold((c) => c.url === ITEMS)
    renderAt(ITEMS, PHARMACIST)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    expect(screen.queryByText('No items in the catalog')).not.toBeInTheDocument()
    release()

    expect(await screen.findByRole('row', { name: /Syringe 5 mL/ })).toBeInTheDocument()
    expect(skeletons()).toBe(0)
  })

  it('says the catalog is empty, and offers to add an item only to someone who can', async () => {
    items = []
    const view = renderAt(ITEMS, INVENTORY_MANAGER)
    expect(await screen.findByText('No items in the catalog')).toBeInTheDocument()
    expect(screen.getByText(/so their stock can be recorded, used and ordered/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Add item/ })).toHaveLength(2)
    view.unmount()

    renderAt(ITEMS, NURSE)
    expect(await screen.findByText('No items in the catalog')).toBeInTheDocument()
    expect(screen.getByText('Items added to the catalog will appear here.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add item/ })).not.toBeInTheDocument()
  })

  it("offers a retry when the list cannot be loaded, without the server's text", async () => {
    intercept = (c) => (c.url === ITEMS ? fail(500, 'boom: relation "inventory_items" does not exist') : undefined)
    const user = userEvent.setup()
    renderAt(ITEMS, PHARMACIST)

    expect(await screen.findByText("Couldn't load the item catalog")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom|relation/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: /Syringe 5 mL/ })).toBeInTheDocument()
    expect(sent('get', ITEMS)).toHaveLength(2)
  })
})

describe('adding an item', () => {
  it('adds an item with exactly the body the API takes, blank optionals as null', async () => {
    const user = userEvent.setup()
    const d = await openAddItem(user)
    expect(d).toHaveTextContent("The SKU and whether it is tracked by batch can't be changed afterwards")
    // What a reorder point means, in plain words: the server's judgement, hospital-wide.
    expect(within(d).getByLabelText(/^Reorder point/)).toHaveAccessibleDescription(
      'Optional. When the usable stock across the hospital is at or below this, the item is marked low. Left blank, it never is.',
    )
    expect(within(d).getByLabelText(/^SKU/)).toHaveAccessibleDescription(/can't be changed later/)
    expect(within(d).getByRole('checkbox', { name: /Tracked by batch/ })).not.toBeChecked()
    expect(within(d).getByRole('checkbox', { name: /Tracked by batch/ })).toHaveAccessibleDescription(/Can't be changed later/)
    // A new item is always active: the API does not take the flag.
    expect(within(d).queryByRole('checkbox', { name: /Active/ })).not.toBeInTheDocument()
    expect(within(d).getByLabelText(/^Unit of measure/)).toHaveValue('unit')

    fill(within(d).getByLabelText(/^SKU/), '  glove-l ')
    fill(within(d).getByLabelText(/^Name/), '  Nitrile gloves, large  ')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    // The SKU as the server saved it.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Added Nitrile gloves, large (GLOVE-L)'))
    const [request] = sent('post', ITEMS)
    expect(address(request)).toBe('/api/v1/inventory/items')
    const body = bodyOf(request)
    expect(body).toEqual({
      sku: 'GLOVE-L',
      name: 'Nitrile gloves, large',
      category: null,
      unit_of_measure: 'unit',
      is_batch_tracked: false,
      reorder_point: null,
      target_stock: null,
    })
    expect(body).not.toHaveProperty('is_active')
    expect(request.headers.get('Idempotency-Key')).toBeFalsy()
    const row = await screen.findByRole('row', { name: /Nitrile gloves, large/ })
    expect(cellsOf(row).slice(0, 8)).toEqual(['Nitrile gloves, large', 'GLOVE-L', '—', 'unit', 'No', '—', '—', 'Active'])
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('sends every field when all are filled, the levels as whole numbers', async () => {
    const user = userEvent.setup()
    const d = await openAddItem(user, HOSPITAL_ADMIN)

    fill(within(d).getByLabelText(/^SKU/), 'SUT-3.0/NY')
    fill(within(d).getByLabelText(/^Name/), 'Suture 3-0 nylon')
    fill(within(d).getByLabelText(/^Category/), ' Sterile ')
    fill(within(d).getByLabelText(/^Unit of measure/), ' box of 12 ')
    fill(within(d).getByLabelText(/^Reorder point/), '010')
    fill(within(d).getByLabelText(/^Target stock/), ' 40 ')
    await user.click(within(d).getByRole('checkbox', { name: /Tracked by batch/ }))
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Added Suture 3-0 nylon (SUT-3.0/NY)'))
    expect(bodyOf(sent('post', ITEMS)[0])).toEqual({
      sku: 'SUT-3.0/NY',
      name: 'Suture 3-0 nylon',
      category: 'Sterile',
      unit_of_measure: 'box of 12',
      is_batch_tracked: true,
      reorder_point: 10,
      target_stock: 40,
    })
    const row = await screen.findByRole('row', { name: /Suture 3-0 nylon/ })
    expect(cellsOf(row).slice(0, 8)).toEqual(['Suture 3-0 nylon', 'SUT-3.0/NY', 'Sterile', 'box of 12', 'Yes', '10', '40', 'Active'])
  })

  it('accepts equal levels, a zero reorder point, and one level without the other', async () => {
    const user = userEvent.setup()
    const d = await openAddItem(user)

    fill(within(d).getByLabelText(/^SKU/), 'TAPE-1')
    fill(within(d).getByLabelText(/^Name/), 'Surgical tape')
    fill(within(d).getByLabelText(/^Reorder point/), '0')
    fill(within(d).getByLabelText(/^Target stock/), '0')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(sent('post', ITEMS)[0])).toMatchObject({ reorder_point: 0, target_stock: 0 })

    await user.click(screen.getByRole('button', { name: /Add item/ }))
    const again = await dialog()
    // The form starts empty again.
    expect(within(again).getByLabelText(/^SKU/)).toHaveValue('')
    fill(within(again).getByLabelText(/^SKU/), 'TAPE-2')
    fill(within(again).getByLabelText(/^Name/), 'Surgical tape, wide')
    fill(within(again).getByLabelText(/^Target stock/), '10000000')
    await user.click(within(again).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(2))
    expect(bodyOf(sent('post', ITEMS)[1])).toMatchObject({ reorder_point: null, target_stock: 10000000 })
  })

  it('refuses missing, malformed and over-long fields before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openAddItem(user)

    fill(within(d).getByLabelText(/^Unit of measure/), '  ')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))
    expect(await within(d).findByText('Enter a SKU')).toBeInTheDocument()
    expect(within(d).getByText('Enter a name')).toBeInTheDocument()
    expect(within(d).getByText('Enter what one unit is, e.g. piece or box of 100')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^SKU/)).toBeInvalid()

    fill(within(d).getByLabelText(/^SKU/), 'glove m')
    fill(within(d).getByLabelText(/^Name/), 'N'.repeat(201))
    fill(within(d).getByLabelText(/^Category/), 'C'.repeat(101))
    fill(within(d).getByLabelText(/^Unit of measure/), 'U'.repeat(31))
    fill(within(d).getByLabelText(/^Reorder point/), '2.5')
    fill(within(d).getByLabelText(/^Target stock/), '10000001')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    expect(await within(d).findByText('Letters, digits and - _ . / only, starting with a letter or digit')).toBeInTheDocument()
    expect(within(d).getByText('Keep the name to 200 characters')).toBeInTheDocument()
    expect(within(d).getByText('Keep the category to 100 characters')).toBeInTheDocument()
    expect(within(d).getByText('Keep the unit to 30 characters')).toBeInTheDocument()
    // A fraction is a 422 from the API, never rounded.
    expect(within(d).getByText('Enter the reorder point as a whole number, or leave it blank')).toBeInTheDocument()
    expect(within(d).getByText('Keep the target stock to 10000000 or less')).toBeInTheDocument()

    fill(within(d).getByLabelText(/^SKU/), 'S'.repeat(51))
    fill(within(d).getByLabelText(/^Reorder point/), '-5')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))
    expect(await within(d).findByText('Keep the SKU to 50 characters')).toBeInTheDocument()
    expect(within(d).getByText('Enter the reorder point as a whole number, or leave it blank')).toBeInTheDocument()

    // "ß" grows when uppercased; the API would measure it before that.
    fill(within(d).getByLabelText(/^SKU/), 'straße')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))
    expect(await within(d).findByText('Letters, digits and - _ . / only, starting with a letter or digit')).toBeInTheDocument()
    expect(sent('post', ITEMS)).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('refuses a target below the reorder point, under the target, before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openAddItem(user)

    fill(within(d).getByLabelText(/^SKU/), 'TAPE-1')
    fill(within(d).getByLabelText(/^Name/), 'Surgical tape')
    fill(within(d).getByLabelText(/^Reorder point/), '50')
    fill(within(d).getByLabelText(/^Target stock/), '49')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    await waitFor(() => expect(within(d).getByLabelText(/^Target stock/)).toBeInvalid())
    expect(within(d).getByLabelText(/^Target stock/)).toHaveAccessibleDescription(
      'The target stock must not be less than the reorder point',
    )
    expect(within(d).getByLabelText(/^Reorder point/)).toBeValid()
    expect(sent('post', ITEMS)).toHaveLength(0)

    fill(within(d).getByLabelText(/^Target stock/), '50')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(sent('post', ITEMS)[0])).toMatchObject({ reorder_point: 50, target_stock: 50 })
  })

  it("shows the API's own refusal of the levels, which on a create names no field", async () => {
    // The form would not send this; another client's rule, or a changed one, still has to be readable.
    intercept = (c) =>
      c.url === ITEMS && c.method === 'post'
        ? fail(422, 'Validation failed.', {
            errors: [{ field: '', message: 'Value error, target_stock must not be less than reorder_point.' }],
          })
        : undefined
    const user = userEvent.setup()
    const d = await openAddItem(user)
    fill(within(d).getByLabelText(/^SKU/), 'TAPE-1')
    fill(within(d).getByLabelText(/^Name/), 'Surgical tape')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    expect(await within(d).findByText('target_stock must not be less than reorder_point.')).toBeInTheDocument()
    expect(d).not.toHaveTextContent('Value error')
    expect(toastError).toHaveBeenCalledWith('target_stock must not be less than reorder_point.')
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it("shows the API's message for a SKU already in use, even by an inactive item", async () => {
    const user = userEvent.setup()
    const d = await openAddItem(user)

    fill(within(d).getByLabelText(/^SKU/), 'glove-lx')
    fill(within(d).getByLabelText(/^Name/), 'Latex gloves')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("An item with SKU 'GLOVE-LX' already exists.")
    expect(toastError).toHaveBeenCalledWith("An item with SKU 'GLOVE-LX' already exists.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(items).toHaveLength(8)
    // The dialog stays open with what was typed, so the SKU can be corrected.
    expect(within(d).getByLabelText(/^SKU/)).toHaveValue('glove-lx')
    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Latex gloves')
  })

  it('puts a request-validation 422 under the field it names, without the "Value error" prefix', async () => {
    intercept = (c) =>
      c.url === ITEMS && c.method === 'post'
        ? invalid('sku', 'Value error, SKU may contain only letters, digits and the characters - _ . /')
        : undefined
    const user = userEvent.setup()
    const d = await openAddItem(user)
    fill(within(d).getByLabelText(/^SKU/), 'TAPE-1')
    fill(within(d).getByLabelText(/^Name/), 'Surgical tape')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    await waitFor(() => expect(within(d).getByLabelText(/^SKU/)).toBeInvalid())
    expect(within(d).getByLabelText(/^SKU/)).toHaveAccessibleDescription(
      'SKU may contain only letters, digits and the characters - _ . /',
    )
    expect(d).not.toHaveTextContent('Value error')
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
  })

  it.each([
    // Not something this route answers today; shown in the API's words if it ever does.
    [400, 'Items cannot be added while the catalog is locked.', 'Items cannot be added while the catalog is locked.'],
    [403, 'Permission denied. Required: inventory.item.create.', 'Permission denied. Required: inventory.item.create.'],
    [500, 'boom: connection reset', "Couldn't add the item. Please try again."],
  ])('on a %i shows what the user can act on, never raw server text', async (status, serverMessage, shown) => {
    intercept = (c) => (c.url === ITEMS && c.method === 'post' ? fail(status, serverMessage) : undefined)
    const user = userEvent.setup()
    const d = await openAddItem(user)
    fill(within(d).getByLabelText(/^SKU/), 'TAPE-1')
    fill(within(d).getByLabelText(/^Name/), 'Surgical tape')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/boom/)
    expect(items).toHaveLength(8)
  })

  it('shows a 422 that names the batch flag under its checkbox', async () => {
    intercept = (c) =>
      c.url === ITEMS && c.method === 'post' ? invalid('is_batch_tracked', 'Input should be a valid boolean') : undefined
    const user = userEvent.setup()
    const d = await openAddItem(user)
    // The name is the short label alone; the explanation is the description.
    const box = within(d).getByRole('checkbox', { name: 'Tracked by batch' })
    expect(box).toHaveAccessibleDescription("Stock is kept per batch number, each with its own expiry date. Can't be changed later.")
    expect(within(d).getByLabelText(/^Unit of measure/)).toHaveAccessibleDescription(
      'What one unit is, e.g. piece or box of 100. Every quantity of this item is counted in it.',
    )
    fill(within(d).getByLabelText(/^SKU/), 'TAPE-1')
    fill(within(d).getByLabelText(/^Name/), 'Surgical tape')
    await user.click(within(d).getByRole('button', { name: 'Add item' }))

    // The toast says to check the highlighted fields, so one has to be.
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields."))
    expect(box).toBeInvalid()
    expect(within(d).getByRole('alert')).toHaveTextContent(/^Input should be a valid boolean$/)
    expect(box).toHaveAccessibleDescription(/Input should be a valid boolean$/)
  })

  it('adds one item however many times the form is submitted', async () => {
    const release = hold((c) => c.url === ITEMS && c.method === 'post')
    const user = userEvent.setup()
    const d = await openAddItem(user)
    fill(within(d).getByLabelText(/^SKU/), 'TAPE-1')
    fill(within(d).getByLabelText(/^Name/), 'Surgical tape')
    const form = within(d).getByRole('button', { name: 'Add item' }).closest('form') as HTMLFormElement

    // No Idempotency-Key exists for this route: a second request would be a second item, or a 409.
    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Adding…' })).toBeDisabled()
    expect(sent('post', ITEMS)).toHaveLength(1)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', ITEMS)).toHaveLength(1)
    expect(toastError).not.toHaveBeenCalled()
    expect(items).toHaveLength(9)
  })
})

describe('editing an item', () => {
  it('edits by sending only what changed, a cleared field as null, never the SKU or the batch flag', async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Nitrile gloves, medium')

    expect(within(d).getByRole('heading')).toHaveTextContent(/^Edit item$/)
    expect(within(d).getByLabelText(/^SKU/)).toHaveValue('GLOVE-M')
    expect(within(d).getByLabelText(/^SKU/)).toBeDisabled()
    expect(within(d).getByLabelText(/^SKU/)).toHaveAccessibleDescription("Can't be changed")
    expect(within(d).getByRole('checkbox', { name: /Tracked by batch/ })).toBeDisabled()
    expect(within(d).getByRole('checkbox', { name: /Tracked by batch/ })).not.toBeChecked()
    expect(within(d).getByLabelText(/^Reorder point/)).toHaveValue('20')
    expect(within(d).getByLabelText(/^Target stock/)).toHaveValue('80')
    expect(within(d).getByRole('checkbox', { name: /Active — can be used, moved and ordered/ })).toBeChecked()
    // Nothing to save until something is changed.
    expect(within(d).getByRole('button', { name: 'Save changes' })).toBeDisabled()

    fill(within(d).getByLabelText(/^Name/), ' Nitrile gloves, M ')
    fill(within(d).getByLabelText(/^Category/), '')
    fill(within(d).getByLabelText(/^Reorder point/), '25')
    fill(within(d).getByLabelText(/^Target stock/), '')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Nitrile gloves, M'))
    const [request] = sent('patch', '/inventory/items/i-GLOVE-M')
    expect(address(request)).toBe('/api/v1/inventory/items/i-GLOVE-M')
    const body = bodyOf(request)
    // The unit and the status were not touched, so they are not sent.
    expect(body).toEqual({ name: 'Nitrile gloves, M', category: null, reorder_point: 25, target_stock: null })
    expect(body).not.toHaveProperty('sku')
    expect(body).not.toHaveProperty('is_batch_tracked')
    const row = await screen.findByRole('row', { name: /Nitrile gloves, M/ })
    await waitFor(() =>
      expect(cellsOf(row).slice(0, 8)).toEqual(['Nitrile gloves, M', 'GLOVE-M', '—', 'box of 100', 'No', '25', '—', 'Active']),
    )
  })

  it('shows a batch-tracked item as such, and switches an item off with is_active alone', async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'IV cannula 20G')
    expect(within(d).getByRole('checkbox', { name: /Tracked by batch/ })).toBeChecked()
    expect(within(d).getByRole('checkbox', { name: /Tracked by batch/ })).toBeDisabled()
    expect(d).toHaveTextContent('An inactive item stays in the catalog and keeps its stock')

    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved IV cannula 20G'))
    expect(bodyOf(sent('patch', '/inventory/items/i-CANN-20G')[0])).toEqual({ is_active: false })
    const row = await screen.findByRole('row', { name: /IV cannula 20G/ })
    await waitFor(() => expect(within(row).getByText('Inactive')).toBeInTheDocument())
    // Still listed: there is no delete.
    expect(screen.queryByRole('button', { name: /delete|remove/i })).not.toBeInTheDocument()
  })

  it('switches an inactive item back on, and sets levels on an item that had none', async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Latex gloves (withdrawn)')
    expect(within(d).getByRole('checkbox', { name: /Active/ })).not.toBeChecked()
    expect(within(d).getByLabelText(/^Reorder point/)).toHaveValue('')

    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    fill(within(d).getByLabelText(/^Reorder point/), '5')
    fill(within(d).getByLabelText(/^Unit of measure/), 'box of 50')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Latex gloves (withdrawn)'))
    expect(bodyOf(sent('patch', '/inventory/items/i-GLOVE-LX')[0])).toEqual({
      unit_of_measure: 'box of 50',
      reorder_point: 5,
      is_active: true,
    })
  })

  it('sends nothing when the only change disappears on trimming or is the same number', async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Syringe 5 mL')

    fill(within(d).getByLabelText(/^Name/), 'Syringe 5 mL  ')
    fill(within(d).getByLabelText(/^Reorder point/), '020')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Nothing to save — the item is unchanged'))
    expect(writes()).toHaveLength(0)
  })

  it('refuses an edit that sends both levels with the target below the reorder point, before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Nitrile gloves, medium')

    fill(within(d).getByLabelText(/^Reorder point/), '90')
    fill(within(d).getByLabelText(/^Target stock/), '85')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    // Both figures are in the request, so they are the two the API would judge.
    expect(await within(d).findByRole('alert')).toHaveTextContent('The target stock must not be less than the reorder point')
    expect(toastError).toHaveBeenCalledWith('The target stock must not be less than the reorder point')
    expect(writes()).toHaveLength(0)

    fill(within(d).getByLabelText(/^Target stock/), '90')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Nitrile gloves, medium'))
    expect(bodyOf(sent('patch', '/inventory/items/i-GLOVE-M')[0])).toEqual({ reorder_point: 90, target_stock: 90 })
  })

  it('leaves the level rule to the server when one level is sent, and does not let the disabled SKU block saving', async () => {
    // A SKU the create form would refuse; the edit form must not judge what it cannot send.
    items = [item('legacy sku', 'Legacy item', { reorder_point: 20, target_stock: 80 })]
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Legacy item')

    // The target on screen is only what was saved when the form opened: the server holds the one that counts.
    fill(within(d).getByLabelText(/^Reorder point/), '81')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(within(d).getByLabelText(/^Target stock/)).toBeInvalid())
    expect(within(d).getByLabelText(/^Target stock/)).toHaveAccessibleDescription(
      'target_stock must not be less than reorder_point.',
    )
    expect(bodyOf(sent('patch', '/inventory/items/i-legacy sku')[0])).toEqual({ reorder_point: 81 })
    expect(items[0].reorder_point).toBe(20)

    fill(within(d).getByLabelText(/^Reorder point/), '80')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Legacy item'))
    expect(bodyOf(sent('patch', '/inventory/items/i-legacy sku')[1])).toEqual({ reorder_point: 80 })
  })

  it.each([
    ['raised the target', { target_stock: 200 }, /^Reorder point/, '100', { reorder_point: 100 }],
    ['cleared the target', { target_stock: null }, /^Reorder point/, '100', { reorder_point: 100 }],
    ['lowered the reorder point', { reorder_point: 5 }, /^Target stock/, '10', { target_stock: 10 }],
  ])(
    'does not refuse on a level that is stale: someone else %s while the form was open',
    async (_what, theirs, label, typed, body) => {
      const user = userEvent.setup()
      const d = await openEditItem(user, 'Nitrile gloves, medium')
      // The form still shows 20 and 80; against those the save would look wrong.
      Object.assign(items.find((i) => i.sku === 'GLOVE-M')!, theirs)

      fill(within(d).getByLabelText(label), typed)
      await user.click(within(d).getByRole('button', { name: 'Save changes' }))

      // The server judges against what is saved, and accepts.
      await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Nitrile gloves, medium'))
      expect(toastError).not.toHaveBeenCalled()
      expect(bodyOf(sent('patch', '/inventory/items/i-GLOVE-M')[0])).toEqual(body)
      expect(items.find((i) => i.sku === 'GLOVE-M')).toMatchObject({ ...theirs, ...body })
    },
  )

  it('says what switching an item off and changing its unit do, as the description of each control', async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'IV cannula 20G')

    // The name is the short label alone; the explanation is the description.
    expect(within(d).getByRole('checkbox', { name: 'Active — can be used, moved and ordered' })).toHaveAccessibleDescription(
      'An inactive item stays in the catalog and keeps its stock, which can still be corrected. ' +
        'It cannot be used, transferred or put on a new purchase order, ' +
        'and it is left out of the stock overview and the low-stock list even while it holds stock.',
    )
    expect(within(d).getByRole('checkbox', { name: 'Tracked by batch' })).toHaveAccessibleDescription(
      "Can't be changed. Stock of a batch-tracked item is kept per batch number, each with its own expiry date.",
    )
    // The API changes the unit whatever stock there is; nothing is converted.
    expect(within(d).getByLabelText(/^Unit of measure/)).toHaveAccessibleDescription(
      'What one unit is, e.g. piece or box of 100. Changing it relabels the quantities already recorded; it does not convert them.',
    )
    expect(within(d).getByLabelText(/^Unit of measure/)).toBeEnabled()
  })

  it('shows a 422 that names the status under its checkbox', async () => {
    intercept = (c) => (c.method === 'patch' ? invalid('is_active', 'Input should be a valid boolean') : undefined)
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Syringe 5 mL')
    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    // The toast says to check the highlighted fields, so one has to be.
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields."))
    const box = within(d).getByRole('checkbox', { name: 'Active — can be used, moved and ordered' })
    expect(box).toBeInvalid()
    expect(within(d).getByRole('alert')).toHaveTextContent(/^Input should be a valid boolean$/)
    expect(box).toHaveAccessibleDescription(/Input should be a valid boolean$/)
  })

  it("puts the service's refusal under the target when the saved target has moved meanwhile", async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Nitrile gloves, medium')
    // Someone else lowers the target while the form, opened at 80, is still up.
    items.find((i) => i.sku === 'GLOVE-M')!.target_stock = 30

    fill(within(d).getByLabelText(/^Reorder point/), '50')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(within(d).getByLabelText(/^Target stock/)).toBeInvalid())
    expect(within(d).getByLabelText(/^Target stock/)).toHaveAccessibleDescription(
      'target_stock must not be less than reorder_point.',
    )
    // Only the field that changed was sent; the stale target was not sent back over theirs.
    expect(bodyOf(sent('patch', '/inventory/items/i-GLOVE-M')[0])).toEqual({ reorder_point: 50 })
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(items.find((i) => i.sku === 'GLOVE-M')!.reorder_point).toBe(20)
  })

  it("shows the API's message when the item has gone", async () => {
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Syringe 5 mL')
    items = items.filter((i) => i.sku !== 'SYR-5')

    fill(within(d).getByLabelText(/^Name/), 'Syringe 5 mL, luer lock')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Inventory item not found.'))
    expect(toastSuccess).not.toHaveBeenCalled()
    // What the screen showed was stale, so the list is read again.
    await waitFor(() => expect(screen.queryByRole('row', { name: /Syringe 5 mL/ })).not.toBeInTheDocument())
  })

  it.each([
    [403, 'Permission denied. Required: inventory.item.update.', 'Permission denied. Required: inventory.item.update.'],
    [500, 'boom: could not serialize access', "Couldn't save the item. Please try again."],
  ])('on a %i from an edit shows what the user can act on, never raw server text', async (status, serverMessage, shown) => {
    intercept = (c) => (c.method === 'patch' ? fail(status, serverMessage) : undefined)
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Syringe 5 mL')
    fill(within(d).getByLabelText(/^Name/), 'Syringe 5 mL, luer lock')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/boom/)
    expect(sent('patch', '/inventory/items/i-SYR-5')).toHaveLength(1)
    // Nothing was saved, and the dialog keeps what was typed for another go.
    expect(items.find((i) => i.sku === 'SYR-5')!.name).toBe('Syringe 5 mL')
    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Syringe 5 mL, luer lock')
  })

  it('saves once however many times the form is submitted', async () => {
    const release = hold((c) => c.method === 'patch')
    const user = userEvent.setup()
    const d = await openEditItem(user, 'Syringe 5 mL')
    fill(within(d).getByLabelText(/^Name/), 'Syringe 5 mL, luer lock')
    const form = within(d).getByRole('button', { name: 'Save changes' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Saving…' })).toBeDisabled()
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('patch', '/inventory/items/i-SYR-5')).toHaveLength(1)
  })
})

// ── Locations ───────────────────────────────────────────────────────────────

describe('locations', () => {
  it('lists locations as the API returns them, with plain words for the kind and no search', async () => {
    renderAt(LOCATIONS, INVENTORY_MANAGER)

    const row = await screen.findByRole('row', { name: /General store/ })
    expect(cellsOf(row).slice(0, 4)).toEqual(['General store', 'STORE', 'Store', 'Active'])
    expect(within(row).getByText('STORE')).toHaveClass('font-mono')
    // Name order is the server's; every kind in words, not its code.
    expect(bodyRows().map((r) => cellsOf(r).slice(0, 4))).toEqual([
      ['General store', 'STORE', 'Store', 'Active'],
      ['Intensive care unit', 'ICU', 'ICU', 'Active'],
      ['Operating theatre 1', 'OT-1', 'Operating theatre', 'Active'],
      ['Ward A', 'WARD-A', 'Ward', 'Active'],
      ['Ward Z (closed)', 'WARD-Z', 'Ward', 'Inactive'],
    ])
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Name', 'Code', 'Kind', 'Status', ''])
    expect(screen.getByRole('navigation', { name: 'Inventory sections' })).toBeInTheDocument()

    const [request] = sent('get', LOCATIONS)
    expect(address(request)).toBe('/api/v1/inventory/locations')
    // Not a paginated endpoint: no page, no page size, and no empty filter.
    expect(wire(request)).toEqual({})
    expect(fake.sent).toHaveLength(1)
    // The API has no search parameter for locations, so there is no box to type in.
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /delete|remove/i })).not.toBeInTheDocument()
  })

  it('shows the whole list with no pagination controls, however long it is', async () => {
    locations = Array.from({ length: 40 }, (_, n) => location(`BAY-${String(n).padStart(2, '0')}`, `Bay ${String(n).padStart(2, '0')}`, 'ward'))
    renderAt(LOCATIONS, NURSE)

    await screen.findByRole('row', { name: /Bay 00/ })
    expect(bodyRows()).toHaveLength(40)
    expect(screen.getByRole('row', { name: /Bay 39/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Previous' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Page \d+ of \d+/)).not.toBeInTheDocument()
    expect(sent('get', LOCATIONS).map(wire)).toEqual([{}])
  })

  it('filters on is_active, and leaves the parameter out for "all"', async () => {
    const user = userEvent.setup()
    renderAt(LOCATIONS, PHARMACIST)
    await screen.findByRole('row', { name: /Ward Z/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Ward Z/ })).not.toBeInTheDocument())
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /General store/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Ward Z/ })).toBeInTheDocument()
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active and inactive')
    expect(await screen.findByRole('row', { name: /General store/ })).toBeInTheDocument()

    // "All" is the first request, served again from cache: is_active is never sent empty.
    expect(sent('get', LOCATIONS).map(wire)).toEqual([{}, { is_active: true }, { is_active: false }])
  })

  it('shows skeleton rows while the list loads', async () => {
    const release = hold((c) => c.url === LOCATIONS)
    renderAt(LOCATIONS, PHARMACIST)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    expect(screen.queryByText('No locations yet')).not.toBeInTheDocument()
    release()

    expect(await screen.findByRole('row', { name: /General store/ })).toBeInTheDocument()
    expect(skeletons()).toBe(0)
  })

  it('says there are no locations yet, and offers to add one only to someone who can', async () => {
    locations = []
    const view = renderAt(LOCATIONS, INVENTORY_MANAGER)
    expect(await screen.findByText('No locations yet')).toBeInTheDocument()
    expect(screen.getByText(/Stock is always recorded at a location/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Add location/ })).toHaveLength(2)
    view.unmount()

    renderAt(LOCATIONS, NURSE)
    expect(await screen.findByText('No locations yet')).toBeInTheDocument()
    expect(screen.getByText(/Locations added by someone who manages inventory/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add location/ })).not.toBeInTheDocument()
  })

  it('says so differently when the filter is what leaves the list empty', async () => {
    locations = locations.filter((l) => l.is_active)
    const user = userEvent.setup()
    renderAt(LOCATIONS, INVENTORY_MANAGER)
    await screen.findByRole('row', { name: /General store/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')

    expect(await screen.findByText('No matching locations')).toBeInTheDocument()
    expect(screen.getByText('No location has been switched off.')).toBeInTheDocument()
    expect(screen.queryByText('No locations yet')).not.toBeInTheDocument()
  })

  it("offers a retry when the list cannot be loaded, without the server's text", async () => {
    intercept = (c) => (c.url === LOCATIONS ? fail(500, 'boom: relation "inventory_locations" does not exist') : undefined)
    const user = userEvent.setup()
    renderAt(LOCATIONS, PHARMACIST)

    expect(await screen.findByText("Couldn't load locations")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom|relation/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: /General store/ })).toBeInTheDocument()
    expect(sent('get', LOCATIONS)).toHaveLength(2)
  })
})

describe('adding a location', () => {
  it('adds a store by default, with the trimmed name and the code in capitals', async () => {
    const user = userEvent.setup()
    const d = await openAddLocation(user)
    expect(d).toHaveTextContent("Its code can't be changed afterwards")
    expect(d).toHaveTextContent('switched off later, but not deleted')
    expect(within(d).getByLabelText(/^Code/)).toHaveAccessibleDescription(/can't be changed later/)
    expect(within(d).getByRole('combobox', { name: /Kind/ })).toHaveTextContent('Store')
    expect(within(d).queryByRole('checkbox')).not.toBeInTheDocument()

    fill(within(d).getByLabelText(/^Name/), '  Pharmacy sub-store  ')
    fill(within(d).getByLabelText(/^Code/), ' store-2 ')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))

    // The code as the server saved it.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Added Pharmacy sub-store (STORE-2)'))
    const [request] = sent('post', LOCATIONS)
    expect(address(request)).toBe('/api/v1/inventory/locations')
    const body = bodyOf(request)
    expect(body).toEqual({ name: 'Pharmacy sub-store', code: 'STORE-2', kind: 'store' })
    // The request model forbids the key: a new location is always active.
    expect(body).not.toHaveProperty('is_active')
    const row = await screen.findByRole('row', { name: /Pharmacy sub-store/ })
    expect(cellsOf(row).slice(0, 4)).toEqual(['Pharmacy sub-store', 'STORE-2', 'Store', 'Active'])
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it("offers the four kinds in plain words and sends the API's own value", async () => {
    const user = userEvent.setup()
    const d = await openAddLocation(user)

    await user.click(within(d).getByRole('combobox', { name: /Kind/ }))
    expect((await screen.findAllByRole('option')).map((o) => o.textContent)).toEqual(['Store', 'Ward', 'ICU', 'Operating theatre'])
    await user.click(screen.getByRole('option', { name: 'Operating theatre' }))
    fill(within(d).getByLabelText(/^Name/), 'Operating theatre 2')
    fill(within(d).getByLabelText(/^Code/), 'OT-2')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Added Operating theatre 2 (OT-2)'))
    expect(bodyOf(sent('post', LOCATIONS)[0])).toEqual({ name: 'Operating theatre 2', code: 'OT-2', kind: 'ot' })
  })

  it('refuses a missing name or code and a malformed code before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openAddLocation(user)

    fill(within(d).getByLabelText(/^Name/), '   ')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))
    expect(await within(d).findByText("Enter the location's name")).toBeInTheDocument()
    expect(within(d).getByText('Enter a code')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Name/)).toBeInvalid()

    fill(within(d).getByLabelText(/^Name/), 'N'.repeat(201))
    fill(within(d).getByLabelText(/^Code/), 'ward b')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))
    expect(await within(d).findByText('Keep the name to 200 characters')).toBeInTheDocument()
    expect(within(d).getByText('Letters, digits and - _ . / only, starting with a letter or digit')).toBeInTheDocument()

    fill(within(d).getByLabelText(/^Code/), 'C'.repeat(51))
    await user.click(within(d).getByRole('button', { name: 'Add location' }))
    expect(await within(d).findByText('Keep the code to 50 characters')).toBeInTheDocument()

    fill(within(d).getByLabelText(/^Code/), '-WARD')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))
    expect(await within(d).findByText('Letters, digits and - _ . / only, starting with a letter or digit')).toBeInTheDocument()
    expect(sent('post', LOCATIONS)).toHaveLength(0)
  })

  it('accepts a name of exactly 200 characters once the spaces around it are trimmed', async () => {
    const user = userEvent.setup()
    const d = await openAddLocation(user)

    // The server measures the raw text, so the padding would be a 422 if it were sent.
    fill(within(d).getByLabelText(/^Name/), `  ${'N'.repeat(200)}  `)
    fill(within(d).getByLabelText(/^Code/), `  ${'C'.repeat(50)} `)
    await user.click(within(d).getByRole('button', { name: 'Add location' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(sent('post', LOCATIONS)[0])).toEqual({ name: 'N'.repeat(200), code: 'C'.repeat(50), kind: 'store' })
  })

  it("shows the API's message for a code already in use, even by an inactive location", async () => {
    const user = userEvent.setup()
    const d = await openAddLocation(user)

    fill(within(d).getByLabelText(/^Name/), 'Ward Z, reopened')
    fill(within(d).getByLabelText(/^Code/), 'ward-z')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("A location with code 'WARD-Z' already exists.")
    expect(toastError).toHaveBeenCalledWith("A location with code 'WARD-Z' already exists.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(locations).toHaveLength(5)
    // The dialog stays open with what was typed, so the code can be corrected.
    expect(within(d).getByLabelText(/^Code/)).toHaveValue('ward-z')
  })

  it('puts a request-validation 422 under the field it names, without the "Value error" prefix', async () => {
    intercept = (c) =>
      c.url === LOCATIONS && c.method === 'post'
        ? invalid('code', 'Value error, Code may contain only letters, digits and the characters - _ . /')
        : undefined
    const user = userEvent.setup()
    const d = await openAddLocation(user)
    fill(within(d).getByLabelText(/^Name/), 'Ward B')
    fill(within(d).getByLabelText(/^Code/), 'WARD-B')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))

    await waitFor(() => expect(within(d).getByLabelText(/^Code/)).toBeInvalid())
    expect(within(d).getByLabelText(/^Code/)).toHaveAccessibleDescription(
      'Code may contain only letters, digits and the characters - _ . /',
    )
    expect(d).not.toHaveTextContent('Value error')
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
  })

  it('puts a 422 in the shape a service sends under its field too', async () => {
    // No location rule is enforced by the service today; the shape is the one its other refusals use.
    intercept = (c) => (c.url === LOCATIONS && c.method === 'post' ? refused('name', 'A location of this name is being set up.') : undefined)
    const user = userEvent.setup()
    const d = await openAddLocation(user)
    fill(within(d).getByLabelText(/^Name/), 'Ward B')
    fill(within(d).getByLabelText(/^Code/), 'WARD-B')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))

    await waitFor(() => expect(within(d).getByLabelText(/^Name/)).toBeInvalid())
    expect(within(d).getByLabelText(/^Name/)).toHaveAccessibleDescription('A location of this name is being set up.')
  })

  it.each([
    // Not something this route answers today; shown in the API's words if it ever does.
    [400, 'Locations cannot be added during a stock count.', 'Locations cannot be added during a stock count.'],
    [403, 'Permission denied. Required: inventory.location.create.', 'Permission denied. Required: inventory.location.create.'],
    [500, 'boom: connection reset', "Couldn't add the location. Please try again."],
  ])('on a %i shows what the user can act on, never raw server text', async (status, serverMessage, shown) => {
    intercept = (c) => (c.url === LOCATIONS && c.method === 'post' ? fail(status, serverMessage) : undefined)
    const user = userEvent.setup()
    const d = await openAddLocation(user)
    fill(within(d).getByLabelText(/^Name/), 'Ward B')
    fill(within(d).getByLabelText(/^Code/), 'WARD-B')
    await user.click(within(d).getByRole('button', { name: 'Add location' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/boom/)
    expect(locations).toHaveLength(5)
  })

  it('adds one location however many times the form is submitted', async () => {
    const release = hold((c) => c.url === LOCATIONS && c.method === 'post')
    const user = userEvent.setup()
    const d = await openAddLocation(user)
    fill(within(d).getByLabelText(/^Name/), 'Ward B')
    fill(within(d).getByLabelText(/^Code/), 'WARD-B')
    const form = within(d).getByRole('button', { name: 'Add location' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Adding…' })).toBeDisabled()
    expect(sent('post', LOCATIONS)).toHaveLength(1)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', LOCATIONS)).toHaveLength(1)
    expect(toastError).not.toHaveBeenCalled()
    expect(locations).toHaveLength(6)
  })
})

describe('editing a location', () => {
  it('edits by sending only what changed, and never the code', async () => {
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward A')

    expect(within(d).getByRole('heading')).toHaveTextContent(/^Edit location$/)
    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Ward A')
    expect(within(d).getByLabelText(/^Code/)).toHaveValue('WARD-A')
    expect(within(d).getByLabelText(/^Code/)).toBeDisabled()
    expect(within(d).getByLabelText(/^Code/)).toHaveAccessibleDescription("Can't be changed")
    expect(within(d).getByRole('combobox', { name: /Kind/ })).toHaveTextContent('Ward')
    expect(within(d).getByRole('checkbox', { name: /Active — can receive stock/ })).toBeChecked()
    // Nothing to save until something is changed.
    expect(within(d).getByRole('button', { name: 'Save changes' })).toBeDisabled()

    fill(within(d).getByLabelText(/^Name/), ' Ward A (medical) ')
    await choose(user, within(d).getByRole('combobox', { name: /Kind/ }), 'ICU')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Ward A (medical)'))
    const [request] = sent('patch', '/inventory/locations/l-WARD-A')
    expect(address(request)).toBe('/api/v1/inventory/locations/l-WARD-A')
    const body = bodyOf(request)
    // The status was not touched, so it is not sent.
    expect(body).toEqual({ name: 'Ward A (medical)', kind: 'icu' })
    expect(body).not.toHaveProperty('code')
    const row = await screen.findByRole('row', { name: /Ward A \(medical\)/ })
    await waitFor(() => expect(cellsOf(row).slice(0, 4)).toEqual(['Ward A (medical)', 'WARD-A', 'ICU', 'Active']))
  })

  it('switches a location off with is_active alone, saying what happens to the stock it holds', async () => {
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'General store')
    // The API switches a location off whatever it holds; the form says what that means.
    expect(d).toHaveTextContent('Switching a location off does not move or remove what it holds.')
    expect(d).toHaveTextContent('nothing can be transferred or received into the location')

    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved General store'))
    expect(bodyOf(sent('patch', '/inventory/locations/l-STORE')[0])).toEqual({ is_active: false })
    expect(toastError).not.toHaveBeenCalled()
    const row = await screen.findByRole('row', { name: /General store/ })
    await waitFor(() => expect(within(row).getByText('Inactive')).toBeInTheDocument())
  })

  it('switches an inactive location back on', async () => {
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward Z (closed)')
    expect(within(d).getByRole('checkbox', { name: /Active/ })).not.toBeChecked()

    await user.click(within(d).getByRole('checkbox', { name: /Active/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Ward Z (closed)'))
    expect(bodyOf(sent('patch', '/inventory/locations/l-WARD-Z')[0])).toEqual({ is_active: true })
  })

  it('sends nothing when the only change disappears on trimming', async () => {
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward A')

    fill(within(d).getByLabelText(/^Name/), 'Ward A  ')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Nothing to save — the location is unchanged'))
    expect(writes()).toHaveLength(0)
  })

  it("shows the API's message when the location has gone, and reads the list again", async () => {
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward A')
    locations = locations.filter((l) => l.code !== 'WARD-A')

    fill(within(d).getByLabelText(/^Name/), 'Ward A (medical)')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Inventory location not found.'))
    expect(toastSuccess).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('row', { name: /Ward A/ })).not.toBeInTheDocument())
  })

  it('shows a whole-body 422, which names no field, above the form', async () => {
    intercept = (c) => (c.method === 'patch' ? invalid('', 'Value error, Cannot be null: name.') : undefined)
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward A')
    fill(within(d).getByLabelText(/^Name/), 'Ward A (medical)')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByText('Cannot be null: name.')).toBeInTheDocument()
    // The form itself never sends a null name: the body it sent is the rename.
    expect(bodyOf(sent('patch', '/inventory/locations/l-WARD-A')[0])).toEqual({ name: 'Ward A (medical)' })
  })

  it.each([
    [403, 'Permission denied. Required: inventory.location.update.', 'Permission denied. Required: inventory.location.update.'],
    [500, 'boom: could not serialize access', "Couldn't save the location. Please try again."],
  ])('on a %i from an edit shows what the user can act on, never raw server text', async (status, serverMessage, shown) => {
    intercept = (c) => (c.method === 'patch' ? fail(status, serverMessage) : undefined)
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward A')
    fill(within(d).getByLabelText(/^Name/), 'Ward A (medical)')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(toastError).toHaveBeenCalledWith(shown)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toMatch(/boom/)
    expect(sent('patch', '/inventory/locations/l-WARD-A')).toHaveLength(1)
    expect(locations.find((l) => l.code === 'WARD-A')!.name).toBe('Ward A')
    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Ward A (medical)')
  })

  it('shows a 422 that names the status under its checkbox, which the explanation describes', async () => {
    intercept = (c) => (c.method === 'patch' ? invalid('is_active', 'Input should be a valid boolean') : undefined)
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward A')
    const box = within(d).getByRole('checkbox', { name: 'Active — can receive stock' })
    expect(box).toHaveAccessibleDescription(
      'Switching a location off does not move or remove what it holds. That stock can still be used, corrected or transferred out, ' +
        'but nothing can be transferred or received into the location.',
    )

    await user.click(box)
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    // The toast says to check the highlighted fields, so one has to be.
    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields."))
    expect(box).toBeInvalid()
    expect(within(d).getByRole('alert')).toHaveTextContent(/^Input should be a valid boolean$/)
    expect(box).toHaveAccessibleDescription(/Input should be a valid boolean$/)
  })

  it('saves once however many times the form is submitted', async () => {
    const release = hold((c) => c.method === 'patch')
    const user = userEvent.setup()
    const d = await openEditLocation(user, 'Ward A')
    fill(within(d).getByLabelText(/^Name/), 'Ward A (medical)')
    const form = within(d).getByRole('button', { name: 'Save changes' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Saving…' })).toBeDisabled()
    expect(sent('patch', '/inventory/locations/l-WARD-A')).toHaveLength(1)
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('patch', '/inventory/locations/l-WARD-A')).toHaveLength(1)
    expect(bodyOf(sent('patch', '/inventory/locations/l-WARD-A')[0])).toEqual({ name: 'Ward A (medical)' })
    expect(toastError).not.toHaveBeenCalled()
  })

  it('keeps a long, unbroken location name out of the dialog title, and lets it wrap', async () => {
    const long = 'L'.repeat(200)
    locations = [location('LONG', long, 'store')]
    const user = userEvent.setup()
    const d = await openEditLocation(user, long)

    // A dialog title does not break; the description can.
    expect(within(d).getByRole('heading')).toHaveTextContent(/^Edit location$/)
    expect(within(d).getByText(long)).toHaveClass('[overflow-wrap:anywhere]')
    expect(d).toHaveAccessibleDescription(new RegExp(`^Editing ${long}\\.`))
  })
})
