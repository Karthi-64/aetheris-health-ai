import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { Medicine, MedicineBatch, MedicineStock } from '@/api/pharmacy'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import MedicinesPage from './MedicinesPage'
import MedicineStockPage from './MedicineStockPage'
import { createMedicineSchema, editMedicineSchema, toUpdateBody, type MedicineValues } from './medicineForm'
import { adjustSchema, expiryInWords, receiveSchema, toAdjustBody } from './stockForm'

/**
 * The medicine catalog and batch-level stock, against the merged backend
 * contract (docs/18-API_CONTRACTS.md §9.3; `backend/app/api/v1/medicines.py`,
 * `backend/app/schemas/pharmacy.py`). The real hooks, permission check, `http`
 * wrapper and Axios instance run; only the network adapter is replaced by an
 * in-memory "server" that keeps the catalog and the batches and — like the real
 * one — is the only thing that knows today's date, decides a batch's state and
 * adds up the stock.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → permissions as seeded in backend/app/seeds/seed.py (§9.2).
const ADMIN = [
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
// Neither holds a pharmacy code.
const NURSE = ['notification.read.own', 'patient.read', 'appointment.read', 'appointment.check_in', 'lab.order.read']
const RECEPTIONIST = ['notification.read.own', 'patient.read', 'appointment.read', 'appointment.book', 'invoice.read']

// ── The in-memory server ────────────────────────────────────────────────────

/**
 * The hospital's "today", which only the server knows. Deliberately not the
 * day the tests run: anything the screen worked out from the browser's clock
 * would disagree with every flag and count below, and a browser that judged
 * expiry itself would refuse every date typed here.
 */
const TODAY = '2024-02-10'

const dayNumber = (date: string) =>
  Date.UTC(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10))) / 86_400_000
const daysFromToday = (days: number) => new Date((dayNumber(TODAY) + days) * 86_400_000).toISOString().slice(0, 10)

/** A batch as the server stores it; the four computed fields are added on the way out. */
type StoredBatch = Omit<MedicineBatch, 'days_to_expiry' | 'is_expired' | 'expires_soon' | 'is_dispensable'>

function medicine(sku: string, name: string, extra: Partial<Medicine> = {}): Medicine {
  return {
    id: `m-${sku}`,
    sku,
    name,
    generic_name: null,
    strength: null,
    form: null,
    atc_code: null,
    unit_price: '1.00',
    requires_prescription: true,
    is_active: true,
    created_at: '2024-02-01T00:00:00Z',
    updated_at: '2024-02-01T00:00:00Z',
    ...extra,
  }
}

function batch(sku: string, number: string, expiresIn: number, quantity: number, extra: Partial<StoredBatch> = {}): StoredBatch {
  return {
    id: `b-${number}`,
    medicine_id: `m-${sku}`,
    batch_number: number,
    expiry_date: daysFromToday(expiresIn),
    cost_per_unit: '1.00',
    initial_quantity: quantity,
    quantity_on_hand: quantity,
    is_recalled: false,
    ...extra,
  }
}

let medicines: Medicine[]
let batches: StoredBatch[]
/** What the server lets this user do — the live set, which can drift from the one taken at sign-in. */
let granted: string[]
/** Return an outcome to answer a request yourself; nothing to let the server answer. */
let intercept: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome> | undefined
let fake: FakeApi
let client: QueryClient

const CODE = /^[A-Z0-9][A-Z0-9_./-]*$/
const MONEY = /^\d+(\.\d{1,2})?$/

/** A request-validation 422: `errors` is a list, and custom messages carry Pydantic's prefix. */
const invalid = (field: string, message: string) =>
  fail(422, 'Validation failed.', { error_code: 'VALIDATION_ERROR', errors: [{ field, message }] })
/** A 422 raised by the service: `errors` is an object, and the message is the sentence itself. */
const refused = (field: string, message: string) =>
  fail(422, message, { error_code: 'VALIDATION_ERROR', errors: { errors: [{ field, message }] } })
const rule = (message: string, errors: unknown = null) =>
  fail(400, message, { error_code: 'BUSINESS_RULE_VIOLATION', errors })
const blank = (value: unknown) => (typeof value === 'string' && value.trim() ? value.trim() : null)

/** Query parameters as they go on the wire: an undefined one is not sent at all. */
const wire = (config: InternalAxiosRequestConfig) =>
  JSON.parse(JSON.stringify(config.params ?? {})) as Record<string, unknown>

function view(b: StoredBatch): MedicineBatch {
  const days = dayNumber(b.expiry_date) - dayNumber(TODAY)
  const is_expired = days < 0
  return {
    ...b,
    days_to_expiry: days,
    is_expired,
    expires_soon: !is_expired && days <= 30,
    is_dispensable: b.quantity_on_hand > 0 && !is_expired && !b.is_recalled,
  }
}

/** The server's sums. They overlap, and none of them looks at `is_active` (§9.3). */
function stockOf(m: Medicine): MedicineStock {
  const held = batches
    .filter((b) => b.medicine_id === m.id)
    .sort((a, b) => a.expiry_date.localeCompare(b.expiry_date) || a.id.localeCompare(b.id))
    .map(view)
  const total = (counts: (b: MedicineBatch) => boolean) =>
    held.filter(counts).reduce((sum, b) => sum + b.quantity_on_hand, 0)
  return {
    medicine: structuredClone(m),
    quantity_on_hand: total(() => true),
    dispensable_quantity: total((b) => b.is_dispensable),
    expiring_soon_quantity: total((b) => b.is_dispensable && b.days_to_expiry <= 30),
    expired_quantity: total((b) => b.is_expired),
    recalled_quantity: total((b) => b.is_recalled),
    batches: held,
  }
}

function server(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const method = config.method ?? 'get'
  const lacks = (code: string) =>
    granted.includes(code)
      ? undefined
      : fail(403, `Permission denied. Required: ${code}.`, { error_code: 'PERMISSION_DENIED', errors: null })

  if (url === '/medicines' && method === 'get') {
    const denied = lacks('pharmacy.medicine.read')
    if (denied) return denied
    const params = wire(config)
    const { q, page = 1, page_size: size = 25 } = params as { q?: unknown; page?: number; page_size?: number }
    if ('is_active' in params && typeof params.is_active !== 'boolean') {
      return invalid('query.is_active', 'Input should be a valid boolean')
    }
    if (q !== undefined && (typeof q !== 'string' || q.length > 200)) {
      return invalid('query.q', 'String should have at most 200 characters')
    }
    if (size < 1 || size > 100) return invalid('query.page_size', 'Input should be less than or equal to 100')
    // A prefix of the name or generic name, or the whole SKU; `q` is not trimmed.
    const term = typeof q === 'string' ? q : ''
    const matching = medicines
      .filter(
        (m) =>
          (m.name.toLowerCase().startsWith(term.toLowerCase()) ||
            (m.generic_name ?? '').toLowerCase().startsWith(term.toLowerCase()) ||
            m.sku === term.toUpperCase()) &&
          (params.is_active === undefined || m.is_active === params.is_active),
      )
      .sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id))
    return {
      status: 200,
      data: {
        success: true,
        message: 'Medicines retrieved.',
        data: matching.slice((page - 1) * size, page * size).map((m) => structuredClone(m)),
        metadata: {
          request_id: null,
          pagination: { page, page_size: size, total_records: matching.length, total_pages: Math.max(1, Math.ceil(matching.length / size)) },
        },
      },
    }
  }

  if (url === '/medicines' && method === 'post') {
    const denied = lacks('pharmacy.medicine.create')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const known = ['sku', 'name', 'generic_name', 'strength', 'form', 'atc_code', 'unit_price', 'requires_prescription']
    const extra = Object.keys(body).find((key) => !known.includes(key))
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    const sku = String(body.sku ?? '').trim().toUpperCase()
    if (!CODE.test(sku)) return invalid('sku', 'Value error, SKU may contain only letters, digits and the characters - _ . /')
    if (!blank(body.name)) return invalid('name', 'Value error, Name must not be blank.')
    if (typeof body.unit_price !== 'string' || !MONEY.test(body.unit_price)) {
      return invalid('unit_price', 'Decimal input should have no more than 2 decimal places')
    }
    if (medicines.some((m) => m.sku === sku)) {
      return fail(409, `A medicine with SKU '${sku}' already exists.`, { error_code: 'RESOURCE_CONFLICT', errors: { sku } })
    }
    const created = medicine(sku, String(body.name).trim(), {
      generic_name: blank(body.generic_name),
      strength: blank(body.strength),
      form: blank(body.form),
      atc_code: blank(body.atc_code),
      unit_price: Number(body.unit_price).toFixed(2),
      requires_prescription: body.requires_prescription !== false,
    })
    medicines.push(created)
    return ok(structuredClone(created), 201)
  }

  const match = /^\/medicines\/([^/]+)(?:\/(stock|batches)(?:\/([^/]+)(\/adjust)?)?)?$/.exec(url)
  if (!match) return fail(404, 'Not Found')
  const [, medicineId, section, batchId, adjusting] = match
  const found = medicines.find((m) => m.id === medicineId)
  const noMedicine = fail(404, 'Medicine not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { medicine_id: medicineId } })

  if (!section && method === 'patch') {
    const denied = lacks('pharmacy.medicine.update')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const known = ['name', 'generic_name', 'strength', 'form', 'atc_code', 'unit_price', 'requires_prescription', 'is_active']
    const extra = Object.keys(body).find((key) => !known.includes(key))
    if (extra) return invalid(extra, 'Extra inputs are not permitted')
    const nulls = ['name', 'unit_price', 'requires_prescription', 'is_active'].filter((key) => body[key] === null).sort()
    if (nulls.length > 0) return invalid('', `Value error, Cannot be null: ${nulls.join(', ')}.`)
    if (!found) return noMedicine
    if ('name' in body) found.name = String(body.name).trim()
    for (const key of ['generic_name', 'strength', 'form', 'atc_code'] as const) {
      if (key in body) found[key] = blank(body[key])
    }
    if ('unit_price' in body) found.unit_price = Number(body.unit_price).toFixed(2)
    if ('requires_prescription' in body) found.requires_prescription = body.requires_prescription === true
    if ('is_active' in body) found.is_active = body.is_active === true
    return ok(structuredClone(found))
  }

  if (section === 'stock' && method === 'get') {
    const denied = lacks('pharmacy.batch.read')
    if (denied) return denied
    return found ? ok(stockOf(found)) : noMedicine
  }

  if (section === 'batches' && !batchId && method === 'post') {
    const denied = lacks('pharmacy.batch.create')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    const number = String(body.batch_number ?? '').trim().toUpperCase()
    if (!CODE.test(number)) {
      return invalid('batch_number', 'Value error, Batch number may contain only letters, digits and the characters - _ . /')
    }
    if (typeof body.expiry_date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(body.expiry_date)) {
      return invalid('expiry_date', 'Input should be a valid date')
    }
    if (typeof body.quantity !== 'number' || !Number.isInteger(body.quantity) || body.quantity < 1 || body.quantity > 1_000_000) {
      return invalid('quantity', 'Input should be greater than 0')
    }
    if (typeof body.cost_per_unit !== 'string' || !MONEY.test(body.cost_per_unit)) {
      return invalid('cost_per_unit', 'Decimal input should have no more than 2 decimal places')
    }
    if (!found) return noMedicine
    if (!found.is_active) return rule(`Medicine '${found.sku}' is inactive; stock cannot be received for it.`)
    if (body.expiry_date < TODAY) return refused('expiry_date', 'A batch that has already expired cannot be received.')
    const existing = batches.find((b) => b.medicine_id === found.id && b.batch_number === number)
    if (existing && existing.expiry_date !== body.expiry_date) {
      return refused('expiry_date', `Batch ${number} is already recorded as expiring ${existing.expiry_date}.`)
    }
    if (existing) {
      // A top-up: the cost sent is discarded, and the batch keeps its own.
      existing.quantity_on_hand += body.quantity
      existing.initial_quantity += body.quantity
      return ok(view(existing), 201)
    }
    const created: StoredBatch = {
      id: `b-${number}`,
      medicine_id: found.id,
      batch_number: number,
      expiry_date: body.expiry_date,
      cost_per_unit: Number(body.cost_per_unit).toFixed(2),
      initial_quantity: body.quantity,
      quantity_on_hand: body.quantity,
      is_recalled: false,
    }
    batches.push(created)
    return ok(view(created), 201)
  }

  if (section === 'batches' && batchId) {
    const denied = lacks('pharmacy.batch.update')
    if (denied) return denied
    const body = bodyOf(config) as Record<string, unknown>
    // A batch addressed under another medicine is not found either.
    const target = batches.find((b) => b.id === batchId && b.medicine_id === medicineId)
    const noBatch = fail(404, 'Batch not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { batch_id: batchId } })

    if (!adjusting && method === 'patch') {
      if (Object.keys(body).join() !== 'is_recalled' || typeof body.is_recalled !== 'boolean') {
        return invalid('is_recalled', 'Input should be a valid boolean')
      }
      if (!target) return noBatch
      target.is_recalled = body.is_recalled
      return ok(view(target))
    }

    if (adjusting && method === 'post') {
      const change = body.quantity_change
      if (typeof change !== 'number' || !Number.isInteger(change) || Math.abs(change) > 1_000_000) {
        return invalid('quantity_change', 'Input should be a valid integer')
      }
      if (body.reason !== 'adjusted' && body.reason !== 'expired') return invalid('reason', "Input should be 'adjusted' or 'expired'")
      if (!blank(body.note)) return invalid('note', 'Value error, Note must not be blank.')
      if (change === 0) return invalid('', 'Value error, quantity_change must not be zero.')
      if (body.reason === 'expired' && change > 0) return invalid('', 'Value error, Writing off expired stock must remove units.')
      if (!target) return noBatch
      if (target.quantity_on_hand + change < 0) {
        return rule(`Batch ${target.batch_number} holds ${target.quantity_on_hand} units; ${-change} cannot be removed.`, {
          quantity_on_hand: target.quantity_on_hand,
        })
      }
      // The count moves; what was received in total does not.
      target.quantity_on_hand += change
      return ok(view(target))
    }
  }

  return fail(404, 'Not Found')
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  medicines = [
    medicine('PARA-500', 'Paracetamol', { strength: '500 mg', form: 'tablet', atc_code: 'N02BE01', unit_price: '2.50', requires_prescription: false }),
    medicine('AMOX-500', 'Amoxil', { generic_name: 'Amoxicillin', strength: '500 mg', form: 'capsule', unit_price: '8.00' }),
    medicine('PANT-40', 'Pantoprazole', { strength: '40 mg', form: 'tablet', unit_price: '7.50' }),
    medicine('INSG-100', 'Insulin glargine', { strength: '100 IU/mL', form: 'pen', unit_price: '650.00' }),
    medicine('ORS-21', 'ORS sachet', { generic_name: 'Oral rehydration salts', unit_price: '20.00', requires_prescription: false }),
    medicine('RANI-150', 'Ranitidine (withdrawn)', { strength: '150 mg', form: 'tablet', unit_price: '2.00', is_active: false }),
  ]
  batches = [
    batch('PARA-500', 'PA-2401', 20, 30, { cost_per_unit: '1.10' }),
    batch('PARA-500', 'PA-2502', 365, 500, { cost_per_unit: '1.20' }),
    batch('AMOX-500', 'AX-2311', -30, 40, { cost_per_unit: '4.00' }),
    batch('AMOX-500', 'AX-2503', 300, 200, { cost_per_unit: '4.20' }),
    batch('PANT-40', 'PN-2507', 200, 120, { cost_per_unit: '3.60', is_recalled: true }),
  ]
  granted = []
  intercept = () => undefined
  fake = installFakeApi(async (config) => (await intercept(config)) ?? server(config))
})

