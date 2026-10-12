import type { ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import { ApiError } from '@/api/types'
import type {
  InventoryItem,
  InventoryLocation,
  ItemStockSummary,
  MovementReason,
  StockMovement,
  StockRow,
} from '@/api/inventory'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import InventoryOverviewPage from './InventoryOverviewPage'
import MovementsPage from './MovementsPage'
import StockPage from './StockPage'
import {
  adjustDefaults,
  adjustResult,
  adjustSchema,
  consumeDefaults,
  consumeSchema,
  consumeStatement,
  idParam,
  outcomeUnknown,
  referenceLabel,
  toAdjustBody,
  toConsumeBody,
  toTransferBody,
  transferDefaults,
  transferSchema,
} from './stockActionForms'

/**
 * The inventory overview, the stock list, the ledger and the three writes on
 * stock, against the merged backend contract (docs/18-API_CONTRACTS.md §10;
 * `backend/app/api/v1/inventory.py`, `backend/app/schemas/inventory.py`,
 * `backend/app/services/inventory_service.py`). The real hooks, permission
 * check, `http` wrapper and Axios instance run; only the network adapter is
 * replaced by an in-memory "server" that keeps the items, locations, stock
 * rows and ledger and — like the real one — is the only thing that knows
 * today's date, decides what is expired or low, and does the arithmetic.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → permissions as seeded in backend/app/seeds/seed.py (§10.2).
const INVENTORY_ALL = [
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
const ADMIN = [...INVENTORY_ALL, 'pharmacy.vendor.read', 'department.read', 'notification.read.own']
const INVENTORY_MANAGER = [...INVENTORY_ALL, 'pharmacy.vendor.read', 'pharmacy.po.read', 'department.read', 'notification.read.own']
// A nurse records what the ward uses and nothing else.
const NURSE = [
  'inventory.item.read',
  'inventory.location.read',
  'inventory.stock.read',
  'inventory.consume',
  'department.read',
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
  'department.read',
  'pharmacy.vendor.read',
  'notification.read.own',
]
// None of these holds an inventory code.
const DOCTOR = ['patient.read', 'appointment.read', 'pharmacy.medicine.read', 'department.read', 'notification.read.own']
const RECEPTIONIST = ['patient.read', 'appointment.read', 'appointment.book', 'invoice.read', 'notification.read.own']
const BILLING = ['invoice.read', 'payment.record', 'notification.read.own']
const LAB_TECHNICIAN = ['lab.order.read', 'lab.result.enter', 'notification.read.own']

// ── The in-memory server ────────────────────────────────────────────────────

/**
 * The hospital's "today", which only the server knows. Deliberately not the
 * day the tests run: a screen that judged expiry from the browser's clock
 * would disagree with every flag below.
 */
const TODAY = '2024-02-10'

const dayNumber = (date: string) =>
  Date.UTC(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10))) / 86_400_000
const daysFromToday = (days: number) => new Date((dayNumber(TODAY) + days) * 86_400_000).toISOString().slice(0, 10)