afterEach(() => {
  fake.restore()
  signOut()
})

/**
 * `stockGuard` is the permission the router puts in front of the stock page.
 * It is `pharmacy.batch.read` as mounted; a test passes another to show what
 * the page does by itself.
 */
function renderAt(
  path: string,
  permissions: string[],
  stockGuard: 'pharmacy.batch.read' | 'pharmacy.medicine.read' = 'pharmacy.batch.read',
) {
  granted = [...permissions]
  signIn(permissions)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/dashboard" element={<p>Dashboard home</p>} />
          <Route
            path="/pharmacy/medicines"
            element={
              <RequirePermission permission="pharmacy.medicine.read">
                <MedicinesPage />
              </RequirePermission>
            }
          />
          <Route
            path="/pharmacy/medicines/:medicineId"
            element={
              <RequirePermission permission={stockGuard}>
                <MedicineStockPage />
              </RequirePermission>
            }
          />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const sent = (method: string, urlPart: string) => fake.requests(method, urlPart)
const address = (config: InternalAxiosRequestConfig) => `${config.baseURL}${config.url}`
const listRequests = () => fake.sent.filter((c) => c.url === '/medicines' && c.method === 'get')
const dialog = () => screen.findByRole('dialog')
const choose = async (user: ReturnType<typeof userEvent.setup>, combobox: HTMLElement, option: string) => {
  await user.click(combobox)
  await user.click(await screen.findByRole('option', { name: option }))
}
const retype = async (user: ReturnType<typeof userEvent.setup>, input: HTMLElement, value: string) => {
  await user.clear(input)
  await user.type(input, value)
}
/** One of the five stock figures, found by its label. */
const figure = (label: string) => screen.getByText(label, { selector: 'dt' }).parentElement as HTMLElement
const cellsOf = (row: HTMLElement) => within(row).getAllByRole('cell').map((cell) => cell.textContent)
/** A calendar day as the screen should show it, built without parsing the string. */
const shownDate = (date: string) =>
  new Date(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10))).toLocaleDateString(undefined, {
    dateStyle: 'medium',
  })
const stockLoaded = (name: string) => screen.findByRole('heading', { level: 1, name })
/** What happens when anything else on the screen has the lists read again. */
const refetchEverything = () => act(() => client.invalidateQueries())

const PARA_STOCK = '/pharmacy/medicines/m-PARA-500'

// ── Access ──────────────────────────────────────────────────────────────────

describe('pharmacy catalog access', () => {
  it.each([
    ['a nurse', NURSE],
    ['a receptionist', RECEPTIONIST],
  ])('sends %s, who holds no pharmacy permission, to the dashboard and asks the API nothing', async (_who, permissions) => {
    renderAt('/pharmacy/medicines', permissions)

    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    expect(fake.sent).toHaveLength(0)
  })

  it('shows a doctor the catalog and nothing that needs another permission', async () => {
    renderAt('/pharmacy/medicines', DOCTOR)

    expect(await screen.findByRole('row', { name: /Paracetamol/ })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /^Stock for/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add medicine/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Edit/ })).not.toBeInTheDocument()
    // The catalog is all that was asked for: no stock, for any row.
    expect(fake.sent.map((c) => `${c.method} ${c.url}`)).toEqual(['get /medicines'])
  })

  it('turns a doctor away from a stock page, without asking for the stock', async () => {
    renderAt(PARA_STOCK, DOCTOR)

    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    expect(sent('get', '/stock')).toHaveLength(0)
    expect(fake.sent).toHaveLength(0)
  })

  it('asks for no stock for a doctor even when the route lets the page mount', async () => {
    // Mounted behind the catalog's permission, which a doctor holds.
    renderAt(PARA_STOCK, DOCTOR, 'pharmacy.medicine.read')

    expect(await screen.findByText("You can't view stock")).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to medicines/ })).toHaveAttribute('href', '/pharmacy/medicines')
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(sent('get', '/stock')).toHaveLength(0)
    expect(fake.sent).toHaveLength(0)
  })

  it.each([
    ['a pharmacist', PHARMACIST],
    ['an inventory manager', INVENTORY_MANAGER],
  ])('links %s to stock, but offers no way to change the catalog', async (_who, permissions) => {
    renderAt('/pharmacy/medicines', permissions)

    const row = await screen.findByRole('row', { name: /Paracetamol/ })
    expect(within(row).getByRole('link', { name: 'Stock for Paracetamol' })).toHaveAttribute('href', PARA_STOCK)
    expect(screen.queryByRole('button', { name: /Add medicine/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Edit/ })).not.toBeInTheDocument()
    // A link is not a request: no row asks for its stock.
    expect(sent('get', '/stock')).toHaveLength(0)
  })

  it('offers an admin add, edit and stock', async () => {
    renderAt('/pharmacy/medicines', ADMIN)

    const row = await screen.findByRole('row', { name: /Paracetamol/ })
    expect(screen.getByRole('button', { name: /Add medicine/ })).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Edit Paracetamol' })).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: 'Stock for Paracetamol' })).toBeInTheDocument()
  })
})

// ── The catalog ─────────────────────────────────────────────────────────────

describe('medicine catalog', () => {
  it('lists the catalog as the API returns it, with no stock figure', async () => {
    renderAt('/pharmacy/medicines', PHARMACIST)

    const amoxil = await screen.findByRole('row', { name: /Amoxil/ })
    expect(within(amoxil).getByText('Amoxicillin')).toBeInTheDocument()
    expect(within(amoxil).getByText('AMOX-500')).toBeInTheDocument()
    expect(within(amoxil).getByText('500 mg · capsule')).toBeInTheDocument()
    expect(within(amoxil).getByText('8.00')).toBeInTheDocument()
    expect(within(amoxil).getByText('Needed')).toBeInTheDocument()
    expect(within(amoxil).getByText('Active')).toBeInTheDocument()
    const ors = screen.getByRole('row', { name: /ORS sachet/ })
    expect(within(ors).getByText('Not needed')).toBeInTheDocument()
    expect(within(ors).getByText('—')).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /Ranitidine/ })).getByText('Inactive')).toBeInTheDocument()
    // Ordered by the API, by name.
    expect(screen.getAllByRole('row').slice(1).map((row) => cellsOf(row)[1])).toEqual([
      'AMOX-500',
      'INSG-100',
      'ORS-21',
      'PANT-40',
      'PARA-500',
      'RANI-150',
    ])
    expect(screen.queryByText(/\bunits\b|on hand|in stock|dispensable/i)).not.toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Pharmacy sections' })).getByRole('link', { name: 'Medicines' })).toBeInTheDocument()

    const [request] = listRequests()
    expect(request.method).toBe('get')
    expect(address(request)).toBe('/api/v1/medicines')
    // No empty filter is sent: the API answers 422 to one.
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    expect(fake.sent).toHaveLength(1)
  })

  it('sends the search trimmed, once the typing stops', async () => {
    let searchedAt = 0
    intercept = (c) => {
      if (c.url === '/medicines' && 'q' in wire(c)) searchedAt = performance.now()
      return undefined
    }
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Paracetamol/ })
    const box = screen.getByRole('textbox', { name: 'Search by start of name or whole SKU…' })

    const typedAt = performance.now()
    fireEvent.change(box, { target: { value: '  amo' } })
    fireEvent.change(box, { target: { value: '  amox  ' } })
    expect(listRequests()).toHaveLength(1)

    // The server does not trim: sent as typed, this would match nothing.
    await waitFor(() => expect(screen.queryByRole('row', { name: /Paracetamol/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Amoxil/ })).toBeInTheDocument()
    // One search, for what was left in the box, and not before the pause.
    expect(listRequests().map((c) => wire(c).q)).toEqual([undefined, 'amox'])
    expect(wire(listRequests()[1])).toEqual({ q: 'amox', page: 1, page_size: 25 })
    expect(searchedAt - typedAt).toBeGreaterThanOrEqual(250)
  })

  it('keeps the search within the 200 characters the API accepts', async () => {
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Paracetamol/ })

    fireEvent.change(screen.getByRole('textbox', { name: 'Search by start of name or whole SKU…' }), {
      target: { value: 'x'.repeat(250) },
    })

    // A longer one is a 422, which would show as a failure to load.
    expect(await screen.findByText('No matching medicines')).toBeInTheDocument()
    expect(wire(listRequests().at(-1)!).q).toBe('x'.repeat(200))
  })

  it('finds by generic name and by whole SKU, and says why a part of a SKU finds nothing', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Paracetamol/ })
    const box = screen.getByRole('textbox', { name: 'Search by start of name or whole SKU…' })

    await user.type(box, 'oral')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Paracetamol/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /ORS sachet/ })).toBeInTheDocument()

    await retype(user, box, 'para-500')
    expect(await screen.findByRole('row', { name: /Paracetamol/ })).toBeInTheDocument()
    expect(wire(listRequests().at(-1)!)).toEqual({ q: 'para-500', page: 1, page_size: 25 })

    await retype(user, box, 'para-5')
    expect(await screen.findByText('No matching medicines')).toBeInTheDocument()
    expect(screen.getByText(/start of a name or generic name, or a whole SKU/)).toBeInTheDocument()
  })

  it('sends nothing for a search of spaces', async () => {
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Paracetamol/ })

    fireEvent.change(screen.getByRole('textbox', { name: 'Search by start of name or whole SKU…' }), { target: { value: '   ' } })
    await new Promise((resolve) => setTimeout(resolve, 400))

    expect(listRequests().every((c) => !('q' in wire(c)))).toBe(true)
    expect(screen.getByRole('row', { name: /Paracetamol/ })).toBeInTheDocument()
  })

  it('filters by status with the API\'s own parameter, and omits it for both', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Ranitidine/ })
    const status = screen.getByRole('combobox', { name: 'Filter by status' })

    await choose(user, status, 'Active only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Ranitidine/ })).not.toBeInTheDocument())
    expect(wire(listRequests().at(-1)!)).toEqual({ is_active: true, page: 1, page_size: 25 })

    await choose(user, status, 'Inactive only')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Paracetamol/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Ranitidine/ })).toBeInTheDocument()
    expect(wire(listRequests().at(-1)!)).toEqual({ is_active: false, page: 1, page_size: 25 })

    const before = listRequests().length
    await choose(user, status, 'Active and inactive')
    expect(await screen.findByRole('row', { name: /Paracetamol/ })).toBeInTheDocument()
    // Already in the cache, or asked for again — either way without the filter.
    expect(listRequests().slice(before).every((c) => !('is_active' in wire(c)))).toBe(true)
  })

  it('says so when no medicine has the chosen status', async () => {
    medicines = medicines.filter((m) => m.is_active)
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Paracetamol/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Inactive only')

    expect(await screen.findByText('No matching medicines')).toBeInTheDocument()
    expect(screen.getByText('No medicine in the catalog has this status.')).toBeInTheDocument()
  })

  it('pages through the server\'s pages', async () => {
    medicines = Array.from({ length: 30 }, (_, n) => medicine(`MED-${String(n).padStart(2, '0')}`, `Medicine ${String(n).padStart(2, '0')}`))
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Medicine 00/ })
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /Medicine 29/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByRole('row', { name: /Medicine 29/ })).toBeInTheDocument()
    expect(wire(listRequests().at(-1)!)).toEqual({ page: 2, page_size: 25 })
  })

  const thirty = () =>
    Array.from({ length: 30 }, (_, n) => medicine(`MED-${String(n).padStart(2, '0')}`, `Medicine ${String(n).padStart(2, '0')}`))

  it('searches from a later page with one request, for page 1 of the new term', async () => {
    medicines = thirty()
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Medicine 00/ })
    await user.click(screen.getByRole('button', { name: 'Next' }))
    await screen.findByRole('row', { name: /Medicine 29/ })
    expect(listRequests()).toHaveLength(2)

    fireEvent.change(screen.getByRole('textbox', { name: 'Search by start of name or whole SKU…' }), {
      target: { value: 'medicine 1' },
    })

    expect(await screen.findByRole('row', { name: /Medicine 10/ })).toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 100))
    // No page 1 of the unfiltered catalog on the way.
    expect(listRequests().map(wire)).toEqual([
      { page: 1, page_size: 25 },
      { page: 2, page_size: 25 },
      { q: 'medicine 1', page: 1, page_size: 25 },
    ])
    expect(screen.getAllByRole('row').slice(1)).toHaveLength(10)
  })

  it('stays on the page it is on when only spaces are typed', async () => {
    medicines = thirty()
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)
    await screen.findByRole('row', { name: /Medicine 00/ })
    await user.click(screen.getByRole('button', { name: 'Next' }))
    await screen.findByRole('row', { name: /Medicine 29/ })

    fireEvent.change(screen.getByRole('textbox', { name: 'Search by start of name or whole SKU…' }), { target: { value: '   ' } })
    await new Promise((resolve) => setTimeout(resolve, 400))

    expect(screen.getByText('Page 2 of 2')).toBeInTheDocument()
    expect(screen.getByRole('row', { name: /Medicine 29/ })).toBeInTheDocument()
    expect(listRequests()).toHaveLength(2)
  })

  it('goes to the last page there is when the one it is on empties', async () => {
    medicines = thirty().slice(0, 26)
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', ADMIN)
    await screen.findByRole('row', { name: /Medicine 00/ })
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Active only')
    await waitFor(() => expect(wire(listRequests().at(-1)!)).toEqual({ is_active: true, page: 1, page_size: 25 }))
    await user.click(await screen.findByRole('button', { name: 'Next' }))
    // The one medicine on page 2.
    await user.click(await screen.findByRole('button', { name: 'Edit Medicine 25' }))
    const d = await dialog()
    await user.click(within(d).getByRole('checkbox', { name: /^Active/ }))

    // Hold the read of page 1 that follows, to see what is on screen meanwhile.
    let release: () => void = () => {}
    let held = false
    const before = listRequests().length
    intercept = (c) => {
      if (c.url === '/medicines' && c.method === 'get' && wire(c).page === 1) {
        held = true
        return new Promise<Outcome>((resolve) => (release = () => resolve(server(c))))
      }
      return undefined
    }
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Medicine 25'))
    await waitFor(() => expect(held).toBe(true))

    // Twenty-five medicines still match: page 2 being empty is not "no match".
    expect(screen.queryByText('No matching medicines')).not.toBeInTheDocument()
    release()
    expect(await screen.findByRole('row', { name: /Medicine 00/ })).toBeInTheDocument()
    await waitFor(() => expect(screen.getAllByRole('row').slice(1)).toHaveLength(25))
    expect(screen.queryByText('No matching medicines')).not.toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /Medicine 25/ })).not.toBeInTheDocument()
    // Page 2 of what is now one page was asked for, answered empty, and left.
    expect(listRequests().slice(before).map(wire)).toEqual([
      { is_active: true, page: 2, page_size: 25 },
      { is_active: true, page: 1, page_size: 25 },
    ])
  })

  it('shows loading placeholders until the catalog arrives', async () => {
    let finish: () => void = () => {}
    intercept = (c) => (c.url === '/medicines' ? new Promise<Outcome>((resolve) => (finish = () => resolve(server(c)))) : undefined)
    renderAt('/pharmacy/medicines', PHARMACIST)

    await waitFor(() => expect(listRequests()).toHaveLength(1))
    expect(screen.queryByText('No medicines in the catalog')).not.toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /Paracetamol/ })).not.toBeInTheDocument()
    // Header row plus skeleton rows.
    expect(screen.getAllByRole('row').length).toBeGreaterThan(1)

    finish()
    expect(await screen.findByRole('row', { name: /Paracetamol/ })).toBeInTheDocument()
  })

  it('tells someone who cannot add medicines that the catalog is empty', async () => {
    medicines = []
    renderAt('/pharmacy/medicines', PHARMACIST)

    expect(await screen.findByText('No medicines in the catalog')).toBeInTheDocument()
    expect(screen.getByText('Medicines added to the catalog will appear here.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add medicine/ })).not.toBeInTheDocument()
  })

  it('invites an admin to fill an empty catalog', async () => {
    medicines = []
    renderAt('/pharmacy/medicines', ADMIN)

    expect(await screen.findByText('No medicines in the catalog')).toBeInTheDocument()
    expect(screen.getByText(/Add the medicines your pharmacy stocks/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Add medicine/ }).length).toBeGreaterThan(0)
  })

  it('offers a retry when the catalog cannot be loaded, without the server\'s words', async () => {
    intercept = (c) => (c.url === '/medicines' ? fail(500, 'psycopg.OperationalError: boom') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)

    expect(await screen.findByText("Couldn't load the medicine catalog")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom|psycopg/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: /Paracetamol/ })).toBeInTheDocument()
  })

  it('opens a medicine\'s stock from its row', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines', PHARMACIST)

    await user.click(await screen.findByRole('link', { name: 'Stock for Amoxil' }))

    expect(await stockLoaded('Amoxil')).toBeInTheDocument()
    expect(address(sent('get', '/stock')[0])).toBe('/api/v1/medicines/m-AMOX-500/stock')
  })
})

// ── Adding a medicine ───────────────────────────────────────────────────────