const uid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`

/** A stock row as the server stores it: the quantity in hundredths, so its sums are exact. */
interface Stored {
  id: string
  item_id: string
  location_id: string
  batch_number: string | null
  expiry_date: string | null
  cents: number
}

let items: InventoryItem[]
let locations: InventoryLocation[]
let stock: Stored[]
let ledger: StockMovement[]
let departments: { id: string; code: string; name: string; location: string | null; status: string }[]
let sequence: number
/** What the server lets this user do — the live set, which can drift from the one taken at sign-in. */
let granted: string[]
/** Return an outcome to answer a request yourself; nothing to let the server answer. */
let intercept: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome> | undefined
let fake: FakeApi
let client: QueryClient

/** `NUMERIC(12, 2)` on the wire: always two places. */
const dec = (cents: number) =>
  `${cents < 0 ? '-' : ''}${Math.floor(Math.abs(cents) / 100)}.${String(Math.abs(cents) % 100).padStart(2, '0')}`
/** The service's `_plain`: 20, not 20.00. */
const plain = (cents: number) => String(cents / 100)

function item(n: number, sku: string, name: string, extra: Partial<InventoryItem> = {}): InventoryItem {
  return {
    id: uid(n),
    sku,
    name,
    category: null,
    unit_of_measure: 'unit',
    is_batch_tracked: false,
    reorder_point: null,
    target_stock: null,
    is_active: true,
    created_at: '2024-02-01T00:00:00Z',
    updated_at: '2024-02-01T00:00:00Z',
    ...extra,
  }
}

const GLOVE = uid(1)
const CANNULA = uid(2)
const GAUZE = uid(3)
const OXYGEN = uid(4)
const MASK = uid(5)
const STORE = uid(101)
const WARD = uid(102)
const ICU = uid(103)
const ANNEX = uid(104)
const SURGERY = uid(201)
const ORDER = uid(301)

function hold(itemId: string, locationId: string, batch: string | null, expiresIn: number | null, quantity: string): Stored {
  return {
    id: uid(1000 + stock.length),
    item_id: itemId,
    location_id: locationId,
    batch_number: batch,
    expiry_date: expiresIn === null ? null : daysFromToday(expiresIn),
    cents: Math.round(Number(quantity) * 100),
  }
}

function entry(row: Stored, cents: number, reason: MovementReason, extra: Partial<StockMovement> = {}): StockMovement {
  sequence += 1
  const movement: StockMovement = {
    id: uid(5000 + sequence),
    item_id: row.item_id,
    location_id: row.location_id,
    stock_id: row.id,
    batch_number: row.batch_number,
    quantity_change: dec(cents),
    reason,
    department_id: null,
    reference_type: null,
    reference_id: null,
    note: null,
    moved_at: new Date(Date.UTC(2024, 1, 10, 8, 0, sequence)).toISOString(),
    moved_by: uid(9001),
    ...extra,
  }
  // Newest first.
  ledger.unshift(movement)
  return movement
}

const usable = (row: Stored) => row.cents > 0 && (row.expiry_date === null || row.expiry_date >= TODAY)
const itemOf = (id: unknown) => items.find((i) => i.id === id)
const locationOf = (id: unknown) => locations.find((l) => l.id === id)

function view(row: Stored): StockRow {
  const i = itemOf(row.item_id) as InventoryItem
  const l = locationOf(row.location_id) as InventoryLocation
  return {
    id: row.id,
    item_id: i.id,
    item_sku: i.sku,
    item_name: i.name,
    unit_of_measure: i.unit_of_measure,
    location_id: l.id,
    location_code: l.code,
    location_name: l.name,
    batch_number: row.batch_number,
    expiry_date: row.expiry_date,
    quantity: dec(row.cents),
    is_expired: row.expiry_date !== null && row.expiry_date < TODAY,
  }
}

/** `ItemStockSummaryResponse.build`: hospital-wide, expired stock held but not usable. */
function summaryOf(i: InventoryItem): ItemStockSummary {
  const held = stock.filter((row) => row.item_id === i.id)
  const onHand = held.reduce((sum, row) => sum + row.cents, 0)
  const usableCents = held.filter(usable).reduce((sum, row) => sum + row.cents, 0)
  const isLow = i.reorder_point !== null && usableCents <= i.reorder_point * 100
  const target = i.target_stock ?? i.reorder_point ?? 0
  return {
    item: structuredClone(i),
    quantity_on_hand: dec(onHand),
    usable_quantity: dec(usableCents),
    is_low: isLow,
    suggested_order_quantity: dec(isLow ? Math.max(target * 100 - usableCents, 0) : 0),
  }
}

const CODE = /^[A-Z0-9][A-Z0-9_./-]*$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const QUANTITY = /^\d{1,10}(\.\d{1,2})?$/

/** A request-validation 422: `errors` is a list, and custom messages carry Pydantic's prefix. */
const invalid = (field: string, message: string) =>
  fail(422, 'Validation failed.', { error_code: 'VALIDATION_ERROR', errors: [{ field, message }] })
/** A 422 raised by the service: `errors` is an object, and the message is the sentence itself. */
const refused = (field: string, message: string) =>
  fail(422, message, { error_code: 'VALIDATION_ERROR', errors: { errors: [{ field, message }] } })
const rule = (message: string, errors: unknown = null) =>
  fail(400, message, { error_code: 'BUSINESS_RULE_VIOLATION', errors })

/** Query parameters as they go on the wire: an undefined one is not sent at all. */
const wire = (config: InternalAxiosRequestConfig) =>
  JSON.parse(JSON.stringify(config.params ?? {})) as Record<string, unknown>

function page<T>(config: InternalAxiosRequestConfig, rows: T[], message: string): Outcome {
  const { page: number = 1, page_size: size = 25 } = wire(config) as { page?: number; page_size?: number }
  if (size < 1 || size > 100) return invalid('query.page_size', 'Input should be less than or equal to 100')
  return {
    status: 200,
    data: {
      success: true,
      message,
      data: rows.slice((number - 1) * size, number * size),
      metadata: {
        request_id: null,
        pagination: { page: number, page_size: size, total_records: rows.length, total_pages: Math.max(1, Math.ceil(rows.length / size)) },
      },
    },
  }
}

/** The catalog filter both the item list and the summary use: a name prefix or a whole SKU; an exact category. */
function catalog(params: Record<string, unknown>): InventoryItem[] {
  const term = typeof params.q === 'string' ? params.q : ''
  return items
    .filter(
      (i) =>
        (!term || i.name.toLowerCase().startsWith(term.toLowerCase()) || i.sku === term.toUpperCase()) &&
        (params.category === undefined || i.category === params.category) &&
        (params.is_active === undefined || i.is_active === params.is_active),
    )
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** A filter that names a record must be a UUID; anything else is a 422, as is an empty one. */
function badId(params: Record<string, unknown>, ...names: string[]): Outcome | undefined {
  const name = names.find((n) => n in params && !(typeof params[n] === 'string' && UUID.test(params[n] as string)))
  return name ? invalid(`query.${name}`, 'Input should be a valid UUID') : undefined
}

/** `_allocate`: earliest expiry first, never empty or expired stock; writes nothing. */
function allocate(i: InventoryItem, l: InventoryLocation, cents: number, batch: string | null): [Stored, number][] | Outcome {
  const rows = stock
    .filter((row) => row.item_id === i.id && row.location_id === l.id && usable(row) && (batch === null || row.batch_number === batch))
    .sort((a, b) => (a.expiry_date ?? '9999').localeCompare(b.expiry_date ?? '9999') || a.id.localeCompare(b.id))
  const takes: [Stored, number][] = []
  let needed = cents
  for (const row of rows) {
    if (needed === 0) break
    const taken = Math.min(needed, row.cents)
    takes.push([row, taken])
    needed -= taken
  }
  if (needed > 0) {
    return fail(
      409,
      `Not enough ${i.name} at this location: ${plain(cents)} requested, ${plain(cents - needed)} usable. Nothing was changed.`,
      { error_code: 'RESOURCE_CONFLICT', errors: { requested: dec(cents), available: dec(cents - needed) } },
    )
  }
  return takes
}

function rowFor(i: InventoryItem, l: InventoryLocation, batch: string | null, expiry: string | null): Stored {
  let row = stock.find((r) => r.item_id === i.id && r.location_id === l.id && (r.batch_number ?? '') === (batch ?? ''))
  if (!row) {
    row = { id: uid(1000 + stock.length), item_id: i.id, location_id: l.id, batch_number: batch, expiry_date: expiry, cents: 0 }
    stock.push(row)
  }
  return row
}

/** What `ConsumeRequest` and `TransferRequest` check before a service sees the body. */
function movementBody(body: Record<string, unknown>, known: string[]): Outcome | { cents: number; batch: string | null } {
  const extra = Object.keys(body).find((key) => !known.includes(key))
  if (extra) return invalid(extra, 'Extra inputs are not permitted')
  const id = known.filter((key) => key.endsWith('_id')).find((key) => key in body && !(typeof body[key] === 'string' && UUID.test(body[key] as string)))
  if (id) return invalid(id, 'Input should be a valid UUID')
  // A quantity is a decimal string on the wire, never a JSON number.
  if (typeof body.quantity !== 'string' || !QUANTITY.test(body.quantity)) return invalid('quantity', 'Input should be a valid decimal')
  const cents = Math.round(Number(body.quantity) * 100)
  if (cents <= 0) return invalid('quantity', 'Input should be greater than 0')
  if ('note' in body && (typeof body.note !== 'string' || body.note.length > 500)) return invalid('note', 'String should have at most 500 characters')
  let batch: string | null = null
  if ('batch_number' in body) {
    if (typeof body.batch_number !== 'string' || body.batch_number.length > 50) return invalid('batch_number', 'String should have at most 50 characters')
    batch = body.batch_number.trim().toUpperCase()
    if (!CODE.test(batch)) return invalid('batch_number', 'Value error, Batch number may contain only letters, digits and the characters - _ . /')
  }
  return { cents, batch }
}

const isOutcome = (value: unknown): value is Outcome => typeof value === 'object' && value !== null && 'status' in value

function server(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const method = config.method ?? 'get'
  const lacks = (code: string) =>
    granted.includes(code)
      ? undefined
      : fail(403, `Permission denied. Required: ${code}.`, { error_code: 'PERMISSION_DENIED', errors: null })
  const params = wire(config)

  if (url === '/departments' && method === 'get') {
    return lacks('department.read') ?? page(config, departments, 'Departments retrieved.')
  }

  if (url === '/inventory/items' && method === 'get') {
    const denied = lacks('inventory.item.read')
    if (denied) return denied
    if ('q' in params && (typeof params.q !== 'string' || params.q.length > 200)) return invalid('query.q', 'String should have at most 200 characters')
    return page(config, catalog(params).map((i) => structuredClone(i)), 'Items retrieved.')
  }

  if (url.startsWith('/inventory/items/') && method === 'get') {
    const denied = lacks('inventory.item.read')
    if (denied) return denied
    const found = itemOf(url.slice('/inventory/items/'.length))
    return found
      ? ok(structuredClone(found))
      : fail(404, 'Inventory item not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: null })
  }

  if (url === '/inventory/locations' && method === 'get') {
    const denied = lacks('inventory.location.read')
    if (denied) return denied
    // Not paginated: the whole list, by name.
    return ok(
      locations
        .filter((l) => params.is_active === undefined || l.is_active === params.is_active)
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((l) => structuredClone(l)),
    )
  }

  if (url === '/inventory/stock' && method === 'get') {
    const denied = lacks('inventory.stock.read') ?? badId(params, 'item_id', 'location_id')
    if (denied) return denied
    if ('in_stock_only' in params && typeof params.in_stock_only !== 'boolean') return invalid('query.in_stock_only', 'Input should be a valid boolean')
    const inStockOnly = params.in_stock_only ?? true
    const rows = stock
      .filter(
        (row) =>
          (!params.item_id || row.item_id === params.item_id) &&
          (!params.location_id || row.location_id === params.location_id) &&
          (!inStockOnly || row.cents > 0),
      )
      .map(view)
      .sort(
        (a, b) =>
          a.item_name.localeCompare(b.item_name) ||
          a.location_name.localeCompare(b.location_name) ||
          (a.expiry_date ?? '9999').localeCompare(b.expiry_date ?? '9999') ||
          a.id.localeCompare(b.id),
      )
    return page(config, rows, 'Stock retrieved.')
  }

  if (url === '/inventory/stock/summary' && method === 'get') {
    const denied = lacks('inventory.stock.read')
    if (denied) return denied
    if ('low_stock' in params && typeof params.low_stock !== 'boolean') return invalid('query.low_stock', 'Input should be a valid boolean')
    if ('q' in params && (typeof params.q !== 'string' || params.q.length > 200)) return invalid('query.q', 'String should have at most 200 characters')
    if ('category' in params && (typeof params.category !== 'string' || params.category.length > 100)) return invalid('query.category', 'String should have at most 100 characters')
    // Active items only, including one that holds nothing at all.
    const summaries = catalog({ ...params, is_active: true }).map(summaryOf)
    return page(config, params.low_stock ? summaries.filter((s) => s.is_low) : summaries, 'Stock summary retrieved.')
  }

  if (url === '/inventory/movements' && method === 'get') {
    const denied = lacks('inventory.stock.read') ?? badId(params, 'item_id', 'location_id')
    if (denied) return denied
    const reasons = ['received', 'consumed', 'transferred_in', 'transferred_out', 'adjusted', 'expired']
    if ('reason' in params && !reasons.includes(params.reason as string)) return invalid('query.reason', 'Input should be a valid reason')
    const rows = ledger.filter(
      (m) =>
        (!params.item_id || m.item_id === params.item_id) &&
        (!params.location_id || m.location_id === params.location_id) &&
        (!params.reason || m.reason === params.reason),
    )
    return page(config, rows.map((m) => structuredClone(m)), 'Movements retrieved.')
  }

  if (url === '/inventory/consume' && method === 'post') {
    const denied = lacks('inventory.consume')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const checked = movementBody(body, ['item_id', 'location_id', 'quantity', 'department_id', 'batch_number', 'note'])
    if (isOutcome(checked)) return checked
    const i = itemOf(body.item_id)
    if (!i) return refused('item_id', 'Inventory item not found in this hospital.')
    if (!i.is_active) return rule(`Item '${i.sku}' is inactive.`)
    const l = locationOf(body.location_id)
    if (!l) return refused('location_id', 'Inventory location not found in this hospital.')
    if ('department_id' in body && !departments.some((d) => d.id === body.department_id)) {
      return refused('department_id', 'Department not found in this hospital.')
    }
    const takes = allocate(i, l, checked.cents, checked.batch)
    if (isOutcome(takes)) return takes
    const note = typeof body.note === 'string' && body.note.trim() ? body.note.trim() : null
    const movements = takes.map(([row, taken]) => {
      row.cents -= taken
      return entry(row, -taken, 'consumed', {
        department_id: (body.department_id as string | undefined) ?? null,
        reference_type: 'consumption',
        note,
      })
    })
    return ok({ movements: structuredClone(movements), summary: summaryOf(i) })
  }

  if (url === '/inventory/transfer' && method === 'post') {
    const denied = lacks('inventory.transfer')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const checked = movementBody(body, ['item_id', 'from_location_id', 'to_location_id', 'quantity', 'batch_number', 'note'])
    if (isOutcome(checked)) return checked
    if (body.from_location_id === body.to_location_id) return invalid('', 'Value error, A transfer needs two different locations.')
    const i = itemOf(body.item_id)
    if (!i) return refused('item_id', 'Inventory item not found in this hospital.')
    if (!i.is_active) return rule(`Item '${i.sku}' is inactive.`)
    const source = locationOf(body.from_location_id)
    if (!source) return refused('from_location_id', 'Inventory location not found in this hospital.')
    const destination = locationOf(body.to_location_id)
    if (!destination) return refused('to_location_id', 'Inventory location not found in this hospital.')
    if (!destination.is_active) return rule(`Location '${destination.code}' is inactive and cannot receive stock.`)
    const takes = allocate(i, source, checked.cents, checked.batch)
    if (isOutcome(takes)) return takes
    const note = typeof body.note === 'string' && body.note.trim() ? body.note.trim() : null
    const transferId = uid(7000 + sequence)
    const movements: StockMovement[] = []
    for (const [row, taken] of takes) {
      row.cents -= taken
      movements.push(entry(row, -taken, 'transferred_out', { reference_type: 'transfer', reference_id: transferId, note }))
      const target = rowFor(i, destination, row.batch_number, row.expiry_date)
      target.cents += taken
      movements.push(entry(target, taken, 'transferred_in', { reference_type: 'transfer', reference_id: transferId, note }))
    }
    // The totals a transfer answers with are the hospital's, which it does not change.
    return ok({ movements: structuredClone(movements), summary: summaryOf(i) })
  }

  if (url === '/inventory/adjust' && method === 'post') {
    const denied = lacks('inventory.adjust')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const known = ['item_id', 'location_id', 'quantity_change', 'reason', 'note', 'batch_number', 'expiry_date']
    const extra = Object.keys(body).find((key) => !known.includes(key))
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    if (typeof body.quantity_change !== 'string' || !/^-?\d{1,10}(\.\d{1,2})?$/.test(body.quantity_change)) {
      return invalid('quantity_change', 'Input should be a valid decimal')
    }
    const change = Math.round(Number(body.quantity_change) * 100)
    if (body.reason !== 'adjusted' && body.reason !== 'expired') return invalid('reason', "Input should be 'adjusted' or 'expired'")
    if (typeof body.note !== 'string' || body.note.length < 1 || body.note.length > 500) return invalid('note', 'String should have at least 1 character')
    if (!body.note.trim()) return invalid('note', 'Value error, Note must not be blank.')
    let batch: string | null = null
    if ('batch_number' in body) {
      batch = String(body.batch_number).trim().toUpperCase()
      if (!CODE.test(batch)) return invalid('batch_number', 'Value error, Batch number may contain only letters, digits and the characters - _ . /')
    }
    if ('expiry_date' in body && !/^\d{4}-\d{2}-\d{2}$/.test(String(body.expiry_date))) return invalid('expiry_date', 'Input should be a valid date')
    if (change === 0) return invalid('', 'Value error, quantity_change must not be zero.')
    if (body.reason === 'expired' && change > 0) return invalid('', 'Value error, Writing off expired stock must remove units.')
    // An inactive item can still be corrected.
    const i = itemOf(body.item_id)
    if (!i) return refused('item_id', 'Inventory item not found in this hospital.')
    const l = locationOf(body.location_id)
    if (!l) return refused('location_id', 'Inventory location not found in this hospital.')
    if (i.is_batch_tracked && batch === null) return refused('batch_number', `${i.name} is batch-tracked: give a batch number.`)
    if (!i.is_batch_tracked && batch !== null) return refused('batch_number', `${i.name} is not batch-tracked.`)
    const before = stock.length
    const row = rowFor(i, l, batch, (body.expiry_date as string | undefined) ?? null)
    if (row.cents + change < 0) {
      // Nothing is kept, the row it may have just created included.
      stock.length = before
      return rule(`${l.name} holds ${dec(row.cents)} of ${i.name}; ${dec(-change)} cannot be removed.`, { quantity: dec(row.cents) })
    }
    row.cents += change
    const movement = entry(row, change, body.reason, { reference_type: 'adjustment', note: body.note.trim() })
    return ok({ movements: [structuredClone(movement)], summary: summaryOf(i) })
  }

  return fail(404, 'Not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: null })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  sequence = 0
  items = [
    item(1, 'GLOVE-M', 'Nitrile gloves, medium', { category: 'Disposables', unit_of_measure: 'box of 100', reorder_point: 20, target_stock: 80 }),
    item(2, 'CANN-20G', 'IV cannula 20G', { category: 'Sterile', unit_of_measure: 'piece', is_batch_tracked: true, reorder_point: 100, target_stock: 400 }),
    item(3, 'GAUZE-ST', 'Sterile gauze swab', { category: 'Sterile', unit_of_measure: 'pack of 10', is_batch_tracked: true, reorder_point: 50, target_stock: 200 }),
    item(4, 'O2-B', 'Oxygen cylinder, B type', { category: 'Gases', unit_of_measure: 'cylinder' }),
    item(5, 'MASK-3P', 'Surgical mask, 3-ply', { category: 'Disposables', unit_of_measure: 'box of 50', reorder_point: 30, target_stock: 100 }),
    item(6, 'GLOVE-LX', 'Latex gloves (withdrawn)', { category: 'Disposables', unit_of_measure: 'box of 100', is_active: false }),
  ]
  locations = [
    { id: STORE, name: 'General store', code: 'STORE', kind: 'store', is_active: true },
    { id: WARD, name: 'Ward A', code: 'WARD-A', kind: 'ward', is_active: true },
    { id: ICU, name: 'Intensive care unit', code: 'ICU', kind: 'icu', is_active: true },
    { id: ANNEX, name: 'Old annex', code: 'ANNEX', kind: 'store', is_active: false },
  ]
  stock = []
  stock.push(hold(GLOVE, STORE, null, null, '60'))
  stock.push(hold(GLOVE, WARD, null, null, '4'))
  stock.push(hold(CANNULA, STORE, 'CN-2401', 25, '80'))
  stock.push(hold(CANNULA, STORE, 'CN-2502', 400, '300'))
  stock.push(hold(CANNULA, ICU, 'CN-2502', 400, '40'))
  // Past its expiry on the hospital's date: held, never usable.
  stock.push(hold(GAUZE, STORE, 'GZ-2310', -30, '25'))
  stock.push(hold(GAUZE, STORE, 'GZ-2504', 300, '150'))
  stock.push(hold(OXYGEN, STORE, null, null, '2.50'))
  stock.push(hold(MASK, STORE, null, null, '18'))
  // A row that holds nothing: listed only when asked for.
  stock.push(hold(MASK, WARD, null, null, '0'))
  departments = [{ id: SURGERY, code: 'SURG', name: 'Surgery', location: null, status: 'active' }]
  ledger = []
  entry(stock[3], 30000, 'received', { reference_type: 'purchase_order', reference_id: ORDER })
  entry(stock[1], -200, 'consumed', { reference_type: 'consumption', department_id: SURGERY, note: 'Morning round' })
  entry(stock[7], -50, 'adjusted', { reference_type: 'adjustment', note: 'Leaking valve' })
  granted = []
  intercept = () => undefined
  fake = installFakeApi((config) => intercept(config) ?? server(config))
})

afterEach(() => {
  fake.restore()
  signOut()
})

/** Shows where the router is, so a test can read the address bar. */
function Address() {
  const { pathname, search } = useLocation()
  return <output aria-label="address">{`${pathname}${search}`}</output>
}

/** Each page behind the permission the router puts in front of it. `/inventory/everything` has all three open at once. */
function renderAt(path: string, permissions: string[]) {
  granted = [...permissions]
  signIn(permissions)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const guarded = (element: ReactNode) => (
    <RequirePermission permission="inventory.stock.read">{element}</RequirePermission>
  )
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Address />
        <Routes>
          <Route path="/dashboard" element={<p>Dashboard home</p>} />
          <Route path="/inventory" element={guarded(<InventoryOverviewPage />)} />
          <Route path="/inventory/stock" element={guarded(<StockPage />)} />
          <Route path="/inventory/movements" element={guarded(<MovementsPage />)} />
          <Route
            path="/inventory/everything"
            element={guarded(
              <>
                <StockPage />
                <InventoryOverviewPage />
                <MovementsPage />
              </>,
            )}
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

type User = ReturnType<typeof userEvent.setup>

/** Requests to exactly this path (`/inventory/stock` is also the start of the summary's). */
const sent = (method: string, url: string) => fake.sent.filter((c) => c.method === method && c.url === url)
const address = (config: InternalAxiosRequestConfig) => `${config.baseURL}${config.url}`
const shownAddress = () => screen.getByLabelText('address').textContent
const dialog = () => screen.findByRole('dialog')
const skeletons = () => document.querySelectorAll('[data-slot="skeleton"]').length
const choose = async (user: User, combobox: HTMLElement, option: string) => {
  await waitFor(() => expect(combobox).toBeEnabled())
  await user.click(combobox)
  await user.click(await screen.findByRole('option', { name: option }))
}
const cellsOf = (row: HTMLElement) => within(row).getAllByRole('cell').map((cell) => cell.textContent)
/** A calendar day as the screen should show it, built without parsing the string. */
const shownDate = (date: string) =>
  new Date(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10))).toLocaleDateString(undefined, {
    dateStyle: 'medium',
  })
/** Hold the answer to one kind of request until `release()` is called. */
function holdBack(matches: (config: InternalAxiosRequestConfig) => boolean) {
  let release: () => void = () => {}
  intercept = (c) => (matches(c) ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c)))) : undefined)
  return () => release()
}
const snapshot = () => JSON.stringify({ stock, ledger })
const stockLoaded = () => screen.findByRole('row', { name: /IV cannula 20G.*CN-2401/ })
const ledgerLoaded = () => screen.findByRole('row', { name: /Morning round/ })
/** Stock, the overview and the ledger open together — what a write must refresh. Read before a dialog opens over them. */
const EVERYTHING = '/inventory/everything'
/** Choose an item in an open dialog's picker by typing the start of its name. */
const pickItem = async (user: User, d: HTMLElement, typed: string, name: RegExp) => {
  await user.type(within(d).getByLabelText(/^Item/), typed)
  await user.click(await within(d).findByRole('button', { name }))
}

const MINUS = '−'
const ROW_2401 = 'IV cannula 20G at General store, batch CN-2401'
const ROW_2502 = 'IV cannula 20G at General store, batch CN-2502'

// ── Access ──────────────────────────────────────────────────────────────────

describe('inventory access', () => {
  it.each([
    ['a doctor', DOCTOR],
    ['a receptionist', RECEPTIONIST],
    ['billing staff', BILLING],
    ['a lab technician', LAB_TECHNICIAN],
  ])('sends %s, who holds no inventory permission, to the dashboard and asks the API nothing', async (_who, permissions) => {
    for (const path of ['/inventory', '/inventory/stock', '/inventory/movements']) {
      renderAt(path, permissions)
      expect((await screen.findAllByText('Dashboard home')).length).toBeGreaterThan(0)
    }
    expect(fake.sent).toHaveLength(0)
  })

  it('shows a nurse Consume and neither Transfer nor Adjust', async () => {
    renderAt('/inventory/stock', NURSE)
    await stockLoaded()

    expect(screen.getByRole('button', { name: `Consume ${ROW_2401}` })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Record stock used' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Transfer/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Adjust/ })).not.toBeInTheDocument()
    expect(fake.sent.every((c) => c.method === 'get')).toBe(true)
  })

  it('shows a pharmacist the stock and none of the three writes', async () => {
    renderAt('/inventory/stock', PHARMACIST)
    await stockLoaded()

    expect(screen.queryByRole('button', { name: /^(Consume|Transfer|Adjust|Record stock used)/ })).not.toBeInTheDocument()
    // No empty actions column either.
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Item', 'Location', 'Batch', 'Expiry', 'Quantity'])
    expect(fake.sent.every((c) => c.method === 'get')).toBe(true)
  })

  it.each([
    ['a hospital admin', ADMIN],
    ['an inventory manager', INVENTORY_MANAGER],
  ])('shows %s all three writes, on the row and in the header', async (_who, permissions) => {
    renderAt('/inventory/stock', permissions)
    await stockLoaded()

    for (const action of ['Consume', 'Transfer', 'Adjust']) {
      expect(screen.getByRole('button', { name: `${action} ${ROW_2401}` })).toBeInTheDocument()
    }
    for (const action of ['Record stock used', 'Transfer stock', 'Adjust stock']) {
      expect(screen.getByRole('button', { name: action })).toBeInTheDocument()
    }
  })

  it('asks only for what the user may read: stock alone means no item, location or department request', async () => {
    renderAt(EVERYTHING, ['inventory.stock.read'])
    await stockLoaded()
    await screen.findByRole('row', { name: /Morning round/ })

    expect([...new Set(fake.sent.map((c) => `${c.method} ${c.url}`))].sort()).toEqual([
      'get /inventory/movements',
      'get /inventory/stock',
      'get /inventory/stock/summary',
    ])
    expect(screen.getAllByText("You don't have access to the item list.").length).toBeGreaterThan(0)
    expect(screen.getAllByText("You don't have access to the location list.").length).toBeGreaterThan(0)
  })

  it('lists the sections a nurse may open, and not purchase orders', async () => {
    renderAt('/inventory', NURSE)
    await screen.findByRole('row', { name: /IV cannula 20G/ })

    const nav = within(screen.getByRole('navigation', { name: 'Inventory sections' }))
    expect(nav.getAllByRole('link').map((a) => a.textContent)).toEqual(['Overview', 'Stock', 'Movements', 'Items', 'Locations'])
  })
})

// ── Overview ────────────────────────────────────────────────────────────────

describe('inventory overview', () => {
  const summaryRequests = () => sent('get', '/inventory/stock/summary')
  const loaded = () => screen.findByRole('row', { name: /IV cannula 20G/ })
  const box = () => screen.getByRole('textbox', { name: 'Search by start of name or whole SKU…' })

  it('lists each active item with the server\'s totals, low flag and suggestion', async () => {
    renderAt('/inventory', PHARMACIST)

    expect(cellsOf(await loaded())).toEqual(['IV cannula 20GCANN-20G', 'Sterile', '420 piece', '420 piece', 'OK', '—'])
    // 25 of the gauze has expired: held, not usable.
    expect(cellsOf(screen.getByRole('row', { name: /Sterile gauze swab/ }))).toEqual([
      'Sterile gauze swabGAUZE-ST',
      'Sterile',
      '175 pack of 10',
      '150 pack of 10',
      'OK',
      '—',
    ])
    expect(cellsOf(screen.getByRole('row', { name: /Surgical mask/ }))).toEqual([
      'Surgical mask, 3-plyMASK-3P',
      'Disposables',
      '18 box of 50',
      '18 box of 50',
      'Low',
      '82 box of 50',
    ])
    // No precision is invented: "2.50" is 2.5.
    expect(cellsOf(screen.getByRole('row', { name: /Oxygen cylinder/ })).slice(2, 5)).toEqual(['2.5 cylinder', '2.5 cylinder', 'OK'])
    // By name, as the API orders them; the inactive item is not in the summary.
    expect(screen.getAllByRole('row').slice(1).map((row) => cellsOf(row)[0])).toEqual([
      'IV cannula 20GCANN-20G',
      'Nitrile gloves, mediumGLOVE-M',
      'Oxygen cylinder, B typeO2-B',
      'Sterile gauze swabGAUZE-ST',
      'Surgical mask, 3-plyMASK-3P',
    ])
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Item',
      'Category',
      'On hand',
      'Usable (not expired)',
      'Status',
      'Suggested order',
    ])
    expect(screen.getByText(/Low stock is judged by the server.*at or below the item's reorder point/)).toBeInTheDocument()
    // `ItemStockSummaryResponse.build`: the target, or the reorder point when the item has none.
    expect(
      screen.getByText(/back to the item's target\s+stock, or to its reorder point when no target is set\./),
    ).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Inventory' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Stock of IV cannula 20G' })).toHaveAttribute('href', `/inventory/stock?item_id=${CANNULA}`)
    expect(screen.queryByText(/forecast/i)).not.toBeInTheDocument()

    const [request] = summaryRequests()
    expect(request.method).toBe('get')
    expect(address(request)).toBe('/api/v1/inventory/stock/summary')
    // No empty filter is sent, and `low_stock` only when asked for.
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    expect(fake.sent).toHaveLength(1)
  })

  it('puts low_stock on the wire only while the toggle is on, and keeps it in the address', async () => {
    const user = userEvent.setup()
    renderAt('/inventory', ADMIN)
    await loaded()
    const toggle = screen.getByRole('checkbox', { name: 'Low stock only' })
    expect(toggle).not.toBeChecked()

    await user.click(toggle)

    await waitFor(() => expect(screen.queryByRole('row', { name: /IV cannula 20G/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Surgical mask/ })).toBeInTheDocument()
    expect(wire(summaryRequests().at(-1)!)).toEqual({ low_stock: true, page: 1, page_size: 25 })
    expect(shownAddress()).toBe('/inventory?low_stock=1')

    await user.click(toggle)

    expect(await loaded()).toBeInTheDocument()
    expect(shownAddress()).toBe('/inventory')
    expect(summaryRequests().filter((c) => 'low_stock' in wire(c))).toHaveLength(1)
  })

  it('opens on the low list from a link, which is where the low-stock notification can point', async () => {
    renderAt('/inventory?low_stock=1', ADMIN)

    expect(await screen.findByRole('row', { name: /Surgical mask/ })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Low stock only' })).toBeChecked()
    expect(screen.getAllByRole('row')).toHaveLength(2)
    expect(summaryRequests().map(wire)).toEqual([{ low_stock: true, page: 1, page_size: 25 }])
  })

  it('says so when the server reports nothing low', async () => {
    stock.find((row) => row.item_id === MASK && row.location_id === STORE)!.cents = 9000
    renderAt('/inventory?low_stock=1', ADMIN)

    expect(await screen.findByText('No item is low on stock')).toBeInTheDocument()
  })

  it('sends the search trimmed, once the typing stops', async () => {
    let searchedAt = 0
    intercept = (c) => {
      if (c.url === '/inventory/stock/summary' && 'q' in wire(c)) searchedAt = performance.now()
      return undefined
    }
    renderAt('/inventory', ADMIN)
    await loaded()

    const typedAt = performance.now()
    fireEvent.change(box(), { target: { value: '  ster' } })
    fireEvent.change(box(), { target: { value: '  steri  ' } })
    expect(summaryRequests()).toHaveLength(1)

    // The server does not trim: sent as typed, this would match nothing.
    await waitFor(() => expect(screen.queryByRole('row', { name: /IV cannula 20G/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Sterile gauze swab/ })).toBeInTheDocument()
    expect(summaryRequests().map((c) => wire(c))).toEqual([
      { page: 1, page_size: 25 },
      { q: 'steri', page: 1, page_size: 25 },
    ])
    expect(searchedAt - typedAt).toBeGreaterThanOrEqual(250)
  })

  it('finds by whole SKU, sends nothing for spaces, and says why a part of a name finds nothing', async () => {
    const user = userEvent.setup()
    renderAt('/inventory', ADMIN)
    await loaded()

    fireEvent.change(box(), { target: { value: '   ' } })
    await new Promise((resolve) => setTimeout(resolve, 400))
    expect(summaryRequests().every((c) => !('q' in wire(c)))).toBe(true)

    await user.clear(box())
    await user.type(box(), 'o2-b')
    await waitFor(() => expect(screen.queryByRole('row', { name: /IV cannula 20G/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Oxygen cylinder/ })).toBeInTheDocument()

    await user.clear(box())
    await user.type(box(), 'cannula')
    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(screen.getByText(/the start of a name, or a whole SKU/)).toBeInTheDocument()
  })

  it('says the low list is on when a search of it finds nothing, so an item that is not low is not taken for missing', async () => {
    renderAt('/inventory?low_stock=1', ADMIN)
    await screen.findByRole('row', { name: /Surgical mask/ })

    // The cannula exists and is not low.
    fireEvent.change(box(), { target: { value: 'IV cannula' } })

    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(wire(summaryRequests().at(-1)!)).toEqual({ low_stock: true, q: 'IV cannula', page: 1, page_size: 25 })
    expect(
      screen.getByText(/Only items the server reports as low are listed — untick Low stock only to search every item\./),
    ).toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('checkbox', { name: 'Low stock only' }))
    expect(await loaded()).toBeInTheDocument()

    // Without the low list the sentence would be wrong, and is not shown.
    fireEvent.change(box(), { target: { value: 'cannula' } })
    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(screen.queryByText(/untick Low stock only/)).not.toBeInTheDocument()
  })

  it('filters by an exact category, trimmed, and omits it when blank', async () => {
    renderAt('/inventory', ADMIN)
    await loaded()
    // Labelled on screen, not by a placeholder that goes once something is typed.
    const category = screen.getByRole('textbox', { name: 'Category' })
    expect(screen.getByText('Category', { selector: 'label' })).toBeVisible()
    expect(category).not.toHaveAttribute('placeholder')
    expect(category).toHaveAccessibleDescription('Exactly as on the item, capitals included')

    fireEvent.change(category, { target: { value: ' Sterile ' } })

    await waitFor(() => expect(screen.queryByRole('row', { name: /Surgical mask/ })).not.toBeInTheDocument())
    expect(screen.getAllByRole('row')).toHaveLength(3)
    expect(wire(summaryRequests().at(-1)!)).toEqual({ category: 'Sterile', page: 1, page_size: 25 })

    fireEvent.change(category, { target: { value: '  ' } })
    expect(await screen.findByRole('row', { name: /Surgical mask/ })).toBeInTheDocument()
    expect(summaryRequests().filter((c) => 'category' in wire(c))).toHaveLength(1)
  })

  it('keeps the search within the 200 characters the API accepts', async () => {
    renderAt('/inventory', ADMIN)
    await loaded()

    fireEvent.change(box(), { target: { value: 'x'.repeat(250) } })

    // A longer one is a 422, which would show as a failure to load.
    expect(await screen.findByText('No matching items')).toBeInTheDocument()
    expect(wire(summaryRequests().at(-1)!).q).toBe('x'.repeat(200))
  })

  it('says there are no items yet when the catalog is empty', async () => {
    items = []
    stock = []
    renderAt('/inventory', ADMIN)

    expect(await screen.findByText('No items yet')).toBeInTheDocument()
    expect(screen.queryByText('No matching items')).not.toBeInTheDocument()
  })

  it('shows loading placeholders until the summary arrives', async () => {
    const release = holdBack((c) => c.url === '/inventory/stock/summary')
    renderAt('/inventory', ADMIN)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    expect(screen.queryByText('No items yet')).not.toBeInTheDocument()
    release()
    expect(await loaded()).toBeInTheDocument()
    expect(skeletons()).toBe(0)
  })

  it('offers a retry when the summary cannot be loaded, without the server\'s words', async () => {
    intercept = (c) => (c.url === '/inventory/stock/summary' ? fail(500, 'OperationalError: connection refused') : undefined)
    renderAt('/inventory', ADMIN)

    expect(await screen.findByText("Couldn't load the stock summary")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/OperationalError|connection refused/)

    intercept = () => undefined
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }))
    expect(await loaded()).toBeInTheDocument()
  })

  it('pages through the server\'s pages', async () => {
    items = Array.from({ length: 30 }, (_, n) => item(500 + n, `IT-${String(n).padStart(2, '0')}`, `Item ${String(n).padStart(2, '0')}`))
    stock = []
    const user = userEvent.setup()
    renderAt('/inventory', ADMIN)
    await screen.findByRole('row', { name: /Item 00/ })
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByRole('row', { name: /Item 29/ })).toBeInTheDocument()
    expect(wire(summaryRequests().at(-1)!)).toEqual({ page: 2, page_size: 25 })

    // A new filter starts again at the first page, in one request.
    await user.click(screen.getByRole('checkbox', { name: 'Low stock only' }))
    await waitFor(() => expect(wire(summaryRequests().at(-1)!)).toEqual({ low_stock: true, page: 1, page_size: 25 }))
  })
})

// ── Stock ───────────────────────────────────────────────────────────────────

describe('stock list', () => {
  const stockRequests = () => sent('get', '/inventory/stock')

  it('lists one row per batch at a location, with the server\'s quantity and expiry flag', async () => {
    renderAt('/inventory/stock', PHARMACIST)

    expect(cellsOf(await stockLoaded())).toEqual([
      'IV cannula 20GCANN-20G',
      'General store',
      'CN-2401',
      shownDate(daysFromToday(25)),
      '80 piece',
    ])
    // Expired on the server's date — which is not this browser's.
    expect(cellsOf(screen.getByRole('row', { name: /GZ-2310/ }))).toEqual([
      'Sterile gauze swabGAUZE-ST',
      'General store',
      'GZ-2310',
      `${shownDate(daysFromToday(-30))}Expired`,
      '25 pack of 10',
    ])
    expect(screen.getAllByText('Expired')).toHaveLength(1)
    expect(cellsOf(screen.getByRole('row', { name: /Oxygen cylinder/ }))).toEqual([
      'Oxygen cylinder, B typeO2-B',
      'General store',
      '—',
      '—',
      '2.5 cylinder',
    ])
    // The row that holds nothing is left out by the API unless asked for.
    expect(screen.getAllByRole('row')).toHaveLength(10)

    const [request] = stockRequests()
    expect(address(request)).toBe('/api/v1/inventory/stock')
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
  })

  it('sends in_stock_only=false only when empty rows are asked for', async () => {
    const user = userEvent.setup()
    renderAt('/inventory/stock', ADMIN)
    await stockLoaded()
    const toggle = screen.getByRole('checkbox', { name: /Include empty rows/ })

    await user.click(toggle)

    expect(await screen.findByRole('row', { name: /Surgical mask.*Ward A/ })).toBeInTheDocument()
    expect(wire(stockRequests().at(-1)!)).toEqual({ in_stock_only: false, page: 1, page_size: 25 })
    // An empty row can be adjusted — that is how stock is entered — but there is nothing to consume or move.
    expect(screen.getByRole('button', { name: 'Adjust Surgical mask, 3-ply at Ward A' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Consume Surgical mask, 3-ply at Ward A' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Transfer Surgical mask, 3-ply at Ward A' })).not.toBeInTheDocument()

    await user.click(toggle)
    await waitFor(() => expect(screen.queryByRole('row', { name: /Surgical mask.*Ward A/ })).not.toBeInTheDocument())
    expect(stockRequests().filter((c) => 'in_stock_only' in wire(c))).toHaveLength(1)
  })

  it('offers neither Consume nor Transfer on an expired batch, which the service would never draw on', async () => {
    renderAt('/inventory/stock', ADMIN)
    await stockLoaded()

    const expired = 'Sterile gauze swab at General store, batch GZ-2310'
    expect(screen.queryByRole('button', { name: `Consume ${expired}` })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: `Transfer ${expired}` })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: `Adjust ${expired}` })).toBeInTheDocument()
    // The batch beside it is in date and offers all three.
    expect(screen.getByRole('button', { name: 'Consume Sterile gauze swab at General store, batch GZ-2504' })).toBeInTheDocument()
  })

  it('filters by location and by item with the API\'s parameters, in the address', async () => {
    const user = userEvent.setup()
    renderAt('/inventory/stock', ADMIN)
    await stockLoaded()

    await choose(user, screen.getByRole('combobox', { name: 'Location' }), 'Intensive care unit')

    await waitFor(() => expect(screen.getAllByRole('row')).toHaveLength(2))
    expect(wire(stockRequests().at(-1)!)).toEqual({ location_id: ICU, page: 1, page_size: 25 })
    expect(shownAddress()).toBe(`/inventory/stock?location_id=${ICU}`)
    expect(screen.getByText('Showing part of the stock')).toBeInTheDocument()

    await choose(user, screen.getByRole('combobox', { name: 'Location' }), 'All locations')
    await stockLoaded()
    expect(shownAddress()).toBe('/inventory/stock')

    await user.type(screen.getByLabelText('Item'), ' nitr ')
    // The picker searches with the trimmed term, active items only.
    await waitFor(() =>
      expect(wire(sent('get', '/inventory/items').at(-1)!)).toEqual({ q: 'nitr', is_active: true, page: 1, page_size: 8 }),
    )
    await user.click(await screen.findByRole('button', { name: /^Nitrile gloves, medium/ }))

    await waitFor(() => expect(screen.getAllByRole('row')).toHaveLength(3))
    expect(wire(stockRequests().at(-1)!)).toEqual({ item_id: GLOVE, page: 1, page_size: 25 })
    expect(shownAddress()).toBe(`/inventory/stock?item_id=${GLOVE}`)
    // The chip that replaces the search box still answers to the "Item" label's id, and has a name.
    const chip = screen.getByRole('group', { name: 'Item filter: Nitrile gloves, medium' })
    expect(chip).toHaveAttribute('id', screen.getByText('Item', { selector: 'label' }).getAttribute('for'))
    expect(within(chip).getByRole('button', { name: 'Clear the item filter' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Clear the item filter' }))
    await stockLoaded()
    expect(wire(stockRequests().at(-1)!)).toEqual({ page: 1, page_size: 25 })
  })

  it('opens scoped from a link, names the scope and clears it', async () => {
    const user = userEvent.setup()
    renderAt(`/inventory/stock?item_id=${CANNULA}&location_id=${STORE}`, ADMIN)

    await stockLoaded()
    expect(screen.getAllByRole('row')).toHaveLength(3)
    expect(wire(stockRequests()[0])).toEqual({ item_id: CANNULA, location_id: STORE, page: 1, page_size: 25 })
    const banner = screen.getByText('Showing part of the stock').closest('[role="alert"]') as HTMLElement
    expect(banner).toHaveTextContent('Only IV cannula 20G at General store is listed.')
    expect(screen.getByRole('combobox', { name: 'Location' })).toHaveTextContent('General store')
    // The rows named the item: it was not read separately.
    expect(fake.sent.filter((c) => c.url?.startsWith('/inventory/items/'))).toHaveLength(0)

    await user.click(within(banner).getByRole('button', { name: 'Show all stock' }))

    expect(await screen.findByRole('row', { name: /Nitrile gloves.*Ward A/ })).toBeInTheDocument()
    expect(shownAddress()).toBe('/inventory/stock')
    expect(wire(stockRequests().at(-1)!)).toEqual({ page: 1, page_size: 25 })
  })

  it('reads the item\'s name when a linked item has no stock to name it', async () => {
    renderAt(`/inventory/stock?item_id=${uid(6)}`, ADMIN)

    expect(await screen.findByText('No stock matches')).toBeInTheDocument()
    // Until the name is read the chip does not pretend to one.
    expect(screen.getByRole('group', { name: /^Item filter: (one item|Latex gloves \(withdrawn\))$/ })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Only Latex gloves (withdrawn) is listed.'))
    expect(screen.getByRole('group', { name: 'Item filter: Latex gloves (withdrawn)' })).toBeInTheDocument()
    expect(sent('get', `/inventory/items/${uid(6)}`)).toHaveLength(1)
  })

  it('ignores an id in the address that cannot be one, rather than failing to load', async () => {
    renderAt('/inventory/stock?item_id=not-an-id&location_id=', ADMIN)

    await stockLoaded()
    expect(wire(stockRequests()[0])).toEqual({ page: 1, page_size: 25 })
    expect(screen.queryByText('Showing part of the stock')).not.toBeInTheDocument()
  })

  it('says there is no stock yet when nothing is held anywhere', async () => {
    stock = []
    renderAt('/inventory/stock', ADMIN)

    expect(await screen.findByText('No stock yet')).toBeInTheDocument()
    expect(screen.queryByText('No stock matches')).not.toBeInTheDocument()
  })

  it('shows loading placeholders, then the stock', async () => {
    const release = holdBack((c) => c.url === '/inventory/stock')
    renderAt('/inventory/stock', ADMIN)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    expect(screen.queryByText('No stock yet')).not.toBeInTheDocument()
    release()
    expect(await stockLoaded()).toBeInTheDocument()
  })

  it('offers a retry when the stock cannot be loaded, without the server\'s words', async () => {
    intercept = (c) => (c.url === '/inventory/stock' ? fail(500, 'Traceback: psycopg.OperationalError') : undefined)
    renderAt('/inventory/stock', ADMIN)

    expect(await screen.findByText("Couldn't load the stock")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/Traceback|psycopg/)

    intercept = () => undefined
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }))
    expect(await stockLoaded()).toBeInTheDocument()
  })

  it('pages through the server\'s pages', async () => {
    items = Array.from({ length: 30 }, (_, n) => item(500 + n, `IT-${String(n).padStart(2, '0')}`, `Item ${String(n).padStart(2, '0')}`))
    stock = []
    items.forEach((i) => stock.push(hold(i.id, STORE, null, null, '1')))
    const user = userEvent.setup()
    renderAt('/inventory/stock', PHARMACIST)
    await screen.findByRole('row', { name: /Item 00/ })
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByRole('row', { name: /Item 29/ })).toBeInTheDocument()
    expect(wire(stockRequests().at(-1)!)).toEqual({ page: 2, page_size: 25 })
  })
})

// ── Consume ─────────────────────────────────────────────────────────────────

describe('recording stock used', () => {
  const consumeRequests = () => sent('post', '/inventory/consume')
  const openOnRow = async (user: User, permissions = NURSE, path = '/inventory/stock') => {
    renderAt(path, permissions)
    await stockLoaded()
    if (path === EVERYTHING) await ledgerLoaded()
    await user.click(screen.getByRole('button', { name: `Consume ${ROW_2401}` }))
    return dialog()
  }
  const quantity = (d: HTMLElement) => within(d).getByLabelText(/^Quantity used/)
  const submit = (user: User, d: HTMLElement) => user.click(within(d).getByRole('button', { name: 'Record use' }))
  const ready = async (user: User, d: HTMLElement, amount: string) => {
    await user.type(quantity(d), amount)
    await user.click(within(d).getByRole('checkbox'))
  }
  const UNSURE_USE =
    "Couldn't confirm that the use was recorded. Check Movements before trying again, so it isn't recorded twice. The confirmation has been cleared."

  it('sends nothing until the removal is confirmed, then exactly what was confirmed', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    // Opened on a row: the item, the location and the batch are that row's.
    expect(within(d).getByRole('button', { name: 'Change item (IV cannula 20G)' })).toBeInTheDocument()
    expect(within(d).getByRole('combobox', { name: /^Taken from/ })).toHaveTextContent('General store')
    expect(within(d).getByLabelText(/^Batch/)).toHaveValue('CN-2401')
    const tick = within(d).getByRole('checkbox')
    expect(tick).toBeDisabled()

    await user.type(quantity(d), ' 2 ')
    expect(within(d).getByRole('checkbox', { name: 'Remove 2 piece of IV cannula 20G from General store, from batch CN-2401 only' })).toBeEnabled()
    await user.type(within(d).getByLabelText(/^Note/), '  Ward round ')
    await submit(user, d)

    expect(await within(d).findByText('Tick the box to confirm what will be removed')).toBeInTheDocument()
    expect(consumeRequests()).toHaveLength(0)
    expect(toastError).not.toHaveBeenCalled()

    await user.click(tick)
    const before = sent('get', '/inventory/stock').length
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Recorded IV cannula 20G used at General store: 2 piece from batch CN-2401. Across the hospital: 418 piece on hand, 418 piece usable',
      ),
    )
    const [request] = consumeRequests()
    expect(address(request)).toBe('/api/v1/inventory/consume')
    expect(bodyOf(request)).toEqual({
      item_id: CANNULA,
      location_id: STORE,
      quantity: '2',
      batch_number: 'CN-2401',
      note: 'Ward round',
    })
    expect(request.headers?.['Idempotency-Key']).toBeUndefined()
    expect(consumeRequests()).toHaveLength(1)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // The list is read again and shows the server's new count.
    expect(await screen.findByRole('row', { name: /CN-2401.*78 piece/ })).toBeInTheDocument()
    expect(sent('get', '/inventory/stock').length).toBeGreaterThan(before)
  })

  it('withdraws the confirmation when what will be removed changes', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '2')
    expect(within(d).getByRole('checkbox')).toBeChecked()

    await user.type(quantity(d), '0')

    expect(within(d).getByRole('checkbox', { name: /^Remove 20 piece/ })).not.toBeChecked()
    await submit(user, d)
    expect(await within(d).findByText('Tick the box to confirm what will be removed')).toBeInTheDocument()
    expect(consumeRequests()).toHaveLength(0)
  })

  it('takes earliest expiry first when no batch is named, and reports each batch the server drew on', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await user.clear(within(d).getByLabelText(/^Batch/))
    await user.type(quantity(d), '90.5')
    await user.click(within(d).getByRole('checkbox', { name: 'Remove 90.5 piece of IV cannula 20G from General store' }))
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Recorded IV cannula 20G used at General store: 80 piece from batch CN-2401, 10.5 piece from batch CN-2502. Across the hospital: 329.5 piece on hand, 329.5 piece usable',
      ),
    )
    // No batch on the wire, and the fraction as typed.
    expect(bodyOf(consumeRequests()[0])).toEqual({ item_id: CANNULA, location_id: STORE, quantity: '90.5' })
  })

  it('records a use from the header, for an untracked item, with the department', async () => {
    const user = userEvent.setup()
    renderAt('/inventory/stock', NURSE)
    await stockLoaded()
    await user.click(screen.getByRole('button', { name: 'Record stock used' }))
    const d = await dialog()

    await pickItem(user, d, 'nitr', /Nitrile gloves, medium/)
    // Untracked: there is no batch to name.
    expect(within(d).queryByLabelText(/^Batch/)).not.toBeInTheDocument()
    await choose(user, within(d).getByRole('combobox', { name: /^Taken from/ }), 'Ward A')
    await choose(user, within(d).getByRole('combobox', { name: /^Department/ }), 'Surgery')
    await user.type(quantity(d), '1')
    await user.click(within(d).getByRole('checkbox', { name: 'Remove 1 box of 100 of Nitrile gloves, medium from Ward A' }))
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Recorded Nitrile gloves, medium used at Ward A: 1 box of 100. Across the hospital: 63 box of 100 on hand, 63 box of 100 usable',
      ),
    )
    expect(bodyOf(consumeRequests()[0])).toEqual({ item_id: GLOVE, location_id: WARD, quantity: '1', department_id: SURGERY })
    expect(sent('get', '/departments')).toHaveLength(1)
  })

  it('tells the user when the item has just run low, from the server\'s flag', async () => {
    const user = userEvent.setup()
    renderAt('/inventory/stock', NURSE)
    await stockLoaded()
    await user.click(screen.getByRole('button', { name: 'Consume Nitrile gloves, medium at General store' }))
    const d = await dialog()
    await ready(user, d, '45')
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Recorded Nitrile gloves, medium used at General store: 45 box of 100. Across the hospital: 19 box of 100 on hand, 19 box of 100 usable — low stock',
      ),
    )
  })

  it('does not ask for departments for someone who may not list them', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, NURSE.filter((code) => code !== 'department.read'))

    expect(within(d).queryByRole('combobox', { name: /^Department/ })).not.toBeInTheDocument()
    await ready(user, d, '1')
    await submit(user, d)
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('get', '/departments')).toHaveLength(0)
  })

  it.each([
    ['', 'Enter how many units'],
    ['0', 'Must be more than zero'],
    ['0.00', 'Must be more than zero'],
    ['-1', 'A number with at most two decimal places, such as 2 or 2.5'],
    ['1.234', 'A number with at most two decimal places, such as 2 or 2.5'],
    ['two', 'A number with at most two decimal places, such as 2 or 2.5'],
    ['12345678901', 'That is more than the system can record'],
  ])('refuses the quantity "%s" before anything is sent', async (typed, message) => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    if (typed) await user.type(quantity(d), typed)
    await submit(user, d)

    expect(await within(d).findByText(message)).toBeInTheDocument()
    expect(consumeRequests()).toHaveLength(0)
  })

  it('needs an item and a location, and refuses a batch number that cannot be one', async () => {
    const user = userEvent.setup()
    renderAt('/inventory/stock', NURSE)
    await stockLoaded()
    await user.click(screen.getByRole('button', { name: 'Record stock used' }))
    const d = await dialog()
    await submit(user, d)

    expect(await within(d).findByText('Choose the item')).toBeInTheDocument()
    expect(within(d).getByText('Choose where it was taken from')).toBeInTheDocument()
    // Nothing to confirm yet, so the confirmation is not what blocks.
    expect(within(d).queryByText('Tick the box to confirm what will be removed')).not.toBeInTheDocument()

    await pickItem(user, d, 'iv', /IV cannula 20G/)
    await user.type(within(d).getByLabelText(/^Batch/), 'cn 24')
    await submit(user, d)
    expect(await within(d).findByText('Letters, digits and - _ . / only, starting with a letter or a digit')).toBeInTheDocument()
    expect(consumeRequests()).toHaveLength(0)
  })

  it('shows the server\'s refusal when the location is short, changes nothing and reads the stock again', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '10000000')
    const before = snapshot()
    const reads = sent('get', '/inventory/stock').length
    await submit(user, d)

    const message = 'Not enough IV cannula 20G at this location: 10000000 requested, 80 usable. Nothing was changed.'
    expect(await within(d).findByRole('alert')).toHaveTextContent(message)
    expect(toastError).toHaveBeenCalledWith(message)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(snapshot()).toBe(before)
    await waitFor(() => expect(sent('get', '/inventory/stock').length).toBeGreaterThan(reads))
    // The form is kept, to correct and retry.
    expect(quantity(d)).toHaveValue('10000000')
    expect(bodyOf(consumeRequests()[0])).toEqual({ item_id: CANNULA, location_id: STORE, quantity: '10000000', batch_number: 'CN-2401' })
  })

  it('says nothing was changed on a shortage even if the server\'s sentence does not', async () => {
    intercept = (c) =>
      c.url === '/inventory/consume'
        ? fail(409, 'Not enough stock.', { error_code: 'RESOURCE_CONFLICT', errors: { requested: '5.00', available: '1.00' } })
        : undefined
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '5')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('Not enough stock. Nothing was changed.')
  })

  it('puts a service 422 under the field it names', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await choose(user, within(d).getByRole('combobox', { name: /^Department/ }), 'Surgery')
    await ready(user, d, '1')
    // The department was removed after the form opened; the service names the field.
    departments = []
    await submit(user, d)

    expect(await within(d).findByText('Department not found in this hospital.')).toBeInTheDocument()
    expect(within(d).getByRole('combobox', { name: /^Department/ })).toHaveAttribute('aria-invalid', 'true')

    intercept = (c) => (c.url === '/inventory/consume' ? refused('item_id', 'Inventory item not found in this hospital.') : undefined)
    await submit(user, d)

    expect(await within(d).findByText('Inventory item not found in this hospital.')).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
  })

  it('puts a request-validation 422 under the field it names, without Pydantic\'s prefix', async () => {
    intercept = (c) => (c.url === '/inventory/consume' ? invalid('quantity', 'Value error, Input should be greater than 0') : undefined)
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '1')
    await submit(user, d)

    expect(await within(d).findByText('Input should be greater than 0')).toBeInTheDocument()
    expect(quantity(d)).toHaveAttribute('aria-invalid', 'true')
    expect(d.textContent).not.toMatch(/Value error/)
  })

  it('shows a business-rule refusal (400) in the server\'s words', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '1')
    itemOf(CANNULA)!.is_active = false
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent("Item 'CANN-20G' is inactive.")
    expect(toastError).toHaveBeenCalledWith("Item 'CANN-20G' is inactive.")
  })

  it('shows a 403 when the permission was withdrawn after sign-in', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '1')
    granted = granted.filter((code) => code !== 'inventory.consume')
    const before = snapshot()
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('Permission denied. Required: inventory.consume.')
    expect(snapshot()).toBe(before)
  })

  it('shows a 404 in the server\'s words, and never a 500\'s', async () => {
    intercept = (c) => (c.url === '/inventory/consume' ? fail(404, 'Not found.') : undefined)
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '1')
    await submit(user, d)
    expect(await within(d).findByRole('alert')).toHaveTextContent('Not found.')

    intercept = (c) => (c.url === '/inventory/consume' ? fail(500, 'DeadlockDetected: process 4411') : undefined)
    await submit(user, d)

    await waitFor(() => expect(within(d).getByRole('alert')).toHaveTextContent(UNSURE_USE))
    expect(document.body.textContent).not.toMatch(/Deadlock|4411/)
    expect(toastError).toHaveBeenLastCalledWith(UNSURE_USE)
  })

  it('does not invite a second removal when a use went through but its answer was lost', async () => {
    // The server takes the units, then the answer is lost on the way back.
    intercept = (c) => {
      if (c.url !== '/inventory/consume') return undefined
      server(c)
      return fail(502, 'Bad gateway')
    }
    const user = userEvent.setup()
    const d = await openOnRow(user, ADMIN, EVERYTHING)
    await ready(user, d, '2')
    const urls = ['/inventory/stock', '/inventory/stock/summary', '/inventory/movements']
    const before = urls.map((url) => sent('get', url).length)
    await submit(user, d)

    await waitFor(() => expect(within(d).getByRole('alert')).toHaveTextContent(UNSURE_USE))
    // Not "try again": the write is not idempotent and may already stand.
    expect(d.textContent).not.toMatch(/try again\.|Please try/i)
    expect(UNSURE_USE).toMatch(/Check Movements before trying again, so it isn't recorded twice/)
    expect(toastSuccess).not.toHaveBeenCalled()
    // What is behind the dialog is read again, so the ledger the user is sent to shows the truth.
    await waitFor(() => urls.forEach((url, n) => expect(sent('get', url).length).toBeGreaterThan(before[n])))
    // (Behind the open dialog, so hidden from the accessibility tree for now.)
    expect(await screen.findByRole('row', { name: /CN-2401.*78 piece/, hidden: true })).toBeInTheDocument()
    expect(
      await screen.findByRole('row', { name: new RegExp(`Used.*${MINUS}2 piece.*CN-2401`), hidden: true }),
    ).toBeInTheDocument()

    // The tick was for the request that was sent. Another click sends nothing.
    await waitFor(() => expect(within(d).getByRole('checkbox')).not.toBeChecked())
    intercept = () => undefined
    await submit(user, d)
    expect(await within(d).findByText('Tick the box to confirm what will be removed')).toBeInTheDocument()
    expect(consumeRequests()).toHaveLength(1)
    expect(stock.find((row) => row.batch_number === 'CN-2401')?.cents).toBe(7800)
  })

  it('treats a dropped connection like any other unknown outcome, and keeps the tick after a plain refusal', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '10000000')
    await submit(user, d)
    // A 409 says nothing was changed: the same request, corrected, needs no new tick for the same sentence.
    expect(await within(d).findByRole('alert')).toHaveTextContent(/Nothing was changed\./)
    expect(within(d).getByRole('checkbox')).toBeChecked()

    intercept = (c) => {
      if (c.url === '/inventory/consume') throw new Error('socket hang up')
      return undefined
    }
    const reads = sent('get', '/inventory/stock').length
    await submit(user, d)

    await waitFor(() => expect(within(d).getByRole('alert')).toHaveTextContent(UNSURE_USE))
    expect(document.body.textContent).not.toMatch(/socket hang up/)
    await waitFor(() => expect(within(d).getByRole('checkbox')).not.toBeChecked())
    await waitFor(() => expect(sent('get', '/inventory/stock').length).toBeGreaterThan(reads))
  })

  it('records one use however many times the form is submitted', async () => {
    const release = holdBack((c) => c.url === '/inventory/consume')
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await ready(user, d, '5')
    const form = within(d).getByRole('button', { name: 'Record use' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Recording…' })).toBeDisabled()
    await waitFor(() => expect(consumeRequests()).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(consumeRequests()).toHaveLength(1)
    // A second request would have taken the units again.
    expect(stock.find((row) => row.batch_number === 'CN-2401')?.cents).toBe(7500)
  })

  it('reads stock, the summary and the ledger again after a use', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, ADMIN, EVERYTHING)
    const before = ['/inventory/stock', '/inventory/stock/summary', '/inventory/movements'].map((url) => sent('get', url).length)
    await ready(user, d, '3')
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      ['/inventory/stock', '/inventory/stock/summary', '/inventory/movements'].forEach((url, n) =>
        expect(sent('get', url).length).toBeGreaterThan(before[n]),
      ),
    )
    // All three screens now show what the server holds.
    expect(await screen.findByRole('row', { name: /CN-2401.*77 piece/ })).toBeInTheDocument()
    expect(await screen.findByRole('row', { name: /IV cannula 20G.*417 piece.*417 piece/ })).toBeInTheDocument()
    expect(await screen.findByRole('row', { name: new RegExp(`Used.*${MINUS}3 piece.*CN-2401`) })).toBeInTheDocument()
  })
})

// ── Transfer ────────────────────────────────────────────────────────────────

describe('transferring stock', () => {
  const transferRequests = () => sent('post', '/inventory/transfer')
  const openOnRow = async (user: User, path = '/inventory/stock') => {
    renderAt(path, INVENTORY_MANAGER)
    await stockLoaded()
    if (path === EVERYTHING) await ledgerLoaded()
    await user.click(screen.getByRole('button', { name: `Transfer ${ROW_2502}` }))
    return dialog()
  }
  const quantity = (d: HTMLElement) => within(d).getByLabelText(/^Quantity to move/)
  const from = (d: HTMLElement) => within(d).getByRole('combobox', { name: /^From/ })
  const to = (d: HTMLElement) => within(d).getByRole('combobox', { name: /^To/ })
  const submit = (user: User, d: HTMLElement) => user.click(within(d).getByRole('button', { name: 'Transfer' }))

  it('moves a batch and reports the ledger entries the server wrote for each place', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    expect(from(d)).toHaveTextContent('General store')
    expect(within(d).getByLabelText(/^Batch/)).toHaveValue('CN-2502')

    await choose(user, to(d), 'Ward A')
    await user.type(quantity(d), '40')
    await user.type(within(d).getByLabelText(/^Note/), 'Restock')
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        `Moved IV cannula 20G — batch CN-2502: General store ${MINUS}40 piece, Ward A +40 piece`,
      ),
    )
    const [request] = transferRequests()
    expect(address(request)).toBe('/api/v1/inventory/transfer')
    expect(bodyOf(request)).toEqual({
      item_id: CANNULA,
      from_location_id: STORE,
      to_location_id: WARD,
      quantity: '40',
      batch_number: 'CN-2502',
      note: 'Restock',
    })
    expect(transferRequests()).toHaveLength(1)
    // Both balances come from the list read again, not from arithmetic here.
    expect(await screen.findByRole('row', { name: /General store.*CN-2502.*260 piece/ })).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /Ward A.*CN-2502.*40 piece/ })).toBeInTheDocument()
  })

  it('reports every batch moved when none is named', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await user.clear(within(d).getByLabelText(/^Batch/))
    await choose(user, to(d), 'Ward A')
    await user.type(quantity(d), '100')
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        `Moved IV cannula 20G — batch CN-2401: General store ${MINUS}80 piece, Ward A +80 piece; batch CN-2502: General store ${MINUS}20 piece, Ward A +20 piece`,
      ),
    )
    expect(bodyOf(transferRequests()[0])).toEqual({ item_id: CANNULA, from_location_id: STORE, to_location_id: WARD, quantity: '100' })
  })

  it('refuses a transfer to the place it starts from, before anything is sent', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await choose(user, to(d), 'General store')
    await user.type(quantity(d), '1')
    await submit(user, d)

    expect(await within(d).findByText('Choose a different location — stock cannot be moved to where it already is')).toBeInTheDocument()
    expect(to(d)).toHaveAttribute('aria-invalid', 'true')
    expect(transferRequests()).toHaveLength(0)

    // Corrected, it goes.
    await choose(user, to(d), 'Intensive care unit')
    await submit(user, d)
    await waitFor(() => expect(transferRequests()).toHaveLength(1))
  })

  it('lists an inactive location as a source but not as a destination', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)

    await user.click(to(d))
    expect((await screen.findAllByRole('option')).map((o) => o.textContent)).toEqual(['General store', 'Intensive care unit', 'Ward A'])
    await user.click(screen.getByRole('option', { name: 'Ward A' }))

    await user.click(from(d))
    expect((await screen.findAllByRole('option')).map((o) => o.textContent)).toEqual([
      'General store',
      'Intensive care unit',
      'Old annex (inactive)',
      'Ward A',
    ])
  })

  it('shows the server\'s refusal of a destination closed since the list was read (400)', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await choose(user, to(d), 'Ward A')
    await user.type(quantity(d), '1')
    locationOf(WARD)!.is_active = false
    const before = snapshot()
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent("Location 'WARD-A' is inactive and cannot receive stock.")
    expect(snapshot()).toBe(before)
  })

  it('shows the API\'s own same-location refusal above the form, without Pydantic\'s prefix', async () => {
    intercept = (c) => (c.url === '/inventory/transfer' ? invalid('', 'Value error, A transfer needs two different locations.') : undefined)
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await choose(user, to(d), 'Ward A')
    await user.type(quantity(d), '1')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('A transfer needs two different locations.')
    expect(d.textContent).not.toMatch(/Value error/)
  })

  it('shows a shortage, a vanished location, a 403 and a 500 each as it should', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await choose(user, to(d), 'Ward A')
    await user.type(quantity(d), '301')
    const before = snapshot()
    await submit(user, d)
    expect(await within(d).findByRole('alert')).toHaveTextContent(
      'Not enough IV cannula 20G at this location: 301 requested, 300 usable. Nothing was changed.',
    )
    expect(snapshot()).toBe(before)

    intercept = (c) =>
      c.url === '/inventory/transfer' ? refused('to_location_id', 'Inventory location not found in this hospital.') : undefined
    await submit(user, d)
    expect(await within(d).findByText('Inventory location not found in this hospital.')).toBeInTheDocument()
    expect(to(d)).toHaveAttribute('aria-invalid', 'true')

    intercept = () => undefined
    granted = granted.filter((code) => code !== 'inventory.transfer')
    await submit(user, d)
    await waitFor(() => expect(within(d).getAllByRole('alert')[0]).toHaveTextContent('Permission denied. Required: inventory.transfer.'))

    intercept = (c) => (c.url === '/inventory/transfer' ? fail(500, 'deadlock detected') : undefined)
    const reads = sent('get', '/inventory/stock').length
    await submit(user, d)
    // The outcome is unknown and a transfer repeats if sent again: check first, and read the stock again.
    const unsure =
      "Couldn't confirm that the stock was transferred. Check Movements before trying again, so it isn't moved twice."
    await waitFor(() => expect(within(d).getAllByRole('alert')[0]).toHaveTextContent(unsure))
    expect(toastError).toHaveBeenLastCalledWith(unsure)
    expect(d.textContent).not.toMatch(/Please try/i)
    expect(document.body.textContent).not.toMatch(/deadlock/)
    expect(toastSuccess).not.toHaveBeenCalled()
    await waitFor(() => expect(sent('get', '/inventory/stock').length).toBeGreaterThan(reads))
  })

  it('transfers once however many times the form is submitted', async () => {
    const release = holdBack((c) => c.url === '/inventory/transfer')
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await choose(user, to(d), 'Ward A')
    await user.type(quantity(d), '10')
    const form = within(d).getByRole('button', { name: 'Transfer' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Transferring…' })).toBeDisabled()
    await waitFor(() => expect(transferRequests()).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(transferRequests()).toHaveLength(1)
    expect(stock.find((row) => row.location_id === WARD && row.batch_number === 'CN-2502')?.cents).toBe(1000)
  })

  it('reads stock, the summary and the ledger again after a transfer', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, EVERYTHING)
    const urls = ['/inventory/stock', '/inventory/stock/summary', '/inventory/movements']
    const before = urls.map((url) => sent('get', url).length)
    await choose(user, to(d), 'Ward A')
    await user.type(quantity(d), '5')
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    await waitFor(() => urls.forEach((url, n) => expect(sent('get', url).length).toBeGreaterThan(before[n])))
    expect(await screen.findByRole('row', { name: new RegExp(`Ward A.*Transferred in.*\\+5 piece`) })).toBeInTheDocument()
    expect(screen.getByRole('row', { name: new RegExp(`General store.*Transferred out.*${MINUS}5 piece`) })).toBeInTheDocument()
  })
})

// ── Adjust ──────────────────────────────────────────────────────────────────

describe('adjusting stock', () => {
  const adjustRequests = () => sent('post', '/inventory/adjust')
  const EXPIRED_ROW = 'Sterile gauze swab at General store, batch GZ-2310'
  const openOnRow = async (user: User, row = EXPIRED_ROW, path = '/inventory/stock') => {
    renderAt(path, INVENTORY_MANAGER)
    await stockLoaded()
    if (path === EVERYTHING) await ledgerLoaded()
    await user.click(screen.getByRole('button', { name: `Adjust ${row}` }))
    return dialog()
  }
  const openBlank = async (user: User) => {
    renderAt('/inventory/stock', INVENTORY_MANAGER)
    await stockLoaded()
    await user.click(screen.getByRole('button', { name: 'Adjust stock' }))
    return dialog()
  }
  const amount = (d: HTMLElement) => within(d).getByLabelText(/^Units to (remove|add)/)
  const note = (d: HTMLElement) => within(d).getByLabelText(/^Note/)
  const change = (d: HTMLElement) => within(d).getByRole('combobox', { name: /^Change/ })
  const reason = (d: HTMLElement) => within(d).getByRole('combobox', { name: /^Reason/ })
  const submit = (user: User, d: HTMLElement) => user.click(within(d).getByRole('button', { name: 'Save adjustment' }))

  it('writes off an expired batch as a negative change, and reports what the server recorded', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    // What the row held when it was read, and the server's expiry flag.
    const held = within(d).getByText('Held when last read').parentElement as HTMLElement
    expect(held).toHaveTextContent('25 pack of 10')
    expect(within(d).getByText('Expired')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Batch/)).toHaveValue('GZ-2310')
    expect(change(d)).toHaveTextContent('Remove units')
    // Opened on an expired batch, the write-off is the reason offered first.
    expect(reason(d)).toHaveTextContent('Expired write-off')
    // Removing never starts a batch, so no expiry date is asked for.
    expect(within(d).queryByLabelText(/^Expiry date/)).not.toBeInTheDocument()

    await user.type(amount(d), '25')
    await user.type(note(d), '  Past expiry ')
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        `Expired write-off recorded for Sterile gauze swab at General store, batch GZ-2310: ${MINUS}25 pack of 10. Across the hospital: 150 pack of 10 on hand, 150 pack of 10 usable`,
      ),
    )
    const [request] = adjustRequests()
    expect(address(request)).toBe('/api/v1/inventory/adjust')
    expect(bodyOf(request)).toEqual({
      item_id: GAUZE,
      location_id: STORE,
      quantity_change: '-25',
      reason: 'expired',
      note: 'Past expiry',
      batch_number: 'GZ-2310',
    })
    expect(adjustRequests()).toHaveLength(1)
    // Emptied, the row leaves the in-stock list the server sends back.
    await waitFor(() => expect(screen.queryByRole('row', { name: /GZ-2310/ })).not.toBeInTheDocument())
  })

  it('adds a fraction to an untracked item as a positive change, with no batch or expiry asked for', async () => {
    const user = userEvent.setup()
    const d = await openBlank(user)
    await pickItem(user, d, 'oxy', /Oxygen cylinder/)
    await choose(user, within(d).getByRole('combobox', { name: /^Location/ }), 'General store')
    await choose(user, change(d), 'Add units')
    expect(within(d).queryByLabelText(/^Batch/)).not.toBeInTheDocument()
    expect(within(d).queryByLabelText(/^Expiry date/)).not.toBeInTheDocument()

    await user.type(amount(d), '1.5')
    await user.type(note(d), 'Found in the cage')
    await submit(user, d)

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Count correction recorded for Oxygen cylinder, B type at General store: +1.5 cylinder. Across the hospital: 4 cylinder on hand, 4 cylinder usable',
      ),
    )
    expect(bodyOf(adjustRequests()[0])).toEqual({
      item_id: OXYGEN,
      location_id: STORE,
      quantity_change: '1.5',
      reason: 'adjusted',
      note: 'Found in the cage',
    })
    expect(await screen.findByRole('row', { name: /Oxygen cylinder.*4 cylinder/ })).toBeInTheDocument()
  })

  it('starts a new batch of a tracked item with its number in capitals and its expiry date', async () => {
    const user = userEvent.setup()
    const d = await openBlank(user)
    await pickItem(user, d, 'iv', /IV cannula 20G/)
    await choose(user, within(d).getByRole('combobox', { name: /^Location/ }), 'Intensive care unit')
    await choose(user, change(d), 'Add units')
    await user.type(within(d).getByLabelText(/^Batch/), ' cn-2601 ')
    fireEvent.change(within(d).getByLabelText(/^Expiry date/), { target: { value: daysFromToday(500) } })
    await user.type(amount(d), '10')
    await user.type(note(d), 'Opening balance')
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(adjustRequests()[0])).toEqual({
      item_id: CANNULA,
      location_id: ICU,
      quantity_change: '10',
      reason: 'adjusted',
      note: 'Opening balance',
      batch_number: 'CN-2601',
      expiry_date: daysFromToday(500),
    })
    expect(stock.find((row) => row.batch_number === 'CN-2601')).toMatchObject({ cents: 1000, expiry_date: daysFromToday(500) })
  })

  it('needs a note, an amount and — for a tracked item — a batch, before anything is sent', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    await user.clear(within(d).getByLabelText(/^Batch/))
    await user.type(note(d), '   ')
    await submit(user, d)

    expect(await within(d).findByText('Say why the count is changing')).toBeInTheDocument()
    expect(within(d).getByText('Enter how many units')).toBeInTheDocument()
    expect(within(d).getByText('This item is batch-tracked — enter the batch number')).toBeInTheDocument()
    expect(adjustRequests()).toHaveLength(0)

    // Never zero.
    await user.type(amount(d), '0')
    await submit(user, d)
    expect(await within(d).findByText('Must be more than zero')).toBeInTheDocument()
    expect(adjustRequests()).toHaveLength(0)
  })

  it('offers "expired" only for a removal, and takes it back when the change becomes an addition', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user)
    expect(reason(d)).toHaveTextContent('Expired write-off')

    await choose(user, change(d), 'Add units')

    expect(reason(d)).toHaveTextContent('Count correction')
    await user.click(reason(d))
    expect(await screen.findByRole('option', { name: 'Expired write-off' })).toHaveAttribute('aria-disabled', 'true')
    // Only the two reasons the endpoint accepts are offered at all.
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(['Count correction', 'Expired write-off'])
  })

  it('shows the server\'s refusal to take a row below zero (400) and reads the stock again', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, 'Nitrile gloves, medium at Ward A')
    await user.type(amount(d), '500')
    await user.type(note(d), 'Recount')
    const before = snapshot()
    const reads = sent('get', '/inventory/stock').length
    await submit(user, d)

    const message = 'Ward A holds 4.00 of Nitrile gloves, medium; 500.00 cannot be removed.'
    expect(await within(d).findByRole('alert')).toHaveTextContent(message)
    expect(toastError).toHaveBeenCalledWith(message)
    expect(snapshot()).toBe(before)
    await waitFor(() => expect(sent('get', '/inventory/stock').length).toBeGreaterThan(reads))
  })

  it('puts a vanished item under the item field, and a 403 and a 500 above the form', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, 'Nitrile gloves, medium at Ward A')
    await user.type(amount(d), '1')
    await user.type(note(d), 'Recount')
    intercept = (c) =>
      c.url === '/inventory/adjust' ? refused('item_id', 'Inventory item not found in this hospital.') : undefined
    await submit(user, d)
    expect(await within(d).findByText('Inventory item not found in this hospital.')).toBeInTheDocument()

    intercept = () => undefined
    granted = granted.filter((code) => code !== 'inventory.adjust')
    await submit(user, d)
    await waitFor(() => expect(within(d).getAllByRole('alert')[0]).toHaveTextContent('Permission denied. Required: inventory.adjust.'))

    intercept = (c) => (c.url === '/inventory/adjust' ? fail(500, 'CheckViolation: ck_inventory_stock_quantity') : undefined)
    const reads = sent('get', '/inventory/stock').length
    await submit(user, d)
    // The outcome is unknown and an adjustment repeats if sent again: check first, and read the stock again.
    const unsure =
      "Couldn't confirm that the adjustment was saved. Check Movements before trying again, so it isn't applied twice."
    await waitFor(() => expect(within(d).getAllByRole('alert')[0]).toHaveTextContent(unsure))
    expect(toastError).toHaveBeenLastCalledWith(unsure)
    expect(d.textContent).not.toMatch(/Please try/i)
    expect(document.body.textContent).not.toMatch(/CheckViolation|ck_inventory/)
    expect(toastSuccess).not.toHaveBeenCalled()
    await waitFor(() => expect(sent('get', '/inventory/stock').length).toBeGreaterThan(reads))
  })

  it('adjusts a withdrawn item from its stock row, and says so where the search cannot reach one', async () => {
    // The API corrects an inactive item's stock (`allow_inactive=True`); the item search lists active items only.
    stock.push(hold(uid(6), STORE, null, null, '3'))
    const user = userEvent.setup()
    const d = await openOnRow(user, 'Latex gloves (withdrawn) at General store')
    expect(within(d).getByRole('button', { name: 'Change item (Latex gloves (withdrawn))' })).toBeInTheDocument()
    // The item is chosen: nothing to explain.
    expect(within(d).queryByText(/To adjust a withdrawn item/)).not.toBeInTheDocument()
    await user.type(amount(d), '3')
    await user.type(note(d), 'Withdrawn from use')
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(bodyOf(adjustRequests()[0])).toEqual({
      item_id: uid(6),
      location_id: STORE,
      quantity_change: '-3',
      reason: 'adjusted',
      note: 'Withdrawn from use',
    })

    // Opened blank, the picker cannot find it — the dialog says how to get to it.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Adjust stock' }))
    const blank = await dialog()
    expect(
      within(blank).getByText(
        'The search lists active items. To adjust a withdrawn item, use Adjust on its stock row — tick Include empty rows if it holds nothing',
      ),
    ).toBeInTheDocument()
    await user.type(within(blank).getByLabelText(/^Item/), 'latex')
    expect(await within(blank).findByText(/No active item matches “latex”/)).toBeInTheDocument()
  })

  it('ties the hint and a server error to the Change and Reason choosers, for a screen reader', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, 'Nitrile gloves, medium at Ward A')
    expect(reason(d)).toHaveAccessibleDescription('Recorded in the ledger with the adjustment')

    await choose(user, change(d), 'Add units')
    expect(reason(d)).toHaveAccessibleDescription('Expired stock can only be removed, so it is not offered when adding')

    await user.type(amount(d), '1')
    await user.type(note(d), 'Recount')
    intercept = (c) => (c.url === '/inventory/adjust' ? invalid('reason', "Input should be 'adjusted' or 'expired'") : undefined)
    await submit(user, d)
    await waitFor(() => expect(reason(d)).toHaveAccessibleDescription("Input should be 'adjusted' or 'expired'"))
    expect(reason(d)).toHaveAttribute('aria-invalid', 'true')

    intercept = (c) => (c.url === '/inventory/adjust' ? refused('direction', 'Choose whether units are added or removed.') : undefined)
    await submit(user, d)
    await waitFor(() => expect(change(d)).toHaveAccessibleDescription('Choose whether units are added or removed.'))
  })

  it('shows the API\'s refusal of a batch number on an untracked item under the batch field', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, ROW_2401)
    await user.type(amount(d), '1')
    await user.type(note(d), 'Recount')
    // Tracking is fixed when an item is created; this stands in for a form that is out of date.
    itemOf(CANNULA)!.is_batch_tracked = false
    await submit(user, d)

    expect(await within(d).findByText('IV cannula 20G is not batch-tracked.')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Batch/)).toHaveAttribute('aria-invalid', 'true')
  })

  it('adjusts once however many times the form is submitted', async () => {
    const release = holdBack((c) => c.url === '/inventory/adjust')
    const user = userEvent.setup()
    const d = await openOnRow(user, ROW_2401)
    await user.type(amount(d), '4')
    await user.type(note(d), 'Damaged')
    const form = within(d).getByRole('button', { name: 'Save adjustment' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Saving…' })).toBeDisabled()
    await waitFor(() => expect(adjustRequests()).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(adjustRequests()).toHaveLength(1)
    expect(stock.find((row) => row.batch_number === 'CN-2401')?.cents).toBe(7600)
  })

  it('reads stock, the summary and the ledger again after an adjustment', async () => {
    const user = userEvent.setup()
    const d = await openOnRow(user, ROW_2401, EVERYTHING)
    const urls = ['/inventory/stock', '/inventory/stock/summary', '/inventory/movements']
    const before = urls.map((url) => sent('get', url).length)
    await user.type(amount(d), '4')
    await user.type(note(d), 'Damaged')
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    await waitFor(() => urls.forEach((url, n) => expect(sent('get', url).length).toBeGreaterThan(before[n])))
    expect(await screen.findByRole('row', { name: new RegExp(`Adjusted.*${MINUS}4 piece.*Damaged`) })).toBeInTheDocument()
  })
})

// ── The ledger ──────────────────────────────────────────────────────────────

describe('stock ledger', () => {
  const ledgerRequests = () => sent('get', '/inventory/movements')
  const loaded = () => screen.findByRole('row', { name: /Morning round/ })

  it('lists entries newest first, naming the item and place each one carries by id', async () => {
    renderAt('/inventory/movements', ADMIN)

    const used = await loaded()
    await waitFor(() => expect(cellsOf(used)[1]).toBe('Nitrile gloves, mediumGLOVE-M'))
    expect(cellsOf(used).slice(2)).toEqual(['Ward A', 'Used', `${MINUS}2 box of 100`, '—', 'Use recorded', 'Surgery', 'Morning round'])
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows.map((row) => cellsOf(row)[3])).toEqual(['Adjusted', 'Used', 'Received'])
    expect(cellsOf(rows[0]).slice(1)).toEqual([
      'Oxygen cylinder, B typeO2-B',
      'General store',
      'Adjusted',
      `${MINUS}0.5 cylinder`,
      '—',
      'Adjustment',
      '—',
      'Leaking valve',
    ])
    expect(cellsOf(rows[2]).slice(1, 7)).toEqual(['IV cannula 20GCANN-20G', 'General store', 'Received', '+300 piece', 'CN-2502', 'Purchase order'])
    // The order that brought it in, for someone who may open one.
    expect(within(rows[2]).getByRole('link', { name: 'Purchase order' })).toHaveAttribute('href', `/inventory/purchase-orders/${ORDER}`)
    expect(screen.getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'When',
      'Item',
      'Location',
      'Reason',
      'Change',
      'Batch',
      'Source',
      'Department',
      'Note',
    ])
    // A record: nothing to act on.
    expect(screen.queryByRole('button', { name: /Consume|Transfer|Adjust|Edit|Delete/ })).not.toBeInTheDocument()

    const [request] = ledgerRequests()
    expect(address(request)).toBe('/api/v1/inventory/movements')
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    // Names come from one page of the catalog, not a request per entry.
    expect(wire(sent('get', '/inventory/items').find((c) => wire(c).page_size === 100)!)).toEqual({ page: 1, page_size: 100 })
    expect(fake.sent.filter((c) => c.url?.startsWith('/inventory/items/'))).toHaveLength(0)
  })

  it('leaves out the order link and the department for a user who may read neither', async () => {
    renderAt('/inventory/movements', NURSE.filter((code) => code !== 'department.read'))
    await loaded()

    expect(screen.queryByRole('link', { name: 'Purchase order' })).not.toBeInTheDocument()
    expect(screen.getByText('Purchase order')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Department' })).not.toBeInTheDocument()
    expect(sent('get', '/departments')).toHaveLength(0)
  })

  it('shows a name still being read as loading, and "not available" only once it is known to be', async () => {
    const release = holdBack((c) => c.url === '/inventory/items')
    renderAt('/inventory/movements', ADMIN)

    const used = await loaded()
    // The catalog has not answered: that is a wait, not a fault in the data.
    expect(cellsOf(used)[1]).toBe('…')
    expect(screen.queryByText('Item not available')).not.toBeInTheDocument()

    release()
    await waitFor(() => expect(cellsOf(used)[1]).toBe('Nitrile gloves, mediumGLOVE-M'))
    expect(screen.queryByText('Item not available')).not.toBeInTheDocument()
  })

  it('waits for an item read by itself too, and says so if it cannot be read', async () => {
    const extra = Array.from({ length: 100 }, (_, n) => item(600 + n, `AA-${String(n).padStart(3, '0')}`, `Aardvark ${String(n).padStart(3, '0')}`))
    items = [...extra, ...items]
    let answer: (outcome: Outcome) => void = () => {}
    intercept = (c) =>
      c.url === `/inventory/items/${GLOVE}` ? new Promise<Outcome>((resolve) => (answer = resolve)) : undefined
    renderAt('/inventory/movements', ADMIN)

    const used = await loaded()
    await waitFor(() => expect(sent('get', `/inventory/items/${GLOVE}`)).toHaveLength(1))
    expect(cellsOf(used)[1]).toBe('…')
    expect(within(used).queryByText('Item not available')).not.toBeInTheDocument()

    answer(fail(404, 'Inventory item not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: null }))
    await waitFor(() => expect(cellsOf(used)[1]).toBe('Item not available'))
  })

  it('does not wait for names it may not ask for', async () => {
    renderAt('/inventory/movements', ['inventory.stock.read'])

    const used = await loaded()
    expect(cellsOf(used)[1]).toBe('Item not available')
    expect(fake.sent.filter((c) => c.url?.startsWith('/inventory/items'))).toHaveLength(0)
  })

  it('reads an item by itself only when the catalog page does not hold it', async () => {
    const extra = Array.from({ length: 100 }, (_, n) => item(600 + n, `AA-${String(n).padStart(3, '0')}`, `Aardvark ${String(n).padStart(3, '0')}`))
    items = [...extra, ...items]
    renderAt('/inventory/movements', ADMIN)

    const used = await loaded()
    await waitFor(() => expect(cellsOf(used)[1]).toBe('Nitrile gloves, mediumGLOVE-M'))
    // Once per item, not once per entry or per cell.
    expect(sent('get', `/inventory/items/${GLOVE}`)).toHaveLength(1)
  })

  it('filters by reason, location and item with the API\'s parameters, and omits "all"', async () => {
    const user = userEvent.setup()
    renderAt('/inventory/movements', ADMIN)
    await loaded()

    await choose(user, screen.getByRole('combobox', { name: 'Reason' }), 'Received')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Morning round/ })).not.toBeInTheDocument())
    expect(screen.getAllByRole('row')).toHaveLength(2)
    expect(wire(ledgerRequests().at(-1)!)).toEqual({ reason: 'received', page: 1, page_size: 25 })

    await choose(user, screen.getByRole('combobox', { name: 'Location' }), 'Ward A')
    expect(await screen.findByText('No matching movements')).toBeInTheDocument()
    expect(wire(ledgerRequests().at(-1)!)).toEqual({ location_id: WARD, reason: 'received', page: 1, page_size: 25 })

    await choose(user, screen.getByRole('combobox', { name: 'Reason' }), 'All reasons')
    expect(await loaded()).toBeInTheDocument()
    expect(wire(ledgerRequests().at(-1)!)).toEqual({ location_id: WARD, page: 1, page_size: 25 })

    await choose(user, screen.getByRole('combobox', { name: 'Location' }), 'All locations')
    await user.type(screen.getByLabelText('Item'), 'oxy')
    await user.click(await screen.findByRole('button', { name: /Oxygen cylinder/ }))
    await waitFor(() => expect(screen.queryByRole('row', { name: /Morning round/ })).not.toBeInTheDocument())
    expect(wire(ledgerRequests().at(-1)!)).toEqual({ item_id: OXYGEN, page: 1, page_size: 25 })

    await user.click(screen.getByRole('button', { name: 'Clear the item filter' }))
    expect(await loaded()).toBeInTheDocument()
  })

  it('offers the six reasons the ledger records, in plain words', async () => {
    const user = userEvent.setup()
    renderAt('/inventory/movements', ADMIN)
    await loaded()

    await user.click(screen.getByRole('combobox', { name: 'Reason' }))

    expect((await screen.findAllByRole('option')).map((o) => o.textContent)).toEqual([
      'All reasons',
      'Received',
      'Used',
      'Transferred in',
      'Transferred out',
      'Adjusted',
      'Expired write-off',
    ])
  })

  it('pages through the server\'s pages, and starts a new filter at the first', async () => {
    for (let n = 0; n < 30; n += 1) entry(stock[0], -100, 'consumed', { reference_type: 'consumption', note: `Round ${n}` })
    const user = userEvent.setup()
    renderAt('/inventory/movements', ADMIN)
    await screen.findByRole('row', { name: /Round 29/ })
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await loaded()).toBeInTheDocument()
    expect(wire(ledgerRequests().at(-1)!)).toEqual({ page: 2, page_size: 25 })

    await choose(user, screen.getByRole('combobox', { name: 'Reason' }), 'Used')
    await waitFor(() => expect(wire(ledgerRequests().at(-1)!)).toEqual({ reason: 'consumed', page: 1, page_size: 25 }))
  })

  it('says the ledger is empty when nothing has moved', async () => {
    ledger = []
    renderAt('/inventory/movements', ADMIN)

    expect(await screen.findByText('No movements yet')).toBeInTheDocument()
    expect(screen.queryByText('No matching movements')).not.toBeInTheDocument()
  })

  it('shows loading placeholders, then a retry when the ledger cannot be loaded', async () => {
    let failing = true
    let release: () => void = () => {}
    intercept = (c) =>
      c.url === '/inventory/movements' && failing
        ? new Promise<Outcome>((resolve) => (release = () => resolve(fail(500, 'KeyError: moved_at'))))
        : undefined
    renderAt('/inventory/movements', ADMIN)

    await waitFor(() => expect(skeletons()).toBeGreaterThan(0))
    release()
    expect(await screen.findByText("Couldn't load the stock ledger")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/KeyError|moved_at/)

    failing = false
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }))
    expect(await loaded()).toBeInTheDocument()
  })
})

// ── The forms' own rules ────────────────────────────────────────────────────

describe('stock form rules', () => {
  const row: StockRow = {
    id: uid(1),
    item_id: CANNULA,
    item_sku: 'CANN-20G',
    item_name: 'IV cannula 20G',
    unit_of_measure: 'piece',
    location_id: STORE,
    location_code: 'STORE',
    location_name: 'General store',
    batch_number: 'CN-2401',
    expiry_date: '2024-03-06',
    quantity: '80.00',
    is_expired: false,
  }

  it('words what a consume will remove from the request itself', () => {
    const values = { ...consumeDefaults(row), quantity: ' 2.50 ' }
    expect(consumeStatement(values)).toBe('Remove 2.5 piece of IV cannula 20G from General store, from batch CN-2401 only')
    expect(consumeStatement({ ...values, batch_number: '' })).toBe('Remove 2.5 piece of IV cannula 20G from General store')
    expect(consumeStatement({ ...values, quantity: '0' })).toBeNull()
    expect(consumeStatement({ ...values, location_id: '' })).toBeNull()
    expect(consumeStatement(consumeDefaults())).toBeNull()
  })

  it('accepts a consume only with the sentence that matches it', () => {
    const values = { ...consumeDefaults(row), quantity: '2' }
    expect(consumeSchema.safeParse(values).success).toBe(false)
    expect(consumeSchema.safeParse({ ...values, confirmed: 'Remove 3 piece of IV cannula 20G from General store, from batch CN-2401 only' }).success).toBe(false)
    expect(consumeSchema.safeParse({ ...values, confirmed: consumeStatement(values) }).success).toBe(true)
  })

  it('never sends a batch for an untracked item, nor a blank optional field', () => {
    const values = { ...consumeDefaults(row), batch_tracked: false, quantity: '2', note: '   ', batch_number: 'left-over' }
    expect(toConsumeBody(values)).toEqual({ item_id: CANNULA, location_id: STORE, quantity: '2' })
    // A field that is not on screen does not block the form.
    expect(consumeSchema.safeParse({ ...values, batch_number: 'not a batch!', confirmed: consumeStatement(values) }).success).toBe(true)
    expect(toTransferBody({ ...transferDefaults(row), batch_tracked: false, to_location_id: WARD, quantity: '1' })).toEqual({
      item_id: CANNULA,
      from_location_id: STORE,
      to_location_id: WARD,
      quantity: '1',
    })
  })

  it('knows which failures leave a write\'s outcome unknown', () => {
    // Each of these is the API saying it refused: nothing was changed.
    for (const status of [400, 403, 404, 409, 422]) expect(outcomeUnknown(new ApiError('Refused', 'x', status))).toBe(false)
    // A 5xx, a gateway, no answer at all, or something that is not the API's error: it may have been applied.
    for (const status of [500, 502, 503, 504]) expect(outcomeUnknown(new ApiError('Broke', 'x', status))).toBe(true)
    expect(outcomeUnknown(new ApiError('Network Error', 'network_error'))).toBe(true)
    expect(outcomeUnknown(new Error('timeout of 30000ms exceeded'))).toBe(true)
  })

  it('refuses a transfer to its own source', () => {
    const result = transferSchema.safeParse({ ...transferDefaults(row), to_location_id: STORE, quantity: '1' })
    expect(result.success).toBe(false)
    expect(result.error?.issues.map((issue) => issue.path.join('.'))).toEqual(['to_location_id'])
  })

  it('builds the signed change as a string and keeps the adjust rules', () => {
    const values = { ...adjustDefaults(row), quantity_change: '2.5', note: ' Recount ' }
    expect(toAdjustBody(values)).toEqual({
      item_id: CANNULA,
      location_id: STORE,
      quantity_change: '-2.5',
      reason: 'adjusted',
      note: 'Recount',
      batch_number: 'CN-2401',
    })
    expect(toAdjustBody({ ...values, direction: 'add', expiry_date: '2026-01-31' })).toMatchObject({
      quantity_change: '2.5',
      expiry_date: '2026-01-31',
    })
    // An expiry typed before switching back to a removal is not sent.
    expect(toAdjustBody({ ...values, expiry_date: '2026-01-31' })).not.toHaveProperty('expiry_date')

    const issues = (input: unknown) => adjustSchema.safeParse(input).error?.issues.map((issue) => issue.path.join('.')) ?? []
    expect(issues(values)).toEqual([])
    expect(issues({ ...values, direction: 'add', reason: 'expired' })).toEqual(['reason'])
    expect(issues({ ...values, note: '  ' })).toEqual(['note'])
    expect(issues({ ...values, note: 'x'.repeat(501) })).toEqual(['note'])
    expect(issues({ ...values, quantity_change: '0' })).toEqual(['quantity_change'])
    expect(issues({ ...values, batch_number: '' })).toEqual(['batch_number'])
    expect(issues({ ...values, reason: 'consumed' })).toEqual(['reason'])
    expect(adjustDefaults({ ...row, is_expired: true }).reason).toBe('expired')
  })

  it('reports an adjustment from the response alone, and reads ids and references carefully', () => {
    const summary = summaryOf(itemOf(CANNULA)!)
    expect(adjustResult({ movements: [], summary }, {})).toBe(
      'Adjusted IV cannula 20G. Across the hospital: 420 piece on hand, 420 piece usable',
    )
    expect(referenceLabel('purchase_order')).toBe('Purchase order')
    expect(referenceLabel('something_new')).toBe('something_new')
    expect(referenceLabel(null)).toBeNull()
    expect(idParam(CANNULA)).toBe(CANNULA)
    expect(idParam('')).toBeUndefined()
    expect(idParam("1' OR 1=1")).toBeUndefined()
    expect(idParam(null)).toBeUndefined()
  })
})