describe('adding a medicine', () => {
  const openForm = async (user: ReturnType<typeof userEvent.setup>) => {
    renderAt('/pharmacy/medicines', ADMIN)
    await screen.findByRole('row', { name: /Paracetamol/ })
    await user.click(screen.getByRole('button', { name: /Add medicine/ }))
    return dialog()
  }
  const fill = async (user: ReturnType<typeof userEvent.setup>, d: HTMLElement, sku: string, name: string, price: string) => {
    await user.type(within(d).getByLabelText(/^SKU/), sku)
    await user.type(within(d).getByLabelText(/^Name/), name)
    await user.type(within(d).getByLabelText(/^Selling price/), price)
  }

  it('posts the medicine exactly as the API takes it', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    expect(d).toHaveTextContent("The SKU can't be changed afterwards")
    expect(within(d).getByRole('checkbox', { name: /Needs a prescription/ })).toBeChecked()
    // `is_active` is not a field of a new medicine.
    expect(within(d).queryByRole('checkbox', { name: /^Active/ })).not.toBeInTheDocument()

    await user.type(within(d).getByLabelText(/^SKU/), ' ibu-200 ')
    await user.type(within(d).getByLabelText(/^Name/), 'Brufen')
    await user.type(within(d).getByLabelText(/^Generic name/), 'Ibuprofen')
    await user.type(within(d).getByLabelText(/^Strength/), '200 mg')
    await user.type(within(d).getByLabelText(/^Form/), 'tablet')
    await user.type(within(d).getByLabelText(/^Selling price/), '4.5')
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))

    // The SKU is the server's, in capitals.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Added Brufen (IBU-200)'))
    const [request] = sent('post', '/medicines')
    expect(request.method).toBe('post')
    expect(address(request)).toBe('/api/v1/medicines')
    // An optional field left blank goes as null; `is_active` is never sent.
    expect(bodyOf(request)).toEqual({
      sku: 'IBU-200',
      name: 'Brufen',
      generic_name: 'Ibuprofen',
      strength: '200 mg',
      form: 'tablet',
      atc_code: null,
      unit_price: '4.5',
      requires_prescription: true,
    })
    const row = await screen.findByRole('row', { name: /Brufen/ })
    expect(within(row).getByText('4.50')).toBeInTheDocument()
    expect(within(row).getByText('Needed')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('sends an unticked prescription flag and a zero price as they are', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'SAL-1', 'Saline', '0')
    await user.click(within(d).getByRole('checkbox', { name: /Needs a prescription/ }))
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))

    await waitFor(() => expect(sent('post', '/medicines')).toHaveLength(1))
    expect(bodyOf(sent('post', '/medicines')[0])).toMatchObject({ sku: 'SAL-1', unit_price: '0', requires_prescription: false })
    expect(await screen.findByRole('row', { name: /Saline/ })).toBeInTheDocument()
  })

  it('checks the form before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)

    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))
    expect(await within(d).findByText('Enter a SKU')).toBeInTheDocument()
    expect(within(d).getByText('Enter a name')).toBeInTheDocument()
    expect(within(d).getByText('Enter the selling price per unit')).toBeInTheDocument()

    await fill(user, d, 'para 500', 'Paracetamol again', '2.505')
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))
    expect(await within(d).findByText('Letters, digits and - _ . / only, starting with a letter or digit')).toBeInTheDocument()
    expect(within(d).getByText('Enter an amount such as 2.50')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^SKU/)).toBeInvalid()

    // A letter that grows when uppercased would overflow the column as a 500.
    await retype(user, within(d).getByLabelText(/^SKU/), 'straße')
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))
    expect(await within(d).findByText('Letters, digits and - _ . / only, starting with a letter or digit')).toBeInTheDocument()
    expect(sent('post', '/medicines')).toHaveLength(0)
  })

  it('shows the API\'s message for a SKU already in use', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'para-500', 'Paracetamol again', '3.00')
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("A medicine with SKU 'PARA-500' already exists.")
    expect(toastError).toHaveBeenCalledWith("A medicine with SKU 'PARA-500' already exists.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(medicines.filter((m) => m.sku === 'PARA-500')).toHaveLength(1)
  })

  it('shows the API\'s refusal when the permission was taken away meanwhile', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    granted = granted.filter((code) => code !== 'pharmacy.medicine.create')
    await fill(user, d, 'IBU-200', 'Brufen', '4.50')
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Permission denied. Required: pharmacy.medicine.create.')
    expect(toastError).toHaveBeenCalledWith('Permission denied. Required: pharmacy.medicine.create.')
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(medicines.some((m) => m.sku === 'IBU-200')).toBe(false)
  })

  it('puts a rejected field\'s message under that field, without Pydantic\'s prefix', async () => {
    intercept = (c) =>
      c.url === '/medicines' && c.method === 'post'
        ? invalid('sku', 'Value error, SKU may contain only letters, digits and the characters - _ . /')
        : undefined
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'IBU-200', 'Brufen', '4.50')
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))

    expect(await within(d).findByText('SKU may contain only letters, digits and the characters - _ . /')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^SKU/)).toBeInvalid()
    expect(d).not.toHaveTextContent('Value error')
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
  })

  it('keeps the server\'s words out of sight when the server fails', async () => {
    intercept = (c) => (c.url === '/medicines' && c.method === 'post' ? fail(500, 'IntegrityError: boom') : undefined)
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'IBU-200', 'Brufen', '4.50')
    await user.click(within(d).getByRole('button', { name: 'Add medicine' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("Couldn't add the medicine. Please try again.")
    expect(document.body.textContent).not.toMatch(/boom|IntegrityError/)
    expect(toastError).toHaveBeenCalledWith("Couldn't add the medicine. Please try again.")
  })

  it('adds one medicine however many times the form is submitted', async () => {
    let release: () => void = () => {}
    intercept = (c) =>
      c.url === '/medicines' && c.method === 'post'
        ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c))))
        : undefined
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'IBU-200', 'Brufen', '4.50')
    const form = within(d).getByRole('button', { name: 'Add medicine' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Adding…' })).toBeDisabled()
    await waitFor(() => expect(sent('post', '/medicines')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/medicines')).toHaveLength(1)
    expect(toastError).not.toHaveBeenCalled()
  })
})

// ── Editing a medicine ──────────────────────────────────────────────────────

describe('editing a medicine', () => {
  const openForm = async (user: ReturnType<typeof userEvent.setup>, name: string) => {
    renderAt('/pharmacy/medicines', ADMIN)
    await user.click(await screen.findByRole('button', { name: `Edit ${name}` }))
    return dialog()
  }

  it('sends only what changed, a cleared field as null, and never the SKU', async () => {
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')

    expect(within(d).getByRole('heading', { name: 'Edit medicine' })).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Name/)).toHaveValue('Amoxil')
    expect(within(d).getByLabelText(/^SKU/)).toBeDisabled()
    expect(within(d).getByLabelText(/^SKU/)).toHaveValue('AMOX-500')
    expect(within(d).getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(d).toHaveTextContent('Medicines already dispensed keep the price they were charged at')

    await retype(user, within(d).getByLabelText(/^Selling price/), '9.25')
    await user.clear(within(d).getByLabelText(/^Generic name/))
    await user.click(within(d).getByRole('checkbox', { name: /Needs a prescription/ }))
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Amoxil'))
    const [request] = sent('patch', '/medicines')
    expect(request.method).toBe('patch')
    expect(address(request)).toBe('/api/v1/medicines/m-AMOX-500')
    expect(bodyOf(request)).toEqual({ unit_price: '9.25', generic_name: null, requires_prescription: false })
    const row = await screen.findByRole('row', { name: /Amoxil/ })
    await waitFor(() => expect(within(row).getByText('9.25')).toBeInTheDocument())
    expect(within(row).getByText('Not needed')).toBeInTheDocument()
    expect(within(row).queryByText('Amoxicillin')).not.toBeInTheDocument()
  })

  it('retires a medicine by sending is_active alone', async () => {
    const user = userEvent.setup()
    const d = await openForm(user, 'Paracetamol')

    const active = within(d).getByRole('checkbox', { name: /^Active — can be prescribed, ordered and dispensed/ })
    expect(active).toBeChecked()
    await user.click(active)
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved Paracetamol'))
    expect(bodyOf(sent('patch', '/medicines')[0])).toEqual({ is_active: false })
    const row = await screen.findByRole('row', { name: /Paracetamol/ })
    await waitFor(() => expect(within(row).getByText('Inactive')).toBeInTheDocument())
  })

  it('checks an edit before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')

    await user.clear(within(d).getByLabelText(/^Name/))
    await retype(user, within(d).getByLabelText(/^Selling price/), '-1')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByText('Enter a name')).toBeInTheDocument()
    expect(within(d).getByText('Enter an amount such as 2.50')).toBeInTheDocument()
    expect(sent('patch', '/medicines')).toHaveLength(0)
  })

  it('says so when the medicine is gone', async () => {
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')
    medicines = medicines.filter((m) => m.sku !== 'AMOX-500')
    await retype(user, within(d).getByLabelText(/^Selling price/), '9.25')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Medicine not found.')
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('shows the API\'s refusal when the permission was taken away meanwhile', async () => {
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')
    granted = granted.filter((code) => code !== 'pharmacy.medicine.update')
    await retype(user, within(d).getByLabelText(/^Selling price/), '9.25')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Permission denied. Required: pharmacy.medicine.update.')
    expect(toastError).toHaveBeenCalledWith('Permission denied. Required: pharmacy.medicine.update.')
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(medicines.find((m) => m.sku === 'AMOX-500')?.unit_price).toBe('8.00')
    // What was typed is still there to send again.
    expect(within(d).getByLabelText(/^Selling price/)).toHaveValue('9.25')
  })

  it('keeps the server\'s words out of sight when the server fails', async () => {
    intercept = (c) => (c.method === 'patch' ? fail(500, 'IntegrityError: boom') : undefined)
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')
    await retype(user, within(d).getByLabelText(/^Selling price/), '9.25')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("Couldn't save the medicine. Please try again.")
    expect(document.body.textContent).not.toMatch(/boom|IntegrityError/)
    expect(toastError).toHaveBeenCalledWith("Couldn't save the medicine. Please try again.")
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('saves once however many times the form is submitted', async () => {
    let release: () => void = () => {}
    intercept = (c) => (c.method === 'patch' ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c)))) : undefined)
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')
    await retype(user, within(d).getByLabelText(/^Selling price/), '9.25')
    const form = within(d).getByRole('button', { name: 'Save changes' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Saving…' })).toBeDisabled()
    await waitFor(() => expect(sent('patch', '/medicines')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('patch', '/medicines')).toHaveLength(1)
    expect(bodyOf(sent('patch', '/medicines')[0])).toEqual({ unit_price: '9.25' })
    expect(toastError).not.toHaveBeenCalled()
  })

  it('closes an open edit rather than carry it onto the medicine that takes its row', async () => {
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')
    await retype(user, within(d).getByLabelText(/^Selling price/), '9.25')
    // Added by someone else, and first by name: every row moves down one.
    medicines.push(medicine('AAR-1', 'Aardvark balm', { unit_price: '1.00' }))
    await refetchEverything()

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByRole('row', { name: /Aardvark balm/ })).toBeInTheDocument()
    expect(sent('patch', '/medicines')).toHaveLength(0)
    expect(medicines.find((m) => m.sku === 'AAR-1')?.unit_price).toBe('1.00')
  })

  it('shows a whole-form rule the server enforces above the form', async () => {
    intercept = (c) => (c.method === 'patch' ? invalid('', 'Value error, Cannot be null: unit_price.') : undefined)
    const user = userEvent.setup()
    const d = await openForm(user, 'Amoxil')
    await retype(user, within(d).getByLabelText(/^Selling price/), '9.25')
    await user.click(within(d).getByRole('button', { name: 'Save changes' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Cannot be null: unit_price.')
    expect(d).not.toHaveTextContent('Value error')
  })
})

// ── A medicine's stock ──────────────────────────────────────────────────────

describe('medicine stock', () => {
  it('shows the server\'s totals as separate figures, and its batches in its order', async () => {
    medicines.push(medicine('MIX-1', 'Mixture', { strength: '5 mg/mL', form: 'syrup', unit_price: '12.00' }))
    batches.push(
      batch('MIX-1', 'MX-A', -5, 7, { is_recalled: true }), // expired and recalled: counted under each
      batch('MIX-1', 'MX-B', 0, 20), // expires today: still dispensable
      batch('MIX-1', 'MX-C', 31, 100, { cost_per_unit: '3.25' }), // a day outside the 30-day window
      batch('MIX-1', 'MX-D', 90, 30, { is_recalled: true }),
      batch('MIX-1', 'MX-E', 400, 0, { initial_quantity: 50 }), // emptied
      batch('MIX-1', 'MX-F', 1, 12, { initial_quantity: 10 }), // corrected upwards past what was received
      batch('MIX-1', 'MX-G', 10, 9, { is_recalled: true }), // expires soon, but recalled: not in the expiring count
    )
    renderAt('/pharmacy/medicines/m-MIX-1', INVENTORY_MANAGER)

    expect(await stockLoaded('Mixture')).toBeInTheDocument()
    const header = within(document.querySelector('header') as HTMLElement)
    expect(header.getByText('MIX-1')).toBeInTheDocument()
    expect(header.getByText('Active')).toBeInTheDocument()
    expect(document.querySelector('header')).toHaveTextContent('5 mg/mL · syrup')
    expect(document.querySelector('header')).toHaveTextContent('Selling price 12.00 per unit')
    expect(screen.getByRole('link', { name: /Back to medicines/ })).toHaveAttribute('href', '/pharmacy/medicines')

    // The server's five numbers. They overlap: 132 + 7 + 46 is not 178, and the
    // units in batches flagged `expires_soon` come to 41, not 32.
    expect(figure('Dispensable today')).toHaveTextContent('132 units')
    expect(figure('On hand')).toHaveTextContent('178 units')
    expect(figure('On hand')).toHaveTextContent('Every unit held, including expired and recalled.')
    expect(figure('Expiring within 30 days')).toHaveTextContent('32 units')
    expect(figure('Expired')).toHaveTextContent('7 units')
    expect(figure('Recalled')).toHaveTextContent('46 units')
    expect(within(screen.getByRole('region', { name: 'Stock summary' })).getByText(/separate counts, not parts of one total/)).toBeInTheDocument()

    const rows = within(screen.getByRole('region', { name: 'Batches' })).getAllByRole('row').slice(1)
    expect(rows.map((row) => cellsOf(row)[0])).toEqual(['MX-A', 'MX-B', 'MX-F', 'MX-G', 'MX-C', 'MX-D', 'MX-E'])
    // The days are the server's, counted on its date — not this machine's.
    expect(rows.map((row) => cellsOf(row)[1])).toEqual([
      `${shownDate(daysFromToday(-5))}expired 5 days ago`,
      `${shownDate(daysFromToday(0))}expires today`,
      `${shownDate(daysFromToday(1))}expires in 1 day`,
      `${shownDate(daysFromToday(10))}expires in 10 days`,
      `${shownDate(daysFromToday(31))}expires in 31 days`,
      `${shownDate(daysFromToday(90))}expires in 90 days`,
      `${shownDate(daysFromToday(400))}expires in 400 days`,
    ])
    expect(rows.map((row) => cellsOf(row)[5])).toEqual([
      'Recalled',
      'Expires soon',
      'Expires soon',
      'Recalled',
      'Dispensable',
      'Recalled',
      'Empty',
    ])
    // On hand, received in total, cost per unit.
    expect(cellsOf(rows[2]).slice(2, 5)).toEqual(['12', '10', '1.00'])
    expect(cellsOf(rows[4]).slice(2, 5)).toEqual(['100', '100', '3.25'])
    expect(cellsOf(rows[6]).slice(2, 5)).toEqual(['0', '50', '1.00'])

    const [request] = fake.sent
    expect(request.method).toBe('get')
    expect(address(request)).toBe('/api/v1/medicines/m-MIX-1/stock')
    expect(wire(request)).toEqual({})
    expect(fake.sent).toHaveLength(1)
  })

  it('marks an expired batch as the server flags it', async () => {
    renderAt('/pharmacy/medicines/m-AMOX-500', PHARMACIST)
    await stockLoaded('Amoxil')

    const expired = screen.getByRole('row', { name: /AX-2311/ })
    expect(within(expired).getByText('Expired')).toBeInTheDocument()
    expect(within(expired).getByText('expired 30 days ago')).toBeInTheDocument()
    expect(figure('Dispensable today')).toHaveTextContent('200 units')
    expect(figure('On hand')).toHaveTextContent('240 units')
    expect(figure('Expired')).toHaveTextContent('40 units')
  })

  it.each([
    ['an admin', ADMIN],
    ['a pharmacist', PHARMACIST],
  ])('offers %s receive, adjust and recall', async (_who, permissions) => {
    renderAt(PARA_STOCK, permissions)
    await stockLoaded('Paracetamol')

    expect(screen.getByRole('button', { name: /Receive stock/ })).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /PA-2401/ })
    expect(within(row).getByRole('button', { name: 'Adjust batch PA-2401' })).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Recall batch PA-2401' })).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /^Lift recall/ })).not.toBeInTheDocument()
  })

  it('offers to lift a recall, not to recall again, on a recalled batch', async () => {
    renderAt('/pharmacy/medicines/m-PANT-40', PHARMACIST)
    await stockLoaded('Pantoprazole')

    const row = screen.getByRole('row', { name: /PN-2507/ })
    expect(within(row).getByText('Recalled')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: 'Lift recall on batch PN-2507' })).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /^Recall batch/ })).not.toBeInTheDocument()
    // On hand, and none of it dispensable.
    expect(figure('Dispensable today')).toHaveTextContent('0 units')
    expect(figure('Recalled')).toHaveTextContent('120 units')
  })

  it('shows an inventory manager the stock and no way to change it', async () => {
    renderAt(PARA_STOCK, INVENTORY_MANAGER)
    await stockLoaded('Paracetamol')

    expect(screen.getByRole('row', { name: /PA-2401/ })).toBeInTheDocument()
    expect(figure('On hand')).toHaveTextContent('530 units')
    expect(screen.queryByRole('button', { name: /Receive stock/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Adjust batch/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Recall batch/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Lift recall/ })).not.toBeInTheDocument()
    expect(fake.sent.map((c) => `${c.method} ${c.url}`)).toEqual(['get /medicines/m-PARA-500/stock'])
  })

  it('does not present an inactive medicine as in stock, or offer to receive for it', async () => {
    // The server's dispensable count ignores is_active: it reports 120 here.
    batches.push(batch('RANI-150', 'RN-1', 100, 120))
    renderAt('/pharmacy/medicines/m-RANI-150', PHARMACIST)
    await stockLoaded('Ranitidine (withdrawn)')

    expect(within(document.querySelector('header') as HTMLElement).getByText('Inactive')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Receive stock/ })).not.toBeInTheDocument()
    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent('This medicine is inactive')
    expect(notice).toHaveTextContent('no stock can be received for it — which is why there is no Receive stock button here')
    expect(screen.queryByText('Dispensable today')).not.toBeInTheDocument()
    expect(figure('In date, not recalled')).toHaveTextContent('120 units')
    expect(figure('In date, not recalled')).toHaveTextContent('Inactive — cannot be dispensed')
    expect(figure('On hand')).toHaveTextContent('120 units')
    // What the API still allows on its batches stays on offer.
    expect(screen.getByRole('button', { name: 'Adjust batch RN-1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Recall batch RN-1' })).toBeInTheDocument()
  })

  it('does not call any batch of an inactive medicine dispensable', async () => {
    // The server flags the first two `is_dispensable`: it does not look at the medicine.
    batches.push(
      batch('RANI-150', 'RN-1', 100, 120),
      batch('RANI-150', 'RN-2', 10, 15),
      batch('RANI-150', 'RN-3', -3, 8),
      batch('RANI-150', 'RN-4', 200, 40, { is_recalled: true }),
      batch('RANI-150', 'RN-5', 300, 0, { initial_quantity: 20 }),
    )
    renderAt('/pharmacy/medicines/m-RANI-150', PHARMACIST)
    await stockLoaded('Ranitidine (withdrawn)')

    const region = screen.getByRole('region', { name: 'Batches' })
    const rows = within(region).getAllByRole('row').slice(1)
    expect(rows.map((row) => [cellsOf(row)[0], cellsOf(row)[5]])).toEqual([
      ['RN-3', 'Expired'],
      ['RN-2', 'Held — medicine inactive'],
      ['RN-1', 'Held — medicine inactive'],
      ['RN-4', 'Recalled'],
      ['RN-5', 'Empty'],
    ])
    expect(within(region).queryByText('Dispensable')).not.toBeInTheDocument()
    expect(within(region).queryByText('Expires soon')).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/Dispensable today/)
  })

  it('calls the same batches dispensable once the medicine is active', async () => {
    medicines.find((m) => m.sku === 'RANI-150')!.is_active = true
    batches.push(batch('RANI-150', 'RN-1', 100, 120), batch('RANI-150', 'RN-2', 10, 15))
    renderAt('/pharmacy/medicines/m-RANI-150', PHARMACIST)
    await stockLoaded('Ranitidine (withdrawn)')

    const rows = within(screen.getByRole('region', { name: 'Batches' })).getAllByRole('row').slice(1)
    expect(rows.map((row) => [cellsOf(row)[0], cellsOf(row)[5]])).toEqual([
      ['RN-2', 'Expires soon'],
      ['RN-1', 'Dispensable'],
    ])
    expect(screen.queryByText('Held — medicine inactive')).not.toBeInTheDocument()
  })

  it('keeps a loaded page when reading the stock again fails, and offers a retry', async () => {
    const user = userEvent.setup()
    renderAt(PARA_STOCK, PHARMACIST)
    await stockLoaded('Paracetamol')
    intercept = (c) => (c.url?.endsWith('/stock') ? fail(500, 'KeyError: boom') : undefined)
    // Stock moved at another counter; this screen does not get to see it yet.
    batches.find((b) => b.batch_number === 'PA-2401')!.quantity_on_hand = 5
    await refetchEverything()

    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('These figures may be out of date')
    expect(notice).toHaveTextContent('What is shown is the last reading.')
    expect(document.body.textContent).not.toMatch(/boom|KeyError/)
    // The last reading stays, with everything that can be done from it.
    expect(screen.getByRole('heading', { level: 1, name: 'Paracetamol' })).toBeInTheDocument()
    expect(figure('On hand')).toHaveTextContent('530 units')
    expect(screen.getByRole('button', { name: 'Adjust batch PA-2401' })).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load this medicine's stock")).not.toBeInTheDocument()

    intercept = () => undefined
    await user.click(within(notice).getByRole('button', { name: /Retry/ }))

    await waitFor(() => expect(figure('On hand')).toHaveTextContent('505 units'))
    expect(screen.queryByText('These figures may be out of date')).not.toBeInTheDocument()
  })

  it('gives the page up when a later reading says the medicine is gone', async () => {
    renderAt(PARA_STOCK, PHARMACIST)
    await stockLoaded('Paracetamol')
    medicines = medicines.filter((m) => m.sku !== 'PARA-500')
    await refetchEverything()

    expect(await screen.findByText('Medicine not found')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Adjust batch/ })).not.toBeInTheDocument()
  })

  it('says a medicine has no batches yet', async () => {
    renderAt('/pharmacy/medicines/m-INSG-100', PHARMACIST)
    await stockLoaded('Insulin glargine')

    expect(screen.getByText('No batches yet')).toBeInTheDocument()
    expect(screen.getByText(/Use Receive stock to record its first batch/)).toBeInTheDocument()
    expect(figure('On hand')).toHaveTextContent('0 units')
  })

  it('shows a loading state, then the stock', async () => {
    let finish: () => void = () => {}
    intercept = (c) => (c.url?.endsWith('/stock') ? new Promise<Outcome>((resolve) => (finish = () => resolve(server(c)))) : undefined)
    renderAt(PARA_STOCK, PHARMACIST)

    // A role, so that the label is announced and not only present.
    const loading = await screen.findByRole('status', { name: 'Loading stock' })
    expect(loading).toHaveAttribute('aria-busy', 'true')
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    finish()
    expect(await stockLoaded('Paracetamol')).toBeInTheDocument()
  })

  it('says a medicine is not found rather than failing', async () => {
    renderAt('/pharmacy/medicines/missing', PHARMACIST)

    expect(await screen.findByText('Medicine not found')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Back to medicines/ })).toBeInTheDocument()
  })

  it('says so when the server no longer lets this user read stock', async () => {
    renderAt(PARA_STOCK, PHARMACIST)
    // Taken away by an admin after sign-in: the screen's copy is out of date.
    granted = granted.filter((code) => code !== 'pharmacy.batch.read')

    expect(await screen.findByText("You can't view stock")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/Permission denied/)
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
  })

  it('offers a retry when the stock cannot be loaded, without the server\'s words', async () => {
    intercept = (c) => (c.url?.endsWith('/stock') ? fail(500, 'KeyError: boom') : undefined)
    const user = userEvent.setup()
    renderAt(PARA_STOCK, PHARMACIST)

    expect(await screen.findByText("Couldn't load this medicine's stock")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom|KeyError/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await stockLoaded('Paracetamol')).toBeInTheDocument()
  })
})

// ── Receiving stock ─────────────────────────────────────────────────────────

describe('receiving stock', () => {
  const openForm = async (user: ReturnType<typeof userEvent.setup>, path = PARA_STOCK, name = 'Paracetamol') => {
    renderAt(path, PHARMACIST)
    await stockLoaded(name)
    await user.click(screen.getByRole('button', { name: /Receive stock/ }))
    return dialog()
  }
  const fill = async (user: ReturnType<typeof userEvent.setup>, d: HTMLElement, number: string, expiry: string, quantity: string, cost: string) => {
    await user.type(within(d).getByLabelText(/^Batch number/), number)
    await user.type(within(d).getByLabelText(/^Expiry date/), expiry)
    await user.type(within(d).getByLabelText(/^Quantity/), quantity)
    await user.type(within(d).getByLabelText(/^Cost per unit/), cost)
  }
  const submit = (user: ReturnType<typeof userEvent.setup>, d: HTMLElement) =>
    user.click(within(d).getByRole('button', { name: 'Receive stock' }))

  it('posts a new batch exactly as the API takes it, and reads the stock again', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    expect(within(d).getByRole('heading', { name: 'Receive stock' })).toBeInTheDocument()
    expect(d).toHaveTextContent('Records stock of Paracetamol taken in without a purchase order')
    expect(d).toHaveTextContent('A batch number this medicine already has is topped up rather than added again')
    expect(d).toHaveTextContent('the expiry date must then match the one on record, and the batch keeps its original cost')

    // A date long past on this machine, and months ahead for the hospital.
    await fill(user, d, ' b2024-041 ', daysFromToday(120), '20', '1.2')
    const before = sent('get', '/stock').length
    await submit(user, d)

    // The batch number and the count are the server's.
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Received 20 units into batch B2024-041 — it now holds 20 units'))
    const [request] = sent('post', '/batches')
    expect(request.method).toBe('post')
    expect(address(request)).toBe('/api/v1/medicines/m-PARA-500/batches')
    expect(bodyOf(request)).toEqual({ batch_number: 'B2024-041', expiry_date: daysFromToday(120), quantity: 20, cost_per_unit: '1.2' })
    expect(sent('get', '/stock').length).toBeGreaterThan(before)
    const row = await screen.findByRole('row', { name: /B2024-041/ })
    expect(cellsOf(row).slice(2, 6)).toEqual(['20', '20', '1.20', 'Dispensable'])
    expect(figure('On hand')).toHaveTextContent('550 units')
    expect(figure('Dispensable today')).toHaveTextContent('550 units')
  })

  it('tops up a batch the medicine already has, which keeps its own cost', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'pa-2401', daysFromToday(20), '10', '9.99')
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Received 10 units into batch PA-2401 — it now holds 40 units'))
    expect(bodyOf(sent('post', '/batches')[0])).toEqual({ batch_number: 'PA-2401', expiry_date: daysFromToday(20), quantity: 10, cost_per_unit: '9.99' })
    const row = await screen.findByRole('row', { name: /PA-2401/ })
    await waitFor(() => expect(cellsOf(row).slice(2, 5)).toEqual(['40', '40', '1.10']))
    expect(batches.filter((b) => b.batch_number === 'PA-2401')).toHaveLength(1)
  })

  it('checks the form before asking the server', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)

    await submit(user, d)
    expect(await within(d).findByText('Enter the batch number')).toBeInTheDocument()
    expect(within(d).getByText('Enter the expiry date')).toBeInTheDocument()
    expect(within(d).getByText('Enter how many units')).toBeInTheDocument()
    expect(within(d).getByText('Enter the cost per unit')).toBeInTheDocument()

    await fill(user, d, 'B 1', daysFromToday(120), '0', '1.205')
    await submit(user, d)
    expect(await within(d).findByText('Letters, digits and - _ . / only, starting with a letter or digit')).toBeInTheDocument()
    expect(within(d).getByText('Whole units, 1–1,000,000')).toBeInTheDocument()
    expect(within(d).getByText('Enter an amount such as 1.20')).toBeInTheDocument()
    expect(within(d).queryByText('Enter the expiry date')).not.toBeInTheDocument()

    for (const quantity of ['2.5', '-3', '1000001']) {
      await retype(user, within(d).getByLabelText(/^Quantity/), quantity)
      await submit(user, d)
      expect(await within(d).findByText('Whole units, 1–1,000,000')).toBeInTheDocument()
    }
    expect(sent('post', '/batches')).toHaveLength(0)
  })

  it('leaves it to the server to say a batch has expired, and shows that under the date', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    // Yesterday, by the hospital's date.
    await fill(user, d, 'B-OLD', daysFromToday(-1), '5', '1.00')
    await submit(user, d)

    expect(await within(d).findByText('A batch that has already expired cannot be received.')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Expiry date/)).toBeInvalid()
    expect(sent('post', '/batches')).toHaveLength(1)
    expect(toastError).toHaveBeenCalledWith("Couldn't save. Check the highlighted fields.")
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(batches.some((b) => b.batch_number === 'B-OLD')).toBe(false)

    // Today is still in date: corrected, the same form goes through.
    await retype(user, within(d).getByLabelText(/^Expiry date/), TODAY)
    await submit(user, d)
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Received 5 units into batch B-OLD — it now holds 5 units'))
  })

  it('shows under the date that a known batch is recorded with another expiry', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'PA-2401', daysFromToday(200), '10', '1.10')
    await submit(user, d)

    expect(await within(d).findByText(`Batch PA-2401 is already recorded as expiring ${daysFromToday(20)}.`)).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Expiry date/)).toBeInvalid()
    expect(batches.find((b) => b.batch_number === 'PA-2401')?.quantity_on_hand).toBe(30)
  })

  it('puts a field the request validation rejects under that field', async () => {
    intercept = (c) =>
      c.method === 'post' && c.url?.endsWith('/batches')
        ? invalid('cost_per_unit', 'Decimal input should have no more than 15 digits in total')
        : undefined
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'B-9', daysFromToday(120), '5', '1.00')
    await submit(user, d)

    expect(await within(d).findByText('Decimal input should have no more than 15 digits in total')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Cost per unit/)).toBeInvalid()
  })

  it('shows the API\'s refusal when the medicine was made inactive meanwhile', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    medicines.find((m) => m.sku === 'PARA-500')!.is_active = false
    await fill(user, d, 'B-9', daysFromToday(120), '5', '1.00')
    await submit(user, d)

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Medicine 'PARA-500' is inactive; stock cannot be received for it."))
    expect(toastSuccess).not.toHaveBeenCalled()
    // The screen catches up: the medicine is inactive, and receiving is no longer offered.
    expect(await screen.findByText('This medicine is inactive')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('button', { name: /Receive stock/ })).not.toBeInTheDocument())
    expect(batches.some((b) => b.batch_number === 'B-9')).toBe(false)
  })

  it('says so when the medicine is gone', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'B-9', daysFromToday(120), '5', '1.00')
    medicines = medicines.filter((m) => m.sku !== 'PARA-500')
    await submit(user, d)

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Medicine not found.'))
    expect(await screen.findByText('Medicine not found')).toBeInTheDocument()
  })

  it('keeps the server\'s words out of sight when the server fails', async () => {
    intercept = (c) => (c.method === 'post' && c.url?.endsWith('/batches') ? fail(500, 'UniqueViolation: boom') : undefined)
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'B-9', daysFromToday(120), '5', '1.00')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent("Couldn't receive the stock. Please try again.")
    expect(document.body.textContent).not.toMatch(/boom|UniqueViolation/)
  })

  it('shows the API\'s refusal when the permission was taken away meanwhile', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    granted = granted.filter((code) => code !== 'pharmacy.batch.create')
    await fill(user, d, 'B-9', daysFromToday(120), '5', '1.00')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('Permission denied. Required: pharmacy.batch.create.')
    expect(toastError).toHaveBeenCalledWith('Permission denied. Required: pharmacy.batch.create.')
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(batches.some((b) => b.batch_number === 'B-9')).toBe(false)
  })

  it('receives once however many times the form is submitted', async () => {
    let release: () => void = () => {}
    intercept = (c) =>
      c.method === 'post' && c.url?.endsWith('/batches')
        ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c))))
        : undefined
    const user = userEvent.setup()
    const d = await openForm(user)
    await fill(user, d, 'PA-2401', daysFromToday(20), '10', '1.10')
    const form = within(d).getByRole('button', { name: 'Receive stock' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Receiving…' })).toBeDisabled()
    await waitFor(() => expect(sent('post', '/batches')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/batches')).toHaveLength(1)
    // A second receipt would have topped the batch up twice.
    expect(batches.find((b) => b.batch_number === 'PA-2401')?.quantity_on_hand).toBe(40)
  })
})

// ── Adjusting a batch ───────────────────────────────────────────────────────

describe('adjusting a batch', () => {
  const openForm = async (user: ReturnType<typeof userEvent.setup>, permissions = PHARMACIST) => {
    renderAt(PARA_STOCK, permissions)
    await stockLoaded('Paracetamol')
    await user.click(screen.getByRole('button', { name: 'Adjust batch PA-2401' }))
    return dialog()
  }
  const submit = (user: ReturnType<typeof userEvent.setup>, d: HTMLElement) =>
    user.click(within(d).getByRole('button', { name: 'Save adjustment' }))
  const batchRow = () => screen.findByRole('row', { name: /PA-2401/ })

  it('removes units as a negative change, with the reason and the note', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    expect(within(d).getByText('PA-2401')).toBeInTheDocument()
    expect(within(d).getByText('30 units')).toBeInTheDocument()
    expect(within(d).getByText(shownDate(daysFromToday(20)))).toBeInTheDocument()
    expect(within(d).getByRole('combobox', { name: /^Change/ })).toHaveTextContent('Remove units')

    await user.type(within(d).getByLabelText(/^Units to remove/), '4')
    await choose(user, within(d).getByRole('combobox', { name: /^Reason/ }), 'Expired stock written off')
    await user.type(within(d).getByLabelText(/^Note/), ' Damaged strip ')
    const before = sent('get', '/stock').length
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Batch PA-2401 now holds 26 units'))
    const [request] = sent('post', '/adjust')
    expect(request.method).toBe('post')
    expect(address(request)).toBe('/api/v1/medicines/m-PARA-500/batches/b-PA-2401/adjust')
    expect(bodyOf(request)).toEqual({ quantity_change: -4, reason: 'expired', note: 'Damaged strip' })
    expect(sent('get', '/stock').length).toBeGreaterThan(before)
    // On hand moves; what was received in total does not.
    await waitFor(async () => expect(cellsOf(await batchRow()).slice(2, 4)).toEqual(['26', '30']))
    expect(figure('On hand')).toHaveTextContent('526 units')
  })

  it('adds units as a positive change, which can pass what was received', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)

    await choose(user, within(d).getByRole('combobox', { name: /^Change/ }), 'Add units')
    await user.type(within(d).getByLabelText(/^Units to add/), '6')
    await user.type(within(d).getByLabelText(/^Note/), 'Recount found six more')
    await submit(user, d)

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Batch PA-2401 now holds 36 units'))
    expect(bodyOf(sent('post', '/adjust')[0])).toEqual({ quantity_change: 6, reason: 'adjusted', note: 'Recount found six more' })
    await waitFor(async () => expect(cellsOf(await batchRow()).slice(2, 4)).toEqual(['36', '30']))
  })

  it('does not let expired stock be written off by adding units', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    const reason = () => within(d).getByRole('combobox', { name: /^Reason/ })
    await choose(user, reason(), 'Expired stock written off')
    expect(reason()).toHaveTextContent('Expired stock written off')

    // Switching to an addition takes the reason back to the one it can carry.
    await choose(user, within(d).getByRole('combobox', { name: /^Change/ }), 'Add units')
    expect(reason()).toHaveTextContent('Count correction')
    expect(within(d).getByText('Expired stock can only be removed, so it is not offered when adding')).toBeInTheDocument()
    await user.click(reason())
    expect(await screen.findByRole('option', { name: 'Expired stock written off' })).toHaveAttribute('aria-disabled', 'true')
    await user.click(screen.getByRole('option', { name: 'Count correction' }))

    await user.type(within(d).getByLabelText(/^Units to add/), '2')
    await user.type(within(d).getByLabelText(/^Note/), 'Found behind the shelf')
    await submit(user, d)

    await waitFor(() => expect(sent('post', '/adjust')).toHaveLength(1))
    expect(bodyOf(sent('post', '/adjust')[0])).toEqual({ quantity_change: 2, reason: 'adjusted', note: 'Found behind the shelf' })
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Batch PA-2401 now holds 32 units'))
  })

  it('needs a count that is not zero, and a note', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)

    await submit(user, d)
    expect(await within(d).findByText('Enter how many units')).toBeInTheDocument()
    expect(within(d).getByText('Say why the count is being corrected')).toBeInTheDocument()

    for (const count of ['0', '-4', '1.5', '1000001']) {
      await retype(user, within(d).getByLabelText(/^Units to remove/), count)
      await user.type(within(d).getByLabelText(/^Note/), '   ')
      await submit(user, d)
      expect(await within(d).findByText('Whole units, 1–1,000,000')).toBeInTheDocument()
      expect(within(d).getByText('Say why the count is being corrected')).toBeInTheDocument()
    }
    expect(within(d).getByLabelText(/^Note/)).toBeInvalid()
    expect(sent('post', '/adjust')).toHaveLength(0)
  })

  it('lets the server refuse a removal the batch cannot cover, and shows what it holds', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    expect(within(d).getByText('30 units')).toBeInTheDocument()
    // Twenty were dispensed at another counter since this screen read the stock.
    batches.find((b) => b.batch_number === 'PA-2401')!.quantity_on_hand = 10

    await user.type(within(d).getByLabelText(/^Units to remove/), '15')
    await user.type(within(d).getByLabelText(/^Note/), 'Stock take')
    await submit(user, d)

    // Not blocked here — the count on screen was allowed 15 — and refused there.
    expect(await within(d).findByRole('alert')).toHaveTextContent('Batch PA-2401 holds 10 units; 15 cannot be removed.')
    expect(bodyOf(sent('post', '/adjust')[0])).toEqual({ quantity_change: -15, reason: 'adjusted', note: 'Stock take' })
    expect(toastError).toHaveBeenCalledWith('Batch PA-2401 holds 10 units; 15 cannot be removed.')
    expect(toastSuccess).not.toHaveBeenCalled()
    // The stock is read again, and the open form shows the real count.
    expect(await within(d).findByText('10 units')).toBeInTheDocument()
    expect(batches.find((b) => b.batch_number === 'PA-2401')?.quantity_on_hand).toBe(10)

    await retype(user, within(d).getByLabelText(/^Units to remove/), '10')
    await submit(user, d)
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Batch PA-2401 now holds 0 units'))
    expect(within(await batchRow()).getByText('Empty')).toBeInTheDocument()
  })

  it('keeps the form and the server\'s message when the re-read after a refusal fails', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    expect(within(d).getByRole('heading', { name: 'Adjust this batch' })).toBeInTheDocument()
    await user.type(within(d).getByLabelText(/^Units to remove/), '31')
    await user.type(within(d).getByLabelText(/^Note/), 'Damaged strip')
    // The 400 has the stock read again; that reading fails.
    intercept = (c) => (c.url?.endsWith('/stock') ? fail(500, 'KeyError: boom') : undefined)
    const before = sent('get', '/stock').length
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('Batch PA-2401 holds 30 units; 31 cannot be removed.')
    await waitFor(() => expect(sent('get', '/stock').length).toBeGreaterThan(before))
    await screen.findByText('These figures may be out of date')
    // Nothing typed is lost, and the refusal is still in view.
    expect(screen.getByRole('dialog')).toBe(d)
    expect(within(d).getByLabelText(/^Note/)).toHaveValue('Damaged strip')
    expect(within(d).getByLabelText(/^Units to remove/)).toHaveValue('31')
    expect(d).toHaveTextContent('Batch PA-2401 holds 30 units; 31 cannot be removed.')
    expect(document.body.textContent).not.toMatch(/boom|KeyError/)

    // Corrected, the same form goes through.
    intercept = () => undefined
    await retype(user, within(d).getByLabelText(/^Units to remove/), '30')
    await submit(user, d)
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Batch PA-2401 now holds 0 units'))
    await waitFor(() => expect(screen.queryByText('These figures may be out of date')).not.toBeInTheDocument())
  })

  it('does not stop a removal larger than the count it shows', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await user.type(within(d).getByLabelText(/^Units to remove/), '31')
    await user.type(within(d).getByLabelText(/^Note/), 'Stock take')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('Batch PA-2401 holds 30 units; 31 cannot be removed.')
    expect(sent('post', '/adjust')).toHaveLength(1)
  })

  it('shows a whole-form rule the server enforces above the form', async () => {
    intercept = (c) =>
      c.url?.endsWith('/adjust') ? invalid('', 'Value error, Writing off expired stock must remove units.') : undefined
    const user = userEvent.setup()
    const d = await openForm(user)
    await user.type(within(d).getByLabelText(/^Units to remove/), '1')
    await user.type(within(d).getByLabelText(/^Note/), 'Stock take')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('Writing off expired stock must remove units.')
    expect(d).not.toHaveTextContent('Value error')
    expect(toastError).toHaveBeenCalledWith('Writing off expired stock must remove units.')
  })

  it('puts a field the server rejects under that field', async () => {
    intercept = (c) => (c.url?.endsWith('/adjust') ? invalid('note', 'Value error, Note must not be blank.') : undefined)
    const user = userEvent.setup()
    const d = await openForm(user)
    await user.type(within(d).getByLabelText(/^Units to remove/), '1')
    await user.type(within(d).getByLabelText(/^Note/), 'x')
    await submit(user, d)

    expect(await within(d).findByText('Note must not be blank.')).toBeInTheDocument()
    expect(within(d).getByLabelText(/^Note/)).toBeInvalid()
  })

  it('shows the API\'s refusal when the permission was taken away meanwhile', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    granted = granted.filter((code) => code !== 'pharmacy.batch.update')
    await user.type(within(d).getByLabelText(/^Units to remove/), '1')
    await user.type(within(d).getByLabelText(/^Note/), 'Stock take')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent('Permission denied. Required: pharmacy.batch.update.')
    expect(batches.find((b) => b.batch_number === 'PA-2401')?.quantity_on_hand).toBe(30)
  })

  it('says so when the batch is gone, and the list catches up', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await user.type(within(d).getByLabelText(/^Units to remove/), '1')
    await user.type(within(d).getByLabelText(/^Note/), 'Stock take')
    batches = batches.filter((b) => b.batch_number !== 'PA-2401')
    await submit(user, d)

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Batch not found.'))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByRole('row', { name: /PA-2401/ })).not.toBeInTheDocument()
    expect(screen.getByRole('row', { name: /PA-2502/ })).toBeInTheDocument()
  })

  it('closes an open adjustment rather than carry it onto the batch that takes its row', async () => {
    const user = userEvent.setup()
    const d = await openForm(user)
    await user.type(within(d).getByLabelText(/^Units to remove/), '4')
    await user.type(within(d).getByLabelText(/^Note/), 'Damaged strip')
    // Received at another counter, and first to expire: every row moves down one.
    batches.push(batch('PARA-500', 'PA-0001', 5, 60))
    await refetchEverything()

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByRole('row', { name: /PA-0001/ })).toBeInTheDocument()
    expect(sent('post', '/adjust')).toHaveLength(0)
    expect(batches.find((b) => b.batch_number === 'PA-0001')?.quantity_on_hand).toBe(60)
  })

  it('keeps the server\'s words out of sight when the server fails', async () => {
    intercept = (c) => (c.url?.endsWith('/adjust') ? fail(500, 'DeadlockDetected: boom') : undefined)
    const user = userEvent.setup()
    const d = await openForm(user)
    await user.type(within(d).getByLabelText(/^Units to remove/), '1')
    await user.type(within(d).getByLabelText(/^Note/), 'Stock take')
    await submit(user, d)

    expect(await within(d).findByRole('alert')).toHaveTextContent("Couldn't adjust the stock. Please try again.")
    expect(document.body.textContent).not.toMatch(/boom|Deadlock/)
  })

  it('adjusts once however many times the form is submitted', async () => {
    let release: () => void = () => {}
    intercept = (c) =>
      c.url?.endsWith('/adjust') ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c)))) : undefined
    const user = userEvent.setup()
    const d = await openForm(user)
    await user.type(within(d).getByLabelText(/^Units to remove/), '4')
    await user.type(within(d).getByLabelText(/^Note/), 'Damaged strip')
    const form = within(d).getByRole('button', { name: 'Save adjustment' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Saving…' })).toBeDisabled()
    await waitFor(() => expect(sent('post', '/adjust')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/adjust')).toHaveLength(1)
    // A second adjustment would have taken four more.
    expect(batches.find((b) => b.batch_number === 'PA-2401')?.quantity_on_hand).toBe(26)
  })
})

// ── Recalling a batch ───────────────────────────────────────────────────────

describe('recalling a batch', () => {
  it('recalls only after confirming, and the units stop being dispensable', async () => {
    const user = userEvent.setup()
    renderAt(PARA_STOCK, PHARMACIST)
    await stockLoaded('Paracetamol')
    expect(figure('Dispensable today')).toHaveTextContent('530 units')

    await user.click(screen.getByRole('button', { name: 'Recall batch PA-2401' }))
    const d = await dialog()
    expect(within(d).getByRole('heading', { name: 'Recall this batch?' })).toBeInTheDocument()
    expect(d).toHaveTextContent('Batch PA-2401 of Paracetamol holds 30 units. Once it is recalled they can no longer be dispensed.')
    expect(d).toHaveTextContent('the recall can be lifted later')
    expect(sent('patch', '/batches')).toHaveLength(0)
    const before = sent('get', '/stock').length
    await user.click(within(d).getByRole('button', { name: 'Recall batch' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Batch PA-2401 recalled'))
    const [request] = sent('patch', '/batches')
    expect(request.method).toBe('patch')
    expect(address(request)).toBe('/api/v1/medicines/m-PARA-500/batches/b-PA-2401')
    expect(bodyOf(request)).toEqual({ is_recalled: true })
    expect(sent('get', '/stock').length).toBeGreaterThan(before)

    const row = await screen.findByRole('row', { name: /PA-2401/ })
    await waitFor(() => expect(within(row).getByText('Recalled')).toBeInTheDocument())
    // Nothing left the shelf: on hand is unchanged, and the units moved between the server's counts.
    expect(cellsOf(row)[2]).toBe('30')
    expect(figure('On hand')).toHaveTextContent('530 units')
    expect(figure('Dispensable today')).toHaveTextContent('500 units')
    expect(figure('Recalled')).toHaveTextContent('30 units')
    expect(within(row).getByRole('button', { name: 'Lift recall on batch PA-2401' })).toBeInTheDocument()
  })

  it('does nothing when the recall is not confirmed', async () => {
    const user = userEvent.setup()
    renderAt(PARA_STOCK, PHARMACIST)
    await stockLoaded('Paracetamol')

    await user.click(screen.getByRole('button', { name: 'Recall batch PA-2401' }))
    await user.click(within(await dialog()).getByRole('button', { name: 'Not now' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(sent('patch', '/batches')).toHaveLength(0)
    expect(batches.find((b) => b.batch_number === 'PA-2401')?.is_recalled).toBe(false)
  })

  it('lifts a recall, and the units are dispensable again', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines/m-PANT-40', PHARMACIST)
    await stockLoaded('Pantoprazole')

    await user.click(screen.getByRole('button', { name: 'Lift recall on batch PN-2507' }))
    const d = await dialog()
    expect(within(d).getByRole('heading', { name: 'Lift the recall on this batch?' })).toBeInTheDocument()
    expect(d).toHaveTextContent('Batch PN-2507 of Pantoprazole holds 120 units. Once the recall is lifted they can be dispensed again.')
    await user.click(within(d).getByRole('button', { name: 'Lift recall' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Recall lifted — batch PN-2507 can be dispensed again'))
    const [request] = sent('patch', '/batches')
    expect(request.method).toBe('patch')
    expect(address(request)).toBe('/api/v1/medicines/m-PANT-40/batches/b-PN-2507')
    expect(bodyOf(request)).toEqual({ is_recalled: false })
    const row = await screen.findByRole('row', { name: /PN-2507/ })
    await waitFor(() => expect(within(row).getByText('Dispensable')).toBeInTheDocument())
    expect(figure('Dispensable today')).toHaveTextContent('120 units')
    expect(figure('Recalled')).toHaveTextContent('0 units')
    expect(within(row).getByRole('button', { name: 'Recall batch PN-2507' })).toBeInTheDocument()
  })

  it('does not promise that an expired batch can be dispensed once its recall is lifted', async () => {
    batches.find((b) => b.batch_number === 'AX-2311')!.is_recalled = true
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines/m-AMOX-500', PHARMACIST)
    await stockLoaded('Amoxil')

    await user.click(screen.getByRole('button', { name: 'Lift recall on batch AX-2311' }))
    const d = await dialog()
    expect(d).toHaveTextContent('It has expired, so its units still cannot be dispensed once the recall is lifted.')
    await user.click(within(d).getByRole('button', { name: 'Lift recall' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Recall lifted on batch AX-2311'))
    expect(within(await screen.findByRole('row', { name: /AX-2311/ })).getByText('Expired')).toBeInTheDocument()
  })

  it('shows the API\'s refusal when the permission was taken away meanwhile', async () => {
    const user = userEvent.setup()
    renderAt(PARA_STOCK, PHARMACIST)
    await stockLoaded('Paracetamol')
    await user.click(screen.getByRole('button', { name: 'Recall batch PA-2401' }))
    const d = await dialog()
    granted = granted.filter((code) => code !== 'pharmacy.batch.update')
    await user.click(within(d).getByRole('button', { name: 'Recall batch' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Permission denied. Required: pharmacy.batch.update.')
    expect(batches.find((b) => b.batch_number === 'PA-2401')?.is_recalled).toBe(false)
  })

  it('says so when the batch to recall is gone, and the list catches up', async () => {
    const user = userEvent.setup()
    renderAt(PARA_STOCK, PHARMACIST)
    await stockLoaded('Paracetamol')
    await user.click(screen.getByRole('button', { name: 'Recall batch PA-2401' }))
    const d = await dialog()
    batches = batches.filter((b) => b.batch_number !== 'PA-2401')
    await user.click(within(d).getByRole('button', { name: 'Recall batch' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Batch not found.'))
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(address(sent('patch', '/batches')[0])).toBe('/api/v1/medicines/m-PARA-500/batches/b-PA-2401')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByRole('row', { name: /PA-2401/ })).not.toBeInTheDocument()
    expect(screen.getByRole('row', { name: /PA-2502/ })).toBeInTheDocument()
  })

  it('says so when the batch whose recall is being lifted is gone', async () => {
    const user = userEvent.setup()
    renderAt('/pharmacy/medicines/m-PANT-40', PHARMACIST)
    await stockLoaded('Pantoprazole')
    await user.click(screen.getByRole('button', { name: 'Lift recall on batch PN-2507' }))
    const d = await dialog()
    batches = batches.filter((b) => b.batch_number !== 'PN-2507')
    await user.click(within(d).getByRole('button', { name: 'Lift recall' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Batch not found.'))
    expect(bodyOf(sent('patch', '/batches')[0])).toEqual({ is_recalled: false })
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(await screen.findByText('No batches yet')).toBeInTheDocument()
  })

  it.each([
    ['recalling', '/pharmacy/medicines/m-PARA-500', 'Paracetamol', 'Recall batch PA-2401', 'Recall batch', "Couldn't recall the batch. Please try again."],
    ['lifting a recall', '/pharmacy/medicines/m-PANT-40', 'Pantoprazole', 'Lift recall on batch PN-2507', 'Lift recall', "Couldn't lift the recall. Please try again."],
  ])('keeps the server\'s words out of sight when %s fails', async (_what, path, name, open, confirm, message) => {
    intercept = (c) => (c.method === 'patch' ? fail(500, 'OperationalError: boom') : undefined)
    const user = userEvent.setup()
    renderAt(path, PHARMACIST)
    await stockLoaded(name)
    const recalled = batches.map((b) => b.is_recalled)
    await user.click(screen.getByRole('button', { name: open }))
    const d = await dialog()
    await user.click(within(d).getByRole('button', { name: confirm }))

    expect(await within(d).findByRole('alert')).toHaveTextContent(message)
    expect(document.body.textContent).not.toMatch(/boom|OperationalError/)
    expect(toastError).toHaveBeenCalledWith(message)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(batches.map((b) => b.is_recalled)).toEqual(recalled)
  })

  it('recalls once however many times it is confirmed', async () => {
    let release: () => void = () => {}
    intercept = (c) =>
      c.method === 'patch' ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c)))) : undefined
    const user = userEvent.setup()
    renderAt(PARA_STOCK, PHARMACIST)
    await stockLoaded('Paracetamol')
    await user.click(screen.getByRole('button', { name: 'Recall batch PA-2401' }))
    const d = await dialog()
    const form = within(d).getByRole('button', { name: 'Recall batch' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Recalling…' })).toBeDisabled()
    await waitFor(() => expect(sent('patch', '/batches')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('patch', '/batches')).toHaveLength(1)
  })
})

// ── The rules the forms carry ───────────────────────────────────────────────

describe('the pharmacy form rules', () => {
  const valid: MedicineValues = {
    sku: 'PARA-500',
    name: 'Paracetamol',
    generic_name: '',
    strength: '',
    form: '',
    atc_code: '',
    unit_price: '2.50',
    requires_prescription: true,
    is_active: true,
  }

  it.each([
    ['A', true],
    ['a.b/c_d-1', true],
    ['x'.repeat(50), true],
    ['x'.repeat(51), false],
    ['-ABC', false],
    ['AB C', false],
    ['ÄB-1', false],
  ])('takes %s as a SKU: %s', (sku, accepted) => {
    expect(createMedicineSchema.safeParse({ ...valid, sku }).success).toBe(accepted)
  })

  it.each([
    ['0', true],
    ['2.5', true],
    ['2.50', true],
    ['2.505', false],
    ['-1', false],
    ['1,000', false],
    ['', false],
  ])('takes %s as a price: %s', (unit_price, accepted) => {
    expect(createMedicineSchema.safeParse({ ...valid, unit_price }).success).toBe(accepted)
  })

  it('never lets the SKU it cannot send block an edit', () => {
    expect(editMedicineSchema.safeParse({ ...valid, sku: 'legacy sku' }).success).toBe(true)
    expect(toUpdateBody(valid, { ...valid, sku: 'CHANGED', atc_code: 'N02BE01' })).toEqual({ atc_code: 'N02BE01' })
    expect(toUpdateBody(valid, valid)).toEqual({})
  })

  it('refuses expired stock with an addition, and signs the change by its direction', () => {
    const adjustment = { direction: 'add', quantity_change: '3', reason: 'expired', note: 'x' } as const
    const refusal = adjustSchema.safeParse(adjustment)
    expect(refusal.success).toBe(false)
    expect(refusal.error?.issues.map((issue) => issue.path.join('.'))).toEqual(['reason'])
    expect(adjustSchema.safeParse({ ...adjustment, direction: 'remove' }).success).toBe(true)
    expect(toAdjustBody({ ...adjustment, direction: 'remove' })).toEqual({ quantity_change: -3, reason: 'expired', note: 'x' })
    expect(toAdjustBody({ ...adjustment, reason: 'adjusted' })).toEqual({ quantity_change: 3, reason: 'adjusted', note: 'x' })
  })

  it('takes any well-formed expiry date, past or future, and leaves the judging to the server', () => {
    const receipt = { batch_number: 'B-1', quantity: '1', cost_per_unit: '0' }
    expect(receiveSchema.safeParse({ ...receipt, expiry_date: '1999-01-01' }).success).toBe(true)
    expect(receiveSchema.safeParse({ ...receipt, expiry_date: '2999-12-31' }).success).toBe(true)
    expect(receiveSchema.safeParse({ ...receipt, expiry_date: '' }).success).toBe(false)
  })

  it.each([
    [0, 'expires today'],
    [1, 'expires in 1 day'],
    [20, 'expires in 20 days'],
    [-1, 'expired 1 day ago'],
    [-5, 'expired 5 days ago'],
  ])('words %i days to expiry as "%s"', (days, words) => {
    expect(expiryInWords(days)).toBe(words)
  })
})
