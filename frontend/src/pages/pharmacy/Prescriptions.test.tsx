import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { InvoiceSummary } from '@/api/billing'
import type { Dispense, DispenseItem, Medicine, Prescription, PrescriptionItem, PrescriptionStatus } from '@/api/pharmacy'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { PatientInvoices } from '@/components/billing/PatientInvoices'
import { PatientPrescriptions } from '@/components/pharmacy/PatientPrescriptions'
import { PrescribeDialog } from '@/components/pharmacy/PrescribeDialog'
import { formatDate, formatDateTime } from '@/lib/format'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import PrescriptionDetailPage from './PrescriptionDetailPage'
import PrescriptionsPage from './PrescriptionsPage'

/**
 * Prescribing and dispensing, against the merged backend contract
 * (docs/18-API_CONTRACTS.md §9.4–9.6; `backend/app/api/v1/prescriptions.py`,
 * `backend/app/services/dispensing_service.py`). The real hooks, permission
 * check, `http` wrapper and Axios instance run; only the network adapter is
 * replaced by an in-memory "server" that keeps prescriptions, batches and
 * invoices and — like the real one — is the only thing that picks batches,
 * prices a dispense, counts what is available and writes a warning.
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
const NURSE = ['notification.read.own', 'patient.read', 'appointment.read', 'appointment.check_in', 'lab.order.read']
const RECEPTIONIST = ['notification.read.own', 'patient.read', 'appointment.read', 'appointment.book', 'invoice.read']

// ── The in-memory server ────────────────────────────────────────────────────

interface Batch {
  id: string
  medicine_id: string
  batch_number: string
  expiry_date: string
  quantity_on_hand: number
  is_recalled: boolean
}

interface Visit {
  id: string
  patient_id: string
  patient_name: string
  patient_mrn: string
  doctor_id: string
  doctor_name: string
  status: string
}

/** A line as stored. What is left and what is available are worked out on every read. */
type StoredItem = Omit<PrescriptionItem, 'quantity_remaining' | 'available_quantity'>
type Stored = Omit<Prescription, 'items'> & { items: StoredItem[] }
/** A dispense as stored. Its warnings are worked out on every read, against today. */
type StoredDispense = Omit<Dispense, 'warnings'>

function medicine(sku: string, name: string, strength: string, unit_price: string, extra: Partial<Medicine> = {}): Medicine {
  return {
    id: `m-${sku}`,
    sku,
    name,
    generic_name: null,
    strength,
    form: 'tablet',
    atc_code: null,
    unit_price,
    requires_prescription: true,
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...extra,
  }
}

const PARA = medicine('PARA-500', 'Paracetamol', '500 mg', '2.50')
const AMOX = medicine('AMOX-500', 'Amoxicillin', '500 mg', '8.00', { form: 'capsule' })
const ATOR = medicine('ATOR-10', 'Atorvastatin', '10 mg', '6.50')
const RANI = medicine('RANI-150', 'Ranitidine (withdrawn)', '150 mg', '2.00', { is_active: false })

const VISIT: Visit = {
  id: 'a1',
  patient_id: 'p1',
  patient_name: 'Ananya Rao',
  patient_mrn: 'MRN-2026-00007',
  doctor_id: 'd1',
  doctor_name: 'Dr. Priya Sharma',
  status: 'in_progress',
}
const OTHER_VISIT: Visit = { ...VISIT, id: 'a2', patient_id: 'p2', patient_name: 'Ravi Menon', patient_mrn: 'MRN-2026-00002', status: 'completed' }

/** A stored line: a catalog medicine (named with its strength, as the server snapshots it) or a typed name. */
function line(id: string, what: Medicine | string, quantity: number, extra: Partial<StoredItem> = {}): StoredItem {
  const stocked = typeof what !== 'string'
  return {
    id,
    medicine_id: stocked ? what.id : null,
    medicine_name: stocked ? `${what.name} ${what.strength}` : what,
    dosage: '1 tablet',
    frequency: 'three times daily',
    duration_days: 5,
    instructions: null,
    quantity,
    quantity_dispensed: 0,
    ...extra,
  }
}

function rx(id: string, items: StoredItem[], extra: Partial<Stored> = {}): Stored {
  return {
    id,
    appointment_id: VISIT.id,
    patient_id: VISIT.patient_id,
    patient_name: VISIT.patient_name,
    patient_mrn: VISIT.patient_mrn,
    doctor_id: VISIT.doctor_id,
    doctor_name: VISIT.doctor_name,
    status: 'active',
    notes: null,
    prescribed_at: '2026-10-05T09:00:00Z',
    cancelled_at: null,
    cancel_reason: null,
    items,
    ...extra,
  }
}

const FOR_RAVI = { appointment_id: 'a2', patient_id: 'p2', patient_name: 'Ravi Menon', patient_mrn: 'MRN-2026-00002' }

let medicines: Medicine[]
let batches: Batch[]
let visits: Visit[]
let prescriptions: Stored[]
let dispenses: StoredDispense[]
let invoices: InvoiceSummary[]
/** The hospital's date, as the server sees it. */
let today: string
/** Return an outcome to answer a request yourself; nothing to let the server answer. */
let intercept: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome> | undefined
let fake: FakeApi

const WAITING: ReadonlySet<PrescriptionStatus> = new Set(['active', 'partially_dispensed'])
const cents = (amount: string) => Math.round(Number(amount) * 100)
const money = (c: number) => (c / 100).toFixed(2)
const dispensable = (b: Batch) => b.quantity_on_hand > 0 && !b.is_recalled && b.expiry_date >= today
const batchesOf = (medicineId: string) =>
  batches.filter((b) => b.medicine_id === medicineId && dispensable(b)).sort((a, b) => a.expiry_date.localeCompare(b.expiry_date))
const availableOf = (medicineId: string) => batchesOf(medicineId).reduce((n, b) => n + b.quantity_on_hand, 0)
const remainingOf = (i: StoredItem) => i.quantity - i.quantity_dispensed
const daysTo = (date: string) => Math.round((Date.parse(date) - Date.parse(today)) / 86_400_000)

function view(p: Stored): Prescription {
  return structuredClone({
    ...p,
    items: p.items.map((i) => ({
      ...i,
      quantity_remaining: remainingOf(i),
      available_quantity: i.medicine_id ? availableOf(i.medicine_id) : null,
    })),
  })
}

/** Recomputed against today's date on every read, exactly as the API does. */
function warningsOf(d: StoredDispense): string[] {
  const lines = d.items
    .filter((i) => daysTo(i.expiry_date) <= 30)
    .map((i) => {
      const days = daysTo(i.expiry_date)
      const when = days === 0 ? 'expires today' : `expires in ${days} days`
      return `${i.medicine_name} batch ${i.batch_number} ${when} (${i.expiry_date}).`
    })
  return [...new Set(lines)]
}

const dispenseView = (d: StoredDispense): Dispense => structuredClone({ ...d, warnings: warningsOf(d) })

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

/** A 422 from request validation: `errors` is a list, the message is generic. */
const invalid = (...errors: { field: string; message: string }[]) =>
  fail(422, 'Validation failed.', { error_code: 'VALIDATION_ERROR', errors })
/** A 422 from a rule the service enforces: `errors` is an object, the message is the reason. */
const refused = (field: string, message: string) =>
  fail(422, message, { error_code: 'VALIDATION_ERROR', errors: { errors: [{ field, message }] } })
const brokenRule = (message: string, errors: unknown = null) =>
  fail(400, message, { error_code: 'BUSINESS_RULE_VIOLATION', errors })
const notFound = (id: string) =>
  fail(404, 'Prescription not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { prescription_id: id } })

/** The request models forbid extra keys: a patient, a batch or a price sent by the client is a 422. */
function extraKey(value: Record<string, unknown>, allowed: string[], at = ''): Outcome | undefined {
  const key = Object.keys(value).find((k) => !allowed.includes(k))
  return key ? invalid({ field: `${at}${key}`, message: 'Extra inputs are not permitted' }) : undefined
}

const isWhole = (v: unknown, max: number): v is number => typeof v === 'number' && Number.isInteger(v) && v > 0 && v <= max
const isText = (v: unknown, max: number): v is string => typeof v === 'string' && v.trim().length > 0 && v.length <= max

interface LineBody {
  medicine_id?: string
  medicine_name?: string
  dosage: string
  frequency: string
  duration_days?: number
  instructions?: string
  quantity: number
}

function writePrescription(config: InternalAxiosRequestConfig): Outcome {
  const body = bodyOf(config) as { appointment_id: string; notes?: string; items: LineBody[] }
  const extra = extraKey(body as unknown as Record<string, unknown>, ['appointment_id', 'notes', 'items'])
  if (extra) return extra
  if (!Array.isArray(body.items) || body.items.length < 1 || body.items.length > 50) {
    return invalid({ field: 'items', message: 'List should have at least 1 item after validation, not 0' })
  }
  for (const [i, item] of body.items.entries()) {
    const at = `items.${i}`
    const unknownKey = extraKey(
      item as unknown as Record<string, unknown>,
      ['medicine_id', 'medicine_name', 'dosage', 'frequency', 'duration_days', 'instructions', 'quantity'],
      `${at}.`,
    )
    if (unknownKey) return unknownKey
    if (!isText(item.dosage, 100)) return invalid({ field: `${at}.dosage`, message: 'String should have at least 1 character' })
    if (!isText(item.frequency, 100)) return invalid({ field: `${at}.frequency`, message: 'String should have at least 1 character' })
    if (!isWhole(item.quantity, 1_000_000)) return invalid({ field: `${at}.quantity`, message: 'Input should be a valid integer' })
    if (item.duration_days !== undefined && !isWhole(item.duration_days, 3650)) {
      return invalid({ field: `${at}.duration_days`, message: 'Input should be a valid integer' })
    }
    const named = !!item.medicine_name?.trim()
    if (named === !!item.medicine_id) {
      return invalid({ field: at, message: 'Value error, Give either medicine_id or medicine_name, not both and not neither.' })
    }
  }
  const ids = body.items.flatMap((item) => (item.medicine_id ? [item.medicine_id] : []))
  if (new Set(ids).size !== ids.length) {
    return invalid({ field: 'items', message: 'Value error, Each medicine may appear only once in a prescription.' })
  }

  const visit = visits.find((v) => v.id === body.appointment_id)
  if (!visit) return refused('appointment_id', 'Appointment not found in this hospital.')
  if (visit.status === 'cancelled' || visit.status === 'no_show') {
    return refused('appointment_id', `A prescription cannot be written for a visit that is ${visit.status}.`)
  }
  for (const [i, item] of body.items.entries()) {
    if (!item.medicine_id) continue
    const found = medicines.find((m) => m.id === item.medicine_id)
    if (!found) return refused(`items.${i}.medicine_id`, 'Medicine not found in this hospital.')
    if (!found.is_active) return refused(`items.${i}.medicine_id`, `Medicine '${found.sku}' is inactive and cannot be prescribed.`)
  }

  const id = `rx${prescriptions.length + 1}`
  const created = rx(
    id,
    body.items.map((item, i) => {
      const found = medicines.find((m) => m.id === item.medicine_id)
      return line(`${id}-l${i + 1}`, found ?? item.medicine_name!.trim(), item.quantity, {
        dosage: item.dosage.trim(),
        frequency: item.frequency.trim(),
        duration_days: item.duration_days ?? null,
        instructions: item.instructions?.trim() || null,
      })
    }),
    {
      appointment_id: visit.id,
      patient_id: visit.patient_id,
      patient_name: visit.patient_name,
      patient_mrn: visit.patient_mrn,
      doctor_id: visit.doctor_id,
      doctor_name: visit.doctor_name,
      notes: body.notes?.trim() || null,
      prescribed_at: '2026-10-05T09:30:00Z',
    },
  )
  prescriptions.push(created)
  return { status: 201, data: { success: true, message: 'Prescription written.', data: view(created), metadata: null } }
}

function dispensePrescription(p: Stored, config: InternalAxiosRequestConfig): Outcome {
  if (!WAITING.has(p.status)) {
    return brokenRule(`Cannot dispense a prescription that is ${p.status.replace('_', ' ')}.`, { status: p.status })
  }
  const body = (config.data === undefined ? null : bodyOf(config)) as
    | { items?: { prescription_item_id: string; quantity: number }[]; notes?: string }
    | null
  if (body) {
    const extra = extraKey(body as Record<string, unknown>, ['items', 'notes'])
    if (extra) return extra
    if (body.items !== undefined) {
      if (!Array.isArray(body.items) || body.items.length === 0) {
        return invalid({ field: 'items', message: 'List should have at least 1 item after validation, not 0' })
      }
      for (const [i, item] of body.items.entries()) {
        // A batch or a price from the client is refused: the server picks both.
        const unknownKey = extraKey(item as unknown as Record<string, unknown>, ['prescription_item_id', 'quantity'], `items.${i}.`)
        if (unknownKey) return unknownKey
        if (!isWhole(item.quantity, 1_000_000)) return invalid({ field: `items.${i}.quantity`, message: 'Input should be greater than 0' })
      }
      const ids = body.items.map((item) => item.prescription_item_id)
      if (new Set(ids).size !== ids.length) {
        return invalid({ field: 'items', message: 'Value error, Each prescription item may appear only once.' })
      }
    }
  }
  const notes = body?.notes?.trim() || null

  let asked: { item: StoredItem; quantity: number }[]
  if (!body?.items) {
    asked = p.items.filter((i) => i.medicine_id && remainingOf(i) > 0).map((item) => ({ item, quantity: remainingOf(item) }))
    if (asked.length === 0) return brokenRule('Nothing on this prescription is left to dispense.')
  } else {
    asked = []
    for (const [i, sentItem] of body.items.entries()) {
      const item = p.items.find((x) => x.id === sentItem.prescription_item_id)
      if (!item) return refused(`items.${i}.prescription_item_id`, 'Not an item on this prescription.')
      if (!item.medicine_id) {
        return refused(`items.${i}.prescription_item_id`, `'${item.medicine_name}' is a free-text line and is not stocked here.`)
      }
      if (sentItem.quantity > remainingOf(item)) {
        return refused(`items.${i}.quantity`, `Only ${remainingOf(item)} of ${item.medicine_name} is left to dispense.`)
      }
      asked.push({ item, quantity: sentItem.quantity })
    }
  }

  const leftAfter = (item: StoredItem) => remainingOf(item) - (asked.find((a) => a.item === item)?.quantity ?? 0)
  if (p.items.some((i) => i.medicine_id && leftAfter(i) > 0) && !notes) {
    return refused('notes', 'A partial dispense needs a reason. Say why in the notes.')
  }
  const retired = asked.find((a) => !medicines.find((m) => m.id === a.item.medicine_id)?.is_active)
  if (retired) {
    return brokenRule('A prescribed medicine has been deactivated and cannot be dispensed.', { medicine_id: retired.item.medicine_id })
  }

  // First to expire first, all or nothing: every line is allocated before anything is written.
  const shortages = asked
    .filter((a) => availableOf(a.item.medicine_id!) < a.quantity)
    .map((a) => ({ prescription_item_id: a.item.id, medicine: a.item.medicine_name, requested: a.quantity, available: availableOf(a.item.medicine_id!) }))
  if (shortages.length > 0) {
    return fail(409, `Not enough stock to dispense: ${shortages.map((s) => s.medicine).join(', ')}. Nothing was dispensed.`, {
      error_code: 'RESOURCE_CONFLICT',
      errors: { shortages },
    })
  }

  const id = `d${dispenses.length + 1}`
  const items: DispenseItem[] = []
  for (const { item, quantity } of asked) {
    const stocked = medicines.find((m) => m.id === item.medicine_id)!
    let need = quantity
    for (const batch of batchesOf(stocked.id)) {
      if (need === 0) break
      const take = Math.min(need, batch.quantity_on_hand)
      batch.quantity_on_hand -= take
      need -= take
      items.push({
        id: `${id}-i${items.length + 1}`,
        prescription_item_id: item.id,
        medicine_id: stocked.id,
        // The catalog's current name, without the strength the prescription line carries.
        medicine_name: stocked.name,
        batch_id: batch.id,
        batch_number: batch.batch_number,
        expiry_date: batch.expiry_date,
        quantity: take,
        unit_price: stocked.unit_price,
        total: money(take * cents(stocked.unit_price)),
      })
    }
    item.quantity_dispensed += quantity
  }
  const total = items.reduce((sum, i) => sum + cents(i.total), 0)
  p.status = p.items.every((i) => !i.medicine_id || remainingOf(i) === 0) ? 'dispensed' : 'partially_dispensed'

  // Charged to the visit's draft invoice in the same step (§9.6).
  let invoice = invoices.find((inv) => inv.appointment_id === p.appointment_id && inv.status === 'draft')
  if (!invoice) {
    invoice = {
      id: `inv${invoices.length + 1}`,
      invoice_number: null,
      patient_id: p.patient_id,
      patient_name: p.patient_name,
      appointment_id: p.appointment_id,
      status: 'draft',
      currency: 'INR',
      total: '0.00',
      amount_paid: '0.00',
      amount_refunded: '0.00',
      balance_due: '0.00',
      discount_pending_approval: false,
      issued_at: null,
      created_at: '2026-10-05T10:00:00Z',
    }
    invoices.push(invoice)
  }
  invoice.total = invoice.balance_due = money(cents(invoice.total) + total)

  const created: StoredDispense = {
    id,
    prescription_id: p.id,
    dispensed_at: '2026-10-05T10:00:00Z',
    dispensed_by: 'u-pharmacist',
    total_amount: money(total),
    notes,
    invoice_id: invoice.id,
    items,
  }
  dispenses.push(created)
  return { status: 201, data: { success: true, message: 'Dispensed.', data: dispenseView(created), metadata: null } }
}

function server(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  const method = config.method ?? 'get'
  const params = (config.params ?? {}) as Record<string, unknown>

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

  if (url === '/prescriptions/pending' && method === 'get') {
    // Longest waiting first. No filter is read here, whatever is sent.
    const waiting = prescriptions.filter((p) => WAITING.has(p.status)).sort((a, b) => a.prescribed_at.localeCompare(b.prescribed_at))
    return pageOf(waiting.map(view), config)
  }
  if (url === '/prescriptions' && method === 'get') {
    // An empty filter is not "no filter": it fails the enum, as it does on the real API.
    if (params.status !== undefined && !['active', 'partially_dispensed', 'dispensed', 'cancelled'].includes(params.status as string)) {
      return invalid({ field: 'query.status', message: "Input should be 'active', 'partially_dispensed', 'dispensed' or 'cancelled'" })
    }
    for (const key of ['patient_id', 'doctor_id', 'appointment_id']) {
      if (params[key] === '') return invalid({ field: `query.${key}`, message: 'Input should be a valid UUID' })
    }
    const matching = prescriptions
      .filter(
        (p) =>
          (params.status === undefined || p.status === params.status) &&
          (params.patient_id === undefined || p.patient_id === params.patient_id) &&
          (params.doctor_id === undefined || p.doctor_id === params.doctor_id) &&
          (params.appointment_id === undefined || p.appointment_id === params.appointment_id),
      )
      .sort((a, b) => b.prescribed_at.localeCompare(a.prescribed_at))
    return pageOf(matching.map(view), config)
  }
  if (url === '/prescriptions' && method === 'post') return writePrescription(config)

  const match = /^\/prescriptions\/([^/]+)(?:\/(cancel|dispense|dispenses))?$/.exec(url)
  if (match) {
    const [, id, step] = match
    const p = prescriptions.find((x) => x.id === id)
    if (!p) return notFound(id)
    if (method === 'get' && !step) return ok(view(p))
    if (method === 'get' && step === 'dispenses') return ok(dispenses.filter((d) => d.prescription_id === id).map(dispenseView))
    if (method === 'post' && step === 'dispense') return dispensePrescription(p, config)
    if (method === 'post' && step === 'cancel') {
      const body = bodyOf(config) as { reason?: string }
      const extra = extraKey(body as Record<string, unknown>, ['reason'])
      if (extra) return extra
      if (!isText(body.reason, 500)) return invalid({ field: 'reason', message: 'Value error, Reason must not be blank.' })
      if (p.status !== 'active') {
        return brokenRule(`Cannot cancel a prescription that is ${p.status.replace('_', ' ')}.`, { status: p.status })
      }
      Object.assign(p, { status: 'cancelled', cancelled_at: '2026-10-05T09:45:00Z', cancel_reason: body.reason.trim() })
      return ok(view(p))
    }
  }

  if (url === '/invoices' && method === 'get') {
    return pageOf(invoices.filter((inv) => params.patient_id === undefined || inv.patient_id === params.patient_id), config)
  }
  return pageOf([], config)
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  today = '2026-10-05'
  medicines = [PARA, AMOX, ATOR, RANI].map((m) => structuredClone(m))
  batches = [
    // Expires in 20 days: dispensed first, and warned about.
    { id: 'b1', medicine_id: PARA.id, batch_number: 'PA-2401', expiry_date: '2026-10-25', quantity_on_hand: 6, is_recalled: false },
    { id: 'b2', medicine_id: PARA.id, batch_number: 'PA-2502', expiry_date: '2027-10-05', quantity_on_hand: 500, is_recalled: false },
    // Expired: on the shelf, never available.
    { id: 'b3', medicine_id: AMOX.id, batch_number: 'AX-2311', expiry_date: '2026-09-05', quantity_on_hand: 40, is_recalled: false },
    { id: 'b4', medicine_id: AMOX.id, batch_number: 'AX-2503', expiry_date: '2027-08-01', quantity_on_hand: 5, is_recalled: false },
    { id: 'b5', medicine_id: ATOR.id, batch_number: 'AT-2504', expiry_date: '2027-06-02', quantity_on_hand: 8, is_recalled: false },
  ]
  visits = [structuredClone(VISIT), structuredClone(OTHER_VISIT)]
  prescriptions = []
  dispenses = []
  invoices = []
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
          <Route path="/billing/:invoiceId" element={<p>Invoice page</p>} />
          <Route path="/patients/:patientId" element={<p>Patient page</p>} />
          <Route
            path="/pharmacy/prescriptions"
            element={
              <RequirePermission permission="pharmacy.prescription.read">
                <PrescriptionsPage />
              </RequirePermission>
            }
          />
          <Route
            path="/pharmacy/prescriptions/:prescriptionId"
            element={
              <RequirePermission permission="pharmacy.prescription.read">
                <PrescriptionDetailPage />
              </RequirePermission>
            }
          />
          <Route path="*" element={null} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** The prescribing dialog as the appointment queue will launch it: for one visit, behind a button. */
function renderPrescribe(permissions: string[]) {
  renderAt(
    '/nowhere',
    permissions,
    <PrescribeDialog
      visit={{ id: VISIT.id, patient_name: VISIT.patient_name, doctor_name: VISIT.doctor_name, scheduled_start: '2026-10-05T04:00:00Z' }}
      trigger={<button type="button">Prescribe</button>}
    />,
  )
}

const sent = (method: string, urlPart: string) => fake.requests(method, urlPart)
/** Requests to exactly this path — `/prescriptions` is also the start of every other path here. */
const sentTo = (method: string, url: string) => fake.sent.filter((c) => c.method === method && c.url === url)
const fullUrl = (config: InternalAxiosRequestConfig) => `${config.baseURL}${config.url}`
const wire = (config: InternalAxiosRequestConfig) => JSON.parse(JSON.stringify(config.params ?? {})) as Record<string, unknown>
const dialog = () => screen.findByRole('dialog')
/** The prescription's header: its status badge and actions, apart from the sections below. */
const header = () => within(document.querySelector('header') as HTMLElement)
const choose = async (user: ReturnType<typeof userEvent.setup>, combobox: HTMLElement, option: string) => {
  await user.click(combobox)
  await user.click(await screen.findByRole('option', { name: option }))
}
/** An expiry date as the screen words it: the plain day, on the viewer's calendar. */
const expiry = (date: string) => formatDate(`${date}T00:00:00`)
const openLinks = () =>
  screen.getAllByRole('link', { name: /^Open prescription for/ }).map((a) => a.getAttribute('href'))
const actionNames = () =>
  screen
    .queryAllByRole('button')
    .map((b) => b.textContent?.trim() ?? '')
    .filter((t) => /^Dispense$|Cancel prescription/.test(t))
const typeOver = async (user: ReturnType<typeof userEvent.setup>, input: HTMLElement, text: string) => {
  await user.clear(input)
  await user.type(input, text)
}
/**
 * Let anything already under way reach the network. A request leaves a few
 * ticks after the render that caused it, so "nothing was asked" is only worth
 * asserting once those ticks have passed.
 */
const settle = () => act(async () => void (await new Promise((resolve) => setTimeout(resolve, 0))))
/** Hold a request back until `release()` is called, then let the server answer it. */
function hold(matches: (config: InternalAxiosRequestConfig) => boolean) {
  let release: () => void = () => {}
  intercept = (c) => (matches(c) ? new Promise<Outcome>((resolve) => (release = () => resolve(server(c)))) : undefined)
  return () => release()
}

/** The browser's Back button, for a page that must follow the URL while it stays mounted. */
function BrowserBack() {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate(-1)}>
      Browser back
    </button>
  )
}

// ── Access ──────────────────────────────────────────────────────────────────

describe('prescriptions access', () => {
  it.each([
    ['an inventory manager', INVENTORY_MANAGER],
    ['a nurse', NURSE],
    ['a receptionist', RECEPTIONIST],
  ])('sends %s who types the address back to the dashboard, asking the API nothing', async (_who, permissions) => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    renderAt('/pharmacy/prescriptions', permissions)

    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    await settle()
    expect(sent('get', '/prescriptions')).toHaveLength(0)
  })

  it.each([
    ['an inventory manager', INVENTORY_MANAGER],
    ['a nurse', NURSE],
    ['a receptionist', RECEPTIONIST],
  ])('keeps %s out of a single prescription too', async (_who, permissions) => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    renderAt('/pharmacy/prescriptions/rx1', permissions)

    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    await settle()
    expect(sent('get', '/prescriptions')).toHaveLength(0)
  })
})

// ── The queue and the list ──────────────────────────────────────────────────

describe('prescription queue and list', () => {
  const mixed = () => [
    rx('rx1', [line('l1', PARA, 10)], { prescribed_at: '2026-10-05T09:00:00Z' }),
    rx('rx2', [line('l2', AMOX, 21), line('l3', PARA, 4)], { ...FOR_RAVI, prescribed_at: '2026-10-05T08:00:00Z' }),
    rx('rx3', ['Vitamin D drops'].map((name) => line('l4', name, 1)), { prescribed_at: '2026-10-05T08:30:00Z' }),
    rx('rx4', [line('l5', ATOR, 10, { quantity_dispensed: 10 })], { status: 'dispensed', prescribed_at: '2026-10-04T08:00:00Z' }),
  ]

  it('opens a pharmacist on the dispensing queue, longest waiting first, asking for paging only', async () => {
    prescriptions = mixed()
    renderAt('/pharmacy/prescriptions', PHARMACIST)

    const ravi = await screen.findByRole('row', { name: /Ravi Menon/ })
    // Longest waiting first, as the endpoint orders them; the dispensed one is not waiting.
    expect(openLinks()).toEqual(['/pharmacy/prescriptions/rx2', '/pharmacy/prescriptions/rx3', '/pharmacy/prescriptions/rx1'])

    const [request] = sentTo('get', '/prescriptions/pending')
    expect(fullUrl(request)).toBe('/api/v1/prescriptions/pending')
    // The queue endpoint reads no filter, so none is sent.
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    expect(sentTo('get', '/prescriptions')).toHaveLength(0)
    expect(screen.getByRole('combobox', { name: 'View' })).toHaveTextContent('To dispense')
    expect(screen.queryByRole('combobox', { name: 'Filter by status' })).not.toBeInTheDocument()

    expect(within(ravi).getByText('MRN-2026-00002')).toBeInTheDocument()
    expect(within(ravi).getByText('Dr. Priya Sharma')).toBeInTheDocument()
    expect(within(ravi).getByText('Amoxicillin 500 mg, Paracetamol 500 mg')).toBeInTheDocument()
    expect(within(ravi).getByText('To dispense')).toBeInTheDocument()
    // 21 prescribed, 5 dispensable (the expired batch does not count) — the server's numbers.
    expect(within(ravi).getByText('Short: Amoxicillin 500 mg')).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /MRN-2026-00007.*Paracetamol 500 mg/ })).getByText('In stock')).toBeInTheDocument()
    // Nothing on it is stocked: it sits in the queue but cannot be dispensed.
    expect(within(screen.getByRole('row', { name: /Vitamin D drops/ })).getByText('Nothing to dispense here')).toBeInTheDocument()
    // A pharmacist cannot open a patient record, so the name is not a link.
    expect(screen.queryByRole('link', { name: 'Ravi Menon' })).not.toBeInTheDocument()
  })

  it('opens a doctor on every prescription, newest first, with patients linked', async () => {
    prescriptions = mixed()
    renderAt('/pharmacy/prescriptions', DOCTOR)

    await screen.findByRole('row', { name: /Ravi Menon/ })
    expect(openLinks()).toEqual([
      '/pharmacy/prescriptions/rx1',
      '/pharmacy/prescriptions/rx3',
      '/pharmacy/prescriptions/rx2',
      '/pharmacy/prescriptions/rx4',
    ])
    const [request] = sentTo('get', '/prescriptions')
    expect(fullUrl(request)).toBe('/api/v1/prescriptions')
    expect(wire(request)).toEqual({ page: 1, page_size: 25 })
    expect(sentTo('get', '/prescriptions/pending')).toHaveLength(0)
    expect(screen.getByRole('combobox', { name: 'View' })).toHaveTextContent('All prescriptions')

    expect(screen.getByRole('link', { name: 'Ravi Menon' })).toHaveAttribute('href', '/patients/p2')
    // Availability is about what is waiting: a dispensed prescription shows none.
    const done = screen.getByRole('row', { name: /Atorvastatin 10 mg/ })
    expect(within(done).getByText('Dispensed')).toBeInTheDocument()
    expect(within(done).queryByText(/In stock|Short|Nothing to dispense/)).not.toBeInTheDocument()
  })

  it('filters every prescription by status with the API\'s parameter, and omits it for "all"', async () => {
    prescriptions = mixed()
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions', DOCTOR)
    await screen.findByRole('row', { name: /Ravi Menon/ })

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Dispensed')
    await waitFor(() => expect(wire(sentTo('get', '/prescriptions').at(-1)!)).toEqual({ status: 'dispensed', page: 1, page_size: 25 }))
    await waitFor(() => expect(screen.queryByRole('row', { name: /Ravi Menon/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /Atorvastatin 10 mg/ })).toBeInTheDocument()

    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'Cancelled')
    expect(await screen.findByText('No matching prescriptions')).toBeInTheDocument()

    // "All" is no filter at all — an empty `status` would be a 422 — so it is the
    // very list that was loaded first, and nothing more is asked for.
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'All statuses')
    expect(await screen.findByRole('row', { name: /Ravi Menon/ })).toBeInTheDocument()
    expect(sentTo('get', '/prescriptions').map(wire)).toEqual([
      { page: 1, page_size: 25 },
      { status: 'dispensed', page: 1, page_size: 25 },
      { status: 'cancelled', page: 1, page_size: 25 },
    ])
  })

  it('switches between the two endpoints with the View control', async () => {
    prescriptions = mixed()
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions', PHARMACIST)
    await screen.findByRole('row', { name: /Ravi Menon/ })

    await choose(user, screen.getByRole('combobox', { name: 'View' }), 'All prescriptions')

    expect(await screen.findByRole('row', { name: /Atorvastatin 10 mg/ })).toBeInTheDocument()
    expect(wire(sentTo('get', '/prescriptions')[0])).toEqual({ page: 1, page_size: 25 })
    expect(screen.getByRole('combobox', { name: 'Filter by status' })).toBeInTheDocument()

    await choose(user, screen.getByRole('combobox', { name: 'View' }), 'To dispense')
    await waitFor(() => expect(screen.queryByRole('row', { name: /Atorvastatin 10 mg/ })).not.toBeInTheDocument())
    expect(screen.queryByRole('combobox', { name: 'Filter by status' })).not.toBeInTheDocument()
  })

  it('pages through the queue, still sending paging only', async () => {
    prescriptions = Array.from({ length: 30 }, (_, n) =>
      rx(`rx${n}`, [line(`l${n}`, PARA, 1)], { patient_name: `Patient ${n}`, prescribed_at: `2026-10-05T08:${String(n).padStart(2, '0')}:00Z` }),
    )
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions', PHARMACIST)
    await screen.findByRole('row', { name: /Patient 0(?!\d)/ })
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByRole('row', { name: /Patient 29/ })).toBeInTheDocument()
    expect(wire(sentTo('get', '/prescriptions/pending').at(-1)!)).toEqual({ page: 2, page_size: 25 })
  })

  it('shows skeleton rows while the queue loads', async () => {
    prescriptions = mixed()
    const release = hold((c) => c.url === '/prescriptions/pending')
    renderAt('/pharmacy/prescriptions', PHARMACIST)

    await waitFor(() => expect(sentTo('get', '/prescriptions/pending')).toHaveLength(1))
    expect(document.querySelectorAll('tbody [data-slot="skeleton"]').length).toBeGreaterThan(0)
    expect(screen.queryByText('Nothing is waiting to be dispensed')).not.toBeInTheDocument()
    release()

    expect(await screen.findByRole('row', { name: /Ravi Menon/ })).toBeInTheDocument()
    expect(document.querySelectorAll('tbody [data-slot="skeleton"]')).toHaveLength(0)
  })

  it('says the queue is empty when nothing is waiting', async () => {
    prescriptions = [mixed()[3]]
    renderAt('/pharmacy/prescriptions', PHARMACIST)

    expect(await screen.findByText('Nothing is waiting to be dispensed')).toBeInTheDocument()
  })

  it('says where prescriptions come from when there are none at all', async () => {
    renderAt('/pharmacy/prescriptions', DOCTOR)

    expect(await screen.findByText('No prescriptions yet')).toBeInTheDocument()
    expect(screen.getByText(/Prescriptions are written from a visit/)).toBeInTheDocument()
  })

  it('offers a retry when the list cannot be loaded, without the server\'s text', async () => {
    intercept = (c) => (c.url === '/prescriptions/pending' ? fail(500, 'boom: relation "prescriptions" does not exist') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions', PHARMACIST)

    expect(await screen.findByText("Couldn't load prescriptions")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom|relation/)
    prescriptions = mixed()
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('row', { name: /Ravi Menon/ })).toBeInTheDocument()
  })

  it('narrows to one patient from a link — on the list endpoint, even for a pharmacist', async () => {
    prescriptions = mixed()
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions?patient_id=p2', PHARMACIST)

    const banner = await screen.findByText(/Showing prescriptions for/)
    await waitFor(() => expect(banner).toHaveTextContent('Showing prescriptions for Ravi Menon'))
    expect(openLinks()).toEqual(['/pharmacy/prescriptions/rx2'])
    // The queue cannot be narrowed, so the queue endpoint is not the one asked.
    expect(sentTo('get', '/prescriptions/pending')).toHaveLength(0)
    expect(wire(sentTo('get', '/prescriptions')[0])).toEqual({ patient_id: 'p2', page: 1, page_size: 25 })
    expect(screen.getByRole('combobox', { name: 'View' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'View' })).toHaveTextContent('All prescriptions')
    expect(screen.queryByRole('link', { name: 'Ravi Menon' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Show all prescriptions/ }))

    expect(await screen.findByRole('row', { name: /Atorvastatin 10 mg/ })).toBeInTheDocument()
    expect(wire(sentTo('get', '/prescriptions').at(-1)!)).toEqual({ page: 1, page_size: 25 })
    expect(screen.queryByText(/Showing prescriptions for/)).not.toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'View' })).toBeEnabled()
  })

  it('narrows to one visit, and links the patient for a user who may open the record', async () => {
    prescriptions = mixed()
    renderAt('/pharmacy/prescriptions?appointment_id=a2&patient_id=p2', DOCTOR)

    const banner = await screen.findByText(/Showing prescriptions for/)
    await waitFor(() => expect(within(banner).getByRole('link', { name: 'Ravi Menon' })).toHaveAttribute('href', '/patients/p2'))
    expect(banner).toHaveTextContent('one visit')
    expect(wire(sentTo('get', '/prescriptions')[0])).toEqual({ patient_id: 'p2', appointment_id: 'a2', page: 1, page_size: 25 })
  })

  it('tells a scoped list with nothing in it apart from an empty pharmacy', async () => {
    renderAt('/pharmacy/prescriptions?patient_id=p9', DOCTOR)

    expect(await screen.findByText('No matching prescriptions')).toBeInTheDocument()
    expect(screen.getByText('Nothing has been prescribed here yet.')).toBeInTheDocument()
  })

  it('starts a scope the browser goes back to on its own first page and with no status filter', async () => {
    prescriptions = [
      ...Array.from({ length: 30 }, (_, n) =>
        rx(`rx${n}`, [line(`l${n}`, PARA, 1)], { patient_name: `Patient ${n}`, prescribed_at: `2026-10-05T08:${String(n).padStart(2, '0')}:00Z` }),
      ),
      rx('rx-ravi', [line('l-ravi', AMOX, 21)], { ...FOR_RAVI, prescribed_at: '2026-10-04T08:00:00Z' }),
    ]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions?patient_id=p2', DOCTOR, <BrowserBack />)
    await screen.findByRole('row', { name: /Ravi Menon/ })

    await user.click(screen.getByRole('button', { name: /Show all prescriptions/ }))
    await screen.findByRole('row', { name: /Patient 29/ })
    await choose(user, screen.getByRole('combobox', { name: 'Filter by status' }), 'To dispense')
    await waitFor(() => expect(wire(sentTo('get', '/prescriptions').at(-1)!)).toEqual({ status: 'active', page: 1, page_size: 25 }))
    await user.click(await screen.findByRole('button', { name: 'Next' }))
    await waitFor(() => expect(wire(sentTo('get', '/prescriptions').at(-1)!)).toEqual({ status: 'active', page: 2, page_size: 25 }))
    await screen.findByText('Page 2 of 2')

    await user.click(screen.getByRole('button', { name: 'Browser back' }))

    // Ravi has one prescription. Page 2 of the active ones would be none of them.
    const banner = await screen.findByText(/Showing prescriptions for/)
    await waitFor(() => expect(banner).toHaveTextContent('Showing prescriptions for Ravi Menon'))
    expect(await screen.findByRole('row', { name: /Ravi Menon/ })).toBeInTheDocument()
    await settle()
    expect(screen.queryByText('No matching prescriptions')).not.toBeInTheDocument()
    expect(openLinks()).toEqual(['/pharmacy/prescriptions/rx-ravi'])
    expect(screen.getByRole('combobox', { name: 'Filter by status' })).toHaveTextContent('All statuses')
    // The scoped list was only ever asked for whole: page 1, no status.
    const scopedRequests = sentTo('get', '/prescriptions').map(wire).filter((params) => params.patient_id === 'p2')
    expect(scopedRequests.length).toBeGreaterThan(0)
    for (const params of scopedRequests) expect(params).toEqual({ patient_id: 'p2', page: 1, page_size: 25 })
  })

  it('never names or lists another patient under a scope that is still loading', async () => {
    prescriptions = mixed()
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions', DOCTOR, <Link to="/pharmacy/prescriptions?appointment_id=a2">One visit</Link>)
    // Newest first: the row on top is Ananya's, and the visit asked for next is Ravi's.
    await screen.findByRole('row', { name: /Ravi Menon/ })
    expect(openLinks()[0]).toBe('/pharmacy/prescriptions/rx1')
    const release = hold((c) => c.url === '/prescriptions' && c.params?.appointment_id === 'a2')

    await user.click(screen.getByRole('link', { name: 'One visit' }))

    const banner = await screen.findByText(/Showing prescriptions for/)
    await waitFor(() => expect(sentTo('get', '/prescriptions').some((c) => c.params?.appointment_id === 'a2')).toBe(true))
    expect(banner).not.toHaveTextContent('Ananya Rao')
    expect(screen.queryByRole('row', { name: /Ananya Rao/ })).not.toBeInTheDocument()
    expect(document.querySelectorAll('tbody [data-slot="skeleton"]').length).toBeGreaterThan(0)
    release()

    await waitFor(() => expect(openLinks()).toEqual(['/pharmacy/prescriptions/rx2']))
    expect(banner).toHaveTextContent('Showing prescriptions for Ravi Menon')
  })

  it.each([
    [403, 'Account is not active.', 'PERMISSION_DENIED', false],
    [400, 'This account is not scoped to a hospital, so the pharmacy cannot be accessed.', 'BUSINESS_RULE_VIOLATION', false],
    [429, 'Rate limit exceeded. Try again later.', 'RATE_LIMITED', true],
  ])('gives the API\'s reason when the list is refused with a %i, not a connection problem', async (status, message, error_code, retry) => {
    intercept = (c) => (c.url === '/prescriptions/pending' ? fail(status, message, { error_code, errors: null }) : undefined)
    renderAt('/pharmacy/prescriptions', PHARMACIST)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load prescriptions")
    expect(alert).toHaveTextContent(message)
    expect(alert).not.toHaveTextContent(/connection/)
    // Asking again cannot change a refusal; it can once a rate limit has passed.
    expect(within(alert).queryAllByRole('button', { name: /Retry/ })).toHaveLength(retry ? 1 : 0)
  })
})

// ── One prescription ────────────────────────────────────────────────────────

describe('prescription detail', () => {
  it('shows each line with the counts and availability the server returned', async () => {
    prescriptions = [
      rx(
        'rx1',
        [
          line('l1', PARA, 15, { quantity_dispensed: 5, instructions: 'After food.' }),
          line('l2', ATOR, 10, { duration_days: 1 }),
          line('l3', 'Vitamin D drops', 1, { dosage: '5 drops', frequency: 'daily', duration_days: null }),
        ],
        { status: 'partially_dispensed', notes: 'Review in five days.' },
      ),
    ]
    renderAt('/pharmacy/prescriptions/rx1', ADMIN)

    expect(await screen.findByRole('heading', { level: 1, name: 'Prescription' })).toBeInTheDocument()
    expect(fullUrl(sentTo('get', '/prescriptions/rx1')[0])).toBe('/api/v1/prescriptions/rx1')
    expect(header().getByText('Partly dispensed')).toBeInTheDocument()
    expect(header().getByRole('link', { name: 'Ananya Rao' })).toHaveAttribute('href', '/patients/p1')
    expect(header().getByText(/MRN-2026-00007/)).toBeInTheDocument()
    expect(screen.getByText('Review in five days.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'All prescriptions from this visit' })).toHaveAttribute(
      'href',
      '/pharmacy/prescriptions?appointment_id=a1',
    )

    const cards = within(screen.getByRole('region', { name: 'Medicines' })).getAllByRole('listitem')
    const para = cards.find((c) => within(c).queryByRole('heading', { name: 'Paracetamol 500 mg' }))!
    expect(para).toHaveTextContent('1 tablet · three times daily · 5 days')
    expect(within(para).getByText('After food.')).toBeInTheDocument()
    expect(within(para).getByText('Prescribed').nextSibling).toHaveTextContent('15')
    expect(within(para).getByText('Dispensed').nextSibling).toHaveTextContent('5')
    expect(within(para).getByText('Remaining').nextSibling).toHaveTextContent('10')
    expect(within(para).getByText('Available today').nextSibling).toHaveTextContent('506')
    expect(within(para).queryByText(/Only|Out of stock/)).not.toBeInTheDocument()

    // 10 left to dispense, 8 in dispensable stock: said in words.
    const ator = cards.find((c) => within(c).queryByRole('heading', { name: 'Atorvastatin 10 mg' }))!
    expect(ator).toHaveTextContent('1 tablet · three times daily · 1 day')
    expect(within(ator).getByText('Only 8 available')).toBeInTheDocument()

    // A typed name is not stocked: it is never shown as outstanding or available.
    const drops = cards.find((c) => within(c).queryByRole('heading', { name: 'Vitamin D drops' }))!
    expect(within(drops).getByText('Not stocked here')).toBeInTheDocument()
    expect(drops).toHaveTextContent('5 drops · daily')
    expect(within(drops).queryByText('Remaining')).not.toBeInTheDocument()
    expect(within(drops).queryByText('Available today')).not.toBeInTheDocument()

    expect(screen.getByText('Partly dispensed', { selector: 'p' })).toBeInTheDocument()
    expect(screen.getByText(/stays in the dispensing queue until it is dispensed/)).toBeInTheDocument()
  })

  it('says a line is out of stock when the server reports none', async () => {
    batches.find((b) => b.batch_number === 'AT-2504')!.is_recalled = true
    prescriptions = [rx('rx1', [line('l1', ATOR, 10)])]
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)

    const card = (await within(await screen.findByRole('region', { name: 'Medicines' })).findAllByRole('listitem'))[0]
    expect(within(card).getByText('Available today').nextSibling).toHaveTextContent('0')
    expect(within(card).getByText('Out of stock')).toBeInTheDocument()
  })

  it('shows placeholders while the prescription and its history load', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const release = hold((c) => c.url === '/prescriptions/rx1')
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)

    expect(await screen.findByLabelText('Loading prescription')).toHaveAttribute('aria-busy', 'true')
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
    // The history is not asked for until there is a prescription to show it under.
    expect(sent('get', '/dispenses')).toHaveLength(0)
    const releaseHistory = hold((c) => !!c.url?.endsWith('/dispenses'))
    release()

    expect(await screen.findByRole('heading', { level: 1, name: 'Prescription' })).toBeInTheDocument()
    expect(await screen.findByLabelText('Loading dispense history')).toBeInTheDocument()
    releaseHistory()
    expect(await screen.findByText('Nothing has been dispensed from this prescription.')).toBeInTheDocument()
  })

  it('says a prescription is not found rather than failing, and asks for nothing more', async () => {
    renderAt('/pharmacy/prescriptions/missing', PHARMACIST)

    expect(await screen.findByText('Prescription not found')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
    await settle()
    expect(sent('get', '/dispenses')).toHaveLength(0)
  })

  it('offers a retry when the prescription cannot be loaded, without the server\'s text', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    intercept = (c) => (c.url === '/prescriptions/rx1' ? fail(500, 'boom') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)

    expect(await screen.findByText("Couldn't load this prescription")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    intercept = () => undefined
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    expect(await screen.findByRole('heading', { level: 1, name: 'Prescription' })).toBeInTheDocument()
  })

  it.each([
    [403, 'Account is not active.', 'PERMISSION_DENIED', false],
    [400, 'This account is not scoped to a hospital, so the pharmacy cannot be accessed.', 'BUSINESS_RULE_VIOLATION', false],
    [429, 'Rate limit exceeded. Try again later.', 'RATE_LIMITED', true],
  ])('gives the API\'s reason when the prescription is refused with a %i, not a connection problem', async (status, message, error_code, retry) => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    intercept = (c) => (c.url === '/prescriptions/rx1' ? fail(status, message, { error_code, errors: null }) : undefined)
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load this prescription")
    expect(alert).toHaveTextContent(message)
    expect(alert).not.toHaveTextContent(/connection/)
    expect(within(alert).queryAllByRole('button', { name: /Retry/ })).toHaveLength(retry ? 1 : 0)
  })

  it('gives the API\'s reason when the dispense history is refused', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    intercept = (c) =>
      c.url?.endsWith('/dispenses') ? fail(403, 'Account is not active.', { error_code: 'PERMISSION_DENIED', errors: null }) : undefined
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)

    const history = await screen.findByRole('region', { name: 'Dispense history' })
    const alert = await within(history).findByRole('alert')
    expect(alert).toHaveTextContent('Account is not active.')
    expect(alert).not.toHaveTextContent(/connection/)
    expect(within(alert).queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
  })

  it('keeps a prescription on screen, marked as possibly out of date, when refreshing it fails', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)
    await screen.findByRole('heading', { name: 'Paracetamol 500 mg' })
    // Cancelling succeeds and the lists are read again; this read of the record fails.
    intercept = (c) => (c.url === '/prescriptions/rx1' && c.method === 'get' ? fail(500, 'boom: connection reset') : undefined)
    await user.click(screen.getByRole('button', { name: /Cancel prescription/ }))
    const d = await dialog()
    await user.type(within(d).getByLabelText(/Reason/), 'Wrong visit')
    await user.click(within(d).getByRole('button', { name: /Cancel prescription/ }))

    const stale = await screen.findByText('This prescription may be out of date')
    expect(screen.getByRole('heading', { level: 1, name: 'Prescription' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Paracetamol 500 mg' })).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load this prescription")).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom|connection reset/)
    intercept = () => undefined
    await user.click(within(stale.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: /Retry/ }))

    await waitFor(() => expect(screen.queryByText('This prescription may be out of date')).not.toBeInTheDocument())
    expect(header().getByText('Cancelled')).toBeInTheDocument()
  })

  it('shows why a prescription was cancelled, and offers nothing on it', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)], { status: 'cancelled', cancelled_at: '2026-10-05T09:45:00Z', cancel_reason: 'Written for the wrong visit' })]
    renderAt('/pharmacy/prescriptions/rx1', ADMIN)

    expect(await screen.findByText('This prescription was cancelled')).toBeInTheDocument()
    expect(screen.getByText(/Written for the wrong visit/)).toBeInTheDocument()
    expect(actionNames()).toEqual([])
    // Nothing is waiting, so availability is not shown.
    expect(screen.queryByText('Available today')).not.toBeInTheDocument()
  })

  it.each([
    ['a pharmacist, who cannot cancel it either', PHARMACIST, []],
    ['an admin, who can only cancel it', ADMIN, ['Cancel prescription']],
  ])('offers no Dispense on a prescription of typed names only to %s', async (_who, permissions, actions) => {
    prescriptions = [rx('rx1', [line('l1', 'Vitamin D drops', 1), line('l2', 'Zinc syrup', 1)])]
    renderAt('/pharmacy/prescriptions/rx1', permissions)

    expect(await screen.findByText('Nothing to dispense here')).toBeInTheDocument()
    expect(screen.getByText(/Nothing on this prescription is stocked here/)).toHaveTextContent(
      'until someone who can cancel prescriptions cancels it',
    )
    expect(actionNames()).toEqual(actions)
    expect(sent('post', '/dispense')).toHaveLength(0)
  })

  it.each([
    ['a pharmacist dispenses but cannot cancel', PHARMACIST, ['Dispense']],
    ['a doctor cancels but cannot dispense', DOCTOR, ['Cancel prescription']],
    ['an admin does both', ADMIN, ['Dispense', 'Cancel prescription']],
  ])('on a waiting prescription, %s', async (_what, permissions, actions) => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    renderAt('/pharmacy/prescriptions/rx1', permissions)

    await screen.findByRole('heading', { level: 1, name: 'Prescription' })
    expect(actionNames()).toEqual(actions)
  })

  it('offers no cancel once something has been dispensed, and nothing once it is all dispensed', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10, { quantity_dispensed: 4 })], { status: 'partially_dispensed' })]
    renderAt('/pharmacy/prescriptions/rx1', ADMIN)
    await screen.findByRole('heading', { level: 1, name: 'Prescription' })
    expect(actionNames()).toEqual(['Dispense'])
  })

  it('offers nothing on a prescription that is fully dispensed', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10, { quantity_dispensed: 10 }), line('l2', 'Vitamin D drops', 1)], { status: 'dispensed' })]
    renderAt('/pharmacy/prescriptions/rx1', ADMIN)

    await screen.findByRole('heading', { level: 1, name: 'Prescription' })
    expect(actionNames()).toEqual([])
  })
})

// ── Dispense history ────────────────────────────────────────────────────────

describe('dispense history', () => {
  /** A dispense of both lines, with its items out of line order as the API may return them. */
  const dispensedTwice = () => {
    prescriptions = [
      rx('rx1', [line('l1', PARA, 10, { quantity_dispensed: 10 }), line('l2', AMOX, 21, { quantity_dispensed: 3 })], { status: 'partially_dispensed' }),
    ]
    const item = (id: string, itemId: string, m: Medicine, batch: string, expiry_date: string, quantity: number): DispenseItem => ({
      id,
      prescription_item_id: itemId,
      medicine_id: m.id,
      medicine_name: m.name,
      batch_id: `b-${batch}`,
      batch_number: batch,
      expiry_date,
      quantity,
      unit_price: m.unit_price,
      total: money(quantity * cents(m.unit_price)),
    })
    dispenses = [
      {
        id: 'd1',
        prescription_id: 'rx1',
        dispensed_at: '2026-10-05T10:00:00Z',
        dispensed_by: 'u-pharmacist',
        total_amount: '49.00',
        notes: 'Rest of the amoxicillin when stock arrives',
        invoice_id: 'inv1',
        items: [
          item('d1-i1', 'l2', AMOX, 'AX-2503', '2027-08-01', 3),
          item('d1-i2', 'l1', PARA, 'PA-2401', '2026-10-25', 6),
          item('d1-i3', 'l1', PARA, 'PA-2502', '2027-10-05', 4),
        ],
      },
    ]
  }

  it('lists what was drawn from each batch under the name on the prescription, in line order', async () => {
    dispensedTwice()
    renderAt('/pharmacy/prescriptions/rx1', ADMIN)

    const history = await screen.findByRole('region', { name: 'Dispense history' })
    const first = await within(history).findByRole('row', { name: /PA-2401/ })
    expect(fullUrl(sent('get', '/dispenses')[0])).toBe('/api/v1/prescriptions/rx1/dispenses')
    // The dispense names the catalog medicine; the prescription's own name, with its strength, is shown.
    expect(within(first).getByText('Paracetamol 500 mg')).toBeInTheDocument()
    expect(within(first).getByText(expiry('2026-10-25'))).toBeInTheDocument()
    expect(within(first).getByText('6')).toBeInTheDocument()
    expect(within(first).getByText('2.50')).toBeInTheDocument()
    expect(within(first).getByText('15.00')).toBeInTheDocument()
    const amox = within(history).getByRole('row', { name: /AX-2503/ })
    expect(within(amox).getByText('Amoxicillin 500 mg')).toBeInTheDocument()
    expect(within(amox).getByText('24.00')).toBeInTheDocument()
    // Grouped by prescription line, whatever order the API returned the batches in.
    const batchOrder = within(history)
      .getAllByRole('row')
      .slice(1)
      .map((r) => within(r).getAllByRole('cell')[1].textContent)
    expect(batchOrder).toEqual(['PA-2401', 'PA-2502', 'AX-2503'])

    // The server's total, not a sum made here.
    expect(within(history).getByText('49.00')).toBeInTheDocument()
    expect(within(history).getByText('Note: Rest of the amoxicillin when stock arrives')).toBeInTheDocument()
    expect(within(history).getByRole('link', { name: 'View invoice' })).toHaveAttribute('href', '/billing/inv1')
  })

  it('does not repeat the warnings the API recomputes on every read', async () => {
    dispensedTwice()
    // The batch has since expired: read today, the API words it as a negative count.
    today = '2026-11-06'
    renderAt('/pharmacy/prescriptions/rx1', ADMIN)

    const history = await screen.findByRole('region', { name: 'Dispense history' })
    await within(history).findByRole('row', { name: /PA-2401/ })
    expect(warningsOf(dispenses[0])).toEqual(['Paracetamol batch PA-2401 expires in -12 days (2026-10-25).'])
    expect(document.body.textContent).not.toMatch(/expires in|expires today/)
    expect(screen.queryByText('Warning')).not.toBeInTheDocument()
  })

  it('names the invoice without a link for a pharmacist, who cannot open it', async () => {
    dispensedTwice()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)

    const history = await screen.findByRole('region', { name: 'Dispense history' })
    expect(await within(history).findByText(/Charged to an invoice/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /invoice/i })).not.toBeInTheDocument()
    expect(sent('get', '/invoices')).toHaveLength(0)
    // And the patient is named, not linked, without patient.read.
    expect(screen.queryByRole('link', { name: 'Ananya Rao' })).not.toBeInTheDocument()
    expect(header().getByText(/Ananya Rao/)).toBeInTheDocument()
  })

  it('links a doctor, who reads the invoices of their own visits, to the invoice', async () => {
    dispensedTwice()
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)

    const history = await screen.findByRole('region', { name: 'Dispense history' })
    expect(await within(history).findByRole('link', { name: 'View invoice' })).toHaveAttribute('href', '/billing/inv1')
  })

  it('says so when nothing has been dispensed', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)

    expect(await screen.findByText('Nothing has been dispensed from this prescription.')).toBeInTheDocument()
  })

  it('offers a retry when the history cannot be loaded', async () => {
    dispensedTwice()
    intercept = (c) => (c.url?.endsWith('/dispenses') ? fail(500, 'boom') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)

    const history = await screen.findByRole('region', { name: 'Dispense history' })
    expect(await within(history).findByText("Couldn't load the dispense history")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
    // The prescription itself is still shown.
    expect(screen.getByRole('heading', { name: 'Paracetamol 500 mg' })).toBeInTheDocument()
    intercept = () => undefined
    await user.click(within(history).getByRole('button', { name: /Retry/ }))

    expect(await within(history).findByRole('row', { name: /PA-2401/ })).toBeInTheDocument()
  })
})

// ── Dispensing ──────────────────────────────────────────────────────────────

describe('dispensing', () => {
  const quantityOf = (d: HTMLElement, name: string) => within(d).getByLabelText(`Quantity of ${name}`)
  const reason = (d: HTMLElement) => within(d).getByLabelText(/^Reason/)
  const dispenseButton = (d: HTMLElement) => within(d).getByRole('button', { name: 'Dispense' })
  const openDispense = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(await screen.findByRole('button', { name: 'Dispense' }))
    return dialog()
  }

  it('dispenses everything with no body, and shows the batches, total and warning the server returned', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', ADMIN, <PatientInvoices patientId="p1" />)
    const d = await openDispense(user)

    expect(d).toHaveTextContent('Ananya Rao · MRN-2026-00007 · prescribed by Dr. Priya Sharma')
    expect(d).toHaveTextContent('The system chooses the batches — earliest expiry first — and the price.')
    expect(d).toHaveTextContent('if any medicine asked for is short, nothing is dispensed')
    const row = within(within(d).getByRole('list', { name: 'Medicines to dispense' })).getByRole('listitem')
    expect(row).toHaveTextContent('1 tablet · three times daily')
    expect(row).toHaveTextContent('Remaining 10 · Available today 506')
    expect(quantityOf(d, 'Paracetamol 500 mg')).toHaveValue('10')
    // Everything is being dispensed, so no reason is asked for.
    expect(reason(d)).not.toBeRequired()
    expect(within(d).getByText('Optional — kept with the dispense')).toBeInTheDocument()
    expect(sent('post', '/dispense')).toHaveLength(0)
    const invoicesBefore = sent('get', '/invoices').length

    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    const [request] = sent('post', '/dispense')
    expect(request.method).toBe('post')
    expect(fullUrl(request)).toBe('/api/v1/prescriptions/rx1/dispense')
    // No body at all: the server dispenses everything outstanding and picks the batches.
    expect(request.data).toBeUndefined()

    // What the server did: first to expire first, across two batches.
    const first = within(d).getByRole('row', { name: /PA-2401/ })
    expect(within(first).getByText('Paracetamol 500 mg')).toBeInTheDocument()
    expect(within(first).getByText(expiry('2026-10-25'))).toBeInTheDocument()
    expect(within(first).getByText('6')).toBeInTheDocument()
    expect(within(first).getByText('2.50')).toBeInTheDocument()
    expect(within(first).getByText('15.00')).toBeInTheDocument()
    const second = within(d).getByRole('row', { name: /PA-2502/ })
    expect(within(second).getByText('4')).toBeInTheDocument()
    expect(within(second).getByText('10.00')).toBeInTheDocument()
    expect(within(d).getByText('Total').nextSibling).toHaveTextContent('25.00')
    // The server's warning, word for word.
    expect(within(d).getByText('Paracetamol batch PA-2401 expires in 20 days (2026-10-25).')).toBeInTheDocument()
    expect(within(d).getByRole('link', { name: 'View invoice' })).toHaveAttribute('href', '/billing/inv1')
    expect(toastSuccess).toHaveBeenCalledTimes(1)
    expect(toastSuccess).toHaveBeenCalledWith('Dispensed for Ananya Rao — total 25.00')

    // The server billed it; the invoice list on screen is asked for again.
    await waitFor(() => expect(sent('get', '/invoices').length).toBeGreaterThan(invoicesBefore))
    // The prescription behind the dialog is now dispensed — and the result is still on screen.
    await waitFor(() => expect(header().getByText('Dispensed')).toBeInTheDocument())
    expect(within(d).getByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()

    await user.click(within(d).getByRole('button', { name: 'Done' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(actionNames()).toEqual([])
    const history = screen.getByRole('region', { name: 'Dispense history' })
    expect(await within(history).findByRole('row', { name: /PA-2502/ })).toBeInTheDocument()
    // Once it is history the warning is not shown again.
    expect(document.body.textContent).not.toMatch(/expires in/)
    expect(batches.find((b) => b.batch_number === 'PA-2401')!.quantity_on_hand).toBe(0)
  })

  it('tells a pharmacist it was charged, without a link to an invoice they cannot open', async () => {
    prescriptions = [rx('rx1', [line('l1', ATOR, 4)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    expect(within(d).getByText("Charged to the patient's draft invoice.")).toBeInTheDocument()
    expect(within(d).queryByRole('link')).not.toBeInTheDocument()
    expect(within(d).getByText('Total').nextSibling).toHaveTextContent('26.00')
    // Nothing near expiry was drawn on, so the server sent no warning and none is invented.
    expect(within(d).queryByText('Warning')).not.toBeInTheDocument()
    // A pharmacist reads neither invoices nor patient records, and neither is asked for.
    expect(sent('get', '/invoices')).toHaveLength(0)
    expect(sent('get', '/patients')).toHaveLength(0)
  })

  it('opens each line at what the server says is available, and lists typed names as not stocked', async () => {
    batches.find((b) => b.batch_number === 'AT-2504')!.quantity_on_hand = 0
    prescriptions = [rx('rx1', [line('l1', PARA, 10), line('l2', AMOX, 21), line('l3', ATOR, 10), line('l4', 'Vitamin D drops', 1)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)

    const rows = within(within(d).getByRole('list', { name: 'Medicines to dispense' })).getAllByRole('listitem')
    expect(rows).toHaveLength(3)
    expect(quantityOf(d, 'Paracetamol 500 mg')).toHaveValue('10')
    // 21 left, 5 available: opens at 5.
    expect(rows[1]).toHaveTextContent('Remaining 21 · Available today 5')
    expect(quantityOf(d, 'Amoxicillin 500 mg')).toHaveValue('5')
    // Nothing available: opens at 0 and says so in words.
    expect(rows[2]).toHaveTextContent('Remaining 10 · Out of stock')
    expect(quantityOf(d, 'Atorvastatin 10 mg')).toHaveValue('0')
    expect(within(d).getByText(/Vitamin D drops — written by name/)).toBeInTheDocument()
    expect(within(d).queryByLabelText(/Quantity of Vitamin D drops/)).not.toBeInTheDocument()
    // Part of it will be left, so the reason is required before anything is sent.
    expect(reason(d)).toBeRequired()

    await user.type(reason(d), ' Atorvastatin and the rest of the amoxicillin to follow ')
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    // The line at 0 and the typed name are left out; no batch and no price is sent.
    expect(bodyOf(sent('post', '/dispense')[0])).toEqual({
      items: [
        { prescription_item_id: 'l1', quantity: 10 },
        { prescription_item_id: 'l2', quantity: 5 },
      ],
      notes: 'Atorvastatin and the rest of the amoxicillin to follow',
    })
    expect(within(d).getByText('Note: Atorvastatin and the rest of the amoxicillin to follow')).toBeInTheDocument()
    expect(within(d).getByText('Total').nextSibling).toHaveTextContent('65.00')
    const batchOrder = within(d)
      .getAllByRole('row')
      .slice(1)
      .map((r) => within(r).getAllByRole('cell')[1].textContent)
    expect(batchOrder).toEqual(['PA-2401', 'PA-2502', 'AX-2503'])
  })

  it('requires the reason for a partial dispense, and sends exactly the lines chosen', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10), line('l2', ATOR, 8)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    expect(reason(d)).not.toBeRequired()

    await typeOver(user, quantityOf(d, 'Atorvastatin 10 mg'), '0')
    expect(reason(d)).toBeRequired()
    await user.click(dispenseButton(d))

    expect(await within(d).findByText('Say why the rest is not being dispensed now')).toBeInTheDocument()
    expect(reason(d)).toBeInvalid()
    expect(sent('post', '/dispense')).toHaveLength(0)

    await user.type(reason(d), 'Patient will collect the atorvastatin tomorrow')
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    expect(bodyOf(sent('post', '/dispense')[0])).toEqual({
      items: [{ prescription_item_id: 'l1', quantity: 10 }],
      notes: 'Patient will collect the atorvastatin tomorrow',
    })
    await waitFor(() => expect(header().getByText('Partly dispensed')).toBeInTheDocument())
    await user.click(within(d).getByRole('button', { name: 'Done' }))

    // What is left can still be dispensed; it can no longer be cancelled.
    await waitFor(() => expect(actionNames()).toEqual(['Dispense']))
    const next = await openDispense(user)
    expect(within(within(next).getByRole('list', { name: 'Medicines to dispense' })).getAllByRole('listitem')).toHaveLength(1)
    expect(quantityOf(next, 'Atorvastatin 10 mg')).toHaveValue('8')
  })

  it('sends a note typed with a full dispense, still without naming lines', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 2)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    await user.type(reason(d), 'Handed to the patient\'s daughter')
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    expect(bodyOf(sent('post', '/dispense')[0])).toEqual({ notes: "Handed to the patient's daughter" })
  })

  it('checks each quantity before asking the server', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10), line('l2', ATOR, 8)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)

    await typeOver(user, quantityOf(d, 'Paracetamol 500 mg'), '11')
    await typeOver(user, quantityOf(d, 'Atorvastatin 10 mg'), '2.5')
    await user.click(dispenseButton(d))
    expect(await within(d).findByText('Only 10 left to dispense')).toBeInTheDocument()
    expect(within(d).getByText('Enter a whole number — 0 leaves this medicine out')).toBeInTheDocument()
    expect(quantityOf(d, 'Paracetamol 500 mg')).toBeInvalid()

    await typeOver(user, quantityOf(d, 'Paracetamol 500 mg'), '0')
    await typeOver(user, quantityOf(d, 'Atorvastatin 10 mg'), '0')
    await user.click(dispenseButton(d))
    expect(await within(d).findByText('Enter a quantity for at least one medicine')).toBeInTheDocument()
    expect(sent('post', '/dispense')).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('on a shortage dispenses nothing, shows what was short, and can dispense what is available', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    expect(quantityOf(d, 'Paracetamol 500 mg')).toHaveValue('10')
    // Another counter takes the long-dated batch after this screen was loaded.
    batches.find((b) => b.batch_number === 'PA-2502')!.quantity_on_hand = 0

    await user.click(dispenseButton(d))

    const alert = await within(d).findByRole('alert')
    expect(alert).toHaveTextContent('Nothing was dispensed')
    expect(alert).toHaveTextContent('Paracetamol 500 mg: asked for 10, 6 available')
    expect(sent('post', '/dispense')[0].data).toBeUndefined()
    // All or nothing: no dispense, no stock taken, no invoice, same status.
    expect(dispenses).toHaveLength(0)
    expect(invoices).toHaveLength(0)
    expect(batches.find((b) => b.batch_number === 'PA-2401')!.quantity_on_hand).toBe(6)
    expect(prescriptions[0].status).toBe('active')
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(toastError).toHaveBeenCalledWith('Nothing was dispensed — not enough stock.')
    // The prescription was refetched, so the form shows what the server holds now.
    await waitFor(() => expect(d).toHaveTextContent('Remaining 10 · Available today 6'))

    await user.click(within(d).getByRole('button', { name: 'Set these to what is available' }))

    expect(quantityOf(d, 'Paracetamol 500 mg')).toHaveValue('6')
    expect(within(d).getByText('Quantities set to what is available')).toBeInTheDocument()
    expect(reason(d)).toBeRequired()
    await user.click(dispenseButton(d))
    expect(await within(d).findByText('Say why the rest is not being dispensed now')).toBeInTheDocument()
    expect(sent('post', '/dispense')).toHaveLength(1)

    await user.type(reason(d), 'Only 6 in stock; rest on order')
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    expect(bodyOf(sent('post', '/dispense')[1])).toEqual({
      items: [{ prescription_item_id: 'l1', quantity: 6 }],
      notes: 'Only 6 in stock; rest on order',
    })
    expect(within(d).getByText('Total').nextSibling).toHaveTextContent('15.00')
    await waitFor(() => expect(header().getByText('Partly dispensed')).toBeInTheDocument())
  })

  it('says so when a shortage leaves nothing that can be dispensed', async () => {
    prescriptions = [rx('rx1', [line('l1', ATOR, 8)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    // The only batch is recalled after this screen was loaded.
    batches.find((b) => b.batch_number === 'AT-2504')!.is_recalled = true
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Atorvastatin 10 mg: asked for 8, 0 available')
    await user.click(within(d).getByRole('button', { name: 'Set these to what is available' }))

    expect(quantityOf(d, 'Atorvastatin 10 mg')).toHaveValue('0')
    expect(await within(d).findByText('Nothing can be dispensed now')).toBeInTheDocument()
    await waitFor(() => expect(d).toHaveTextContent('Remaining 8 · Out of stock'))
    await user.click(dispenseButton(d))
    expect(await within(d).findByText('Enter a quantity for at least one medicine')).toBeInTheDocument()
    expect(sent('post', '/dispense')).toHaveLength(1)
    expect(dispenses).toHaveLength(0)
  })

  it('puts a refused reason under the Reason field', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    // The service's shape: `errors` is an object, and the message is the reason.
    intercept = (c) =>
      c.url?.endsWith('/dispense') ? refused('notes', 'A partial dispense needs a reason. Say why in the notes.') : undefined
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    await user.click(dispenseButton(d))

    expect(await within(d).findByText('A partial dispense needs a reason. Say why in the notes.')).toBeInTheDocument()
    expect(reason(d)).toBeInvalid()
    expect(reason(d)).toHaveAccessibleDescription('A partial dispense needs a reason. Say why in the notes.')
    expect(within(d).queryByRole('heading', { name: 'Dispensed' })).not.toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('puts a refused quantity under the line it was sent for, not the row it was sent at', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10), line('l2', AMOX, 5)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    // Another counter dispenses 3 of the amoxicillin after this screen was loaded.
    prescriptions[0].items[1].quantity_dispensed = 3
    prescriptions[0].status = 'partially_dispensed'

    // Paracetamol is left out, so amoxicillin — the second row — is item 0 of what is sent.
    await typeOver(user, quantityOf(d, 'Paracetamol 500 mg'), '0')
    await user.type(reason(d), 'Paracetamol not needed today')
    await user.click(dispenseButton(d))

    expect(await within(d).findByText('Only 2 of Amoxicillin 500 mg is left to dispense.')).toBeInTheDocument()
    expect(bodyOf(sent('post', '/dispense')[0])).toEqual({
      items: [{ prescription_item_id: 'l2', quantity: 5 }],
      notes: 'Paracetamol not needed today',
    })
    expect(quantityOf(d, 'Amoxicillin 500 mg')).toBeInvalid()
    expect(quantityOf(d, 'Amoxicillin 500 mg')).toHaveAccessibleDescription('Only 2 of Amoxicillin 500 mg is left to dispense.')
    expect(quantityOf(d, 'Paracetamol 500 mg')).toBeValid()
    expect(dispenses).toHaveLength(0)
    // What is left changed, so the prescription is asked for again and the row catches up.
    await waitFor(() => expect(d).toHaveTextContent('Remaining 2 · Available today 5'))
  })

  it('places a request-validation error, which arrives as a list, the same way', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10), line('l2', ATOR, 8)])]
    intercept = (c) =>
      c.url?.endsWith('/dispense') ? invalid({ field: 'items.0.quantity', message: 'Input should be less than or equal to 1000000' }) : undefined
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    await typeOver(user, quantityOf(d, 'Paracetamol 500 mg'), '0')
    await user.type(reason(d), 'Paracetamol not needed')
    await user.click(dispenseButton(d))

    expect(await within(d).findByText('Input should be less than or equal to 1000000')).toBeInTheDocument()
    expect(quantityOf(d, 'Atorvastatin 10 mg')).toBeInvalid()
    expect(quantityOf(d, 'Paracetamol 500 mg')).toBeValid()
    // The generic "Validation failed." is not what the user is shown.
    expect(d).not.toHaveTextContent('Validation failed.')
  })

  it('shows a rule the form cannot place above it, in the API\'s words', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    intercept = (c) =>
      c.url?.endsWith('/dispense') ? invalid({ field: 'items', message: 'Value error, Each prescription item may appear only once.' }) : undefined
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Each prescription item may appear only once.')
    expect(d).not.toHaveTextContent('Value error')
  })

  it('shows the API\'s reason when the prescription was cancelled meanwhile, and catches up', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    Object.assign(prescriptions[0], { status: 'cancelled', cancelled_at: '2026-10-05T09:45:00Z', cancel_reason: 'Duplicate prescription' })

    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Cannot dispense a prescription that is cancelled.')
    expect(toastError).toHaveBeenCalledWith('Cannot dispense a prescription that is cancelled.')
    // The refreshed prescription is what is on screen: nothing to dispense, here or behind.
    expect(await within(d).findByText('Nothing is left to dispense on this prescription.')).toBeInTheDocument()
    expect(within(d).queryByRole('button', { name: 'Dispense' })).not.toBeInTheDocument()
    await waitFor(() => expect(header().getByText('Cancelled')).toBeInTheDocument())
    expect(toastSuccess).not.toHaveBeenCalled()

    await user.click(within(d).getByRole('button', { name: 'Back to the prescription' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByText('This prescription was cancelled')).toBeInTheDocument()
    expect(actionNames()).toEqual([])
  })

  it('names the medicine when one has been deactivated since it was prescribed', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10), line('l2', ATOR, 8)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    medicines.find((m) => m.id === ATOR.id)!.is_active = false

    await user.click(dispenseButton(d))

    const alert = await within(d).findByRole('alert')
    expect(alert).toHaveTextContent('A prescribed medicine has been deactivated and cannot be dispensed.')
    expect(alert).toHaveTextContent('It is Atorvastatin 10 mg — set it to 0 to dispense the rest.')
    expect(dispenses).toHaveLength(0)
    // Still dispensable: the form stays, with what was typed.
    expect(quantityOf(d, 'Paracetamol 500 mg')).toHaveValue('10')
  })

  it.each([
    [403, 'Permission denied. Required: pharmacy.dispense.execute.', 'PERMISSION_DENIED'],
    [404, 'Prescription not found.', 'RESOURCE_NOT_FOUND'],
    [409, 'The prescription is being dispensed by someone else.', 'RESOURCE_CONFLICT'],
    [429, 'Rate limit exceeded. Try again later.', 'RATE_LIMITED'],
  ])('shows the API\'s own message for a %i', async (status, message, error_code) => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    intercept = (c) => (c.url?.endsWith('/dispense') ? fail(status, message, { error_code, errors: null }) : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('alert')).toHaveTextContent(message)
    expect(toastError).toHaveBeenCalledWith(message)
    expect(within(d).queryByRole('heading', { name: 'Dispensed' })).not.toBeInTheDocument()
  })

  it('does not claim either way after a server error, and never shows its text', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    intercept = (c) => (c.url?.endsWith('/dispense') ? fail(500, 'boom: deadlock detected') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    const readsBefore = sentTo('get', '/prescriptions/rx1').length
    await user.click(dispenseButton(d))

    const alert = await within(d).findByRole('alert')
    expect(alert).toHaveTextContent('Dispense not confirmed')
    expect(alert).toHaveTextContent("Check this prescription's dispense history before trying again")
    expect(document.body.textContent).not.toMatch(/boom|deadlock/)
    expect(toastSuccess).not.toHaveBeenCalled()
    // The record is read again so the screen shows what the server holds.
    await waitFor(() => expect(sentTo('get', '/prescriptions/rx1').length).toBeGreaterThan(readsBefore))
  })

  it('keeps the result of a dispense, warning included, when the prescription cannot be refreshed after it', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', ADMIN)
    const d = await openDispense(user)
    // The dispense goes through; the read of the prescription that follows it does not.
    intercept = (c) => (c.url === '/prescriptions/rx1' && c.method === 'get' ? fail(500, 'boom: pool exhausted') : undefined)
    const readsBefore = sentTo('get', '/prescriptions/rx1').length

    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    expect(sentTo('get', '/prescriptions/rx1').length).toBeGreaterThan(readsBefore)
    expect(dispenses).toHaveLength(1)
    // The page behind says it may be out of date — and is still the page, with the dialog on it.
    expect(await screen.findByText('This prescription may be out of date')).toBeInTheDocument()
    await settle()
    expect(screen.queryByText("Couldn't load this prescription")).not.toBeInTheDocument()
    const result = await dialog()
    expect(within(result).getByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    // Shown here or nowhere: the history never repeats a warning.
    expect(within(result).getByText('Paracetamol batch PA-2401 expires in 20 days (2026-10-25).')).toBeInTheDocument()
    expect(within(result).getByRole('row', { name: /PA-2401/ })).toBeInTheDocument()
    expect(within(result).getByText('Total').nextSibling).toHaveTextContent('25.00')
    expect(document.body.textContent).not.toMatch(/boom|pool exhausted/)

    intercept = () => undefined
    await user.click(within(result).getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Retry/ }))

    await waitFor(() => expect(header().getByText('Dispensed')).toBeInTheDocument())
    expect(screen.queryByText('This prescription may be out of date')).not.toBeInTheDocument()
    expect(actionNames()).toEqual([])
  })

  it('keeps "not confirmed" on screen when the server is down for the refresh as well', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    intercept = (c) =>
      c.url?.endsWith('/dispense') || (c.url === '/prescriptions/rx1' && c.method === 'get') ? fail(500, 'boom: deadlock detected') : undefined
    const readsBefore = sentTo('get', '/prescriptions/rx1').length

    await user.click(dispenseButton(d))

    expect(await within(d).findByRole('alert')).toHaveTextContent('Dispense not confirmed')
    // The refresh was tried and failed; the page says so and stays.
    expect(await screen.findByText('This prescription may be out of date')).toBeInTheDocument()
    expect(sentTo('get', '/prescriptions/rx1').length).toBeGreaterThan(readsBefore)
    await settle()
    expect(screen.queryByText("Couldn't load this prescription")).not.toBeInTheDocument()
    const still = await dialog()
    const alert = within(still).getByRole('alert')
    expect(alert).toHaveTextContent('Dispense not confirmed')
    expect(alert).toHaveTextContent("Check this prescription's dispense history before trying again")
    // What was typed is still there to act on once the history has been checked.
    expect(quantityOf(still, 'Paracetamol 500 mg')).toHaveValue('10')
    expect(document.body.textContent).not.toMatch(/boom|deadlock/)
    expect(sent('post', '/dispense')).toHaveLength(1)
  })

  it('does not advise dispensing "the rest" when the deactivated medicine is the only line', async () => {
    prescriptions = [rx('rx1', [line('l1', ATOR, 8)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    medicines.find((m) => m.id === ATOR.id)!.is_active = false

    await user.click(dispenseButton(d))

    const alert = await within(d).findByRole('alert')
    expect(alert).toHaveTextContent(
      'A prescribed medicine has been deactivated and cannot be dispensed. It is Atorvastatin 10 mg, which cannot be dispensed while it is inactive.',
    )
    expect(alert).not.toHaveTextContent(/set it to 0|the rest/)
    expect(dispenses).toHaveLength(0)
  })

  it('dispenses once however many times the form is submitted', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10), line('l2', ATOR, 8)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', PHARMACIST)
    const d = await openDispense(user)
    // A partial dispense: the one the server would happily repeat.
    await typeOver(user, quantityOf(d, 'Paracetamol 500 mg'), '4')
    await user.type(reason(d), 'Four now')
    const release = hold((c) => !!c.url?.endsWith('/dispense'))
    const form = dispenseButton(d).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Dispensing…' })).toBeDisabled()
    expect(within(d).getByRole('button', { name: 'Not now' })).toBeDisabled()
    await waitFor(() => expect(sent('post', '/dispense')).toHaveLength(1))
    release()

    expect(await within(d).findByRole('heading', { name: 'Dispensed' })).toBeInTheDocument()
    expect(sent('post', '/dispense')).toHaveLength(1)
    expect(toastSuccess).toHaveBeenCalledTimes(1)
    expect(dispenses).toHaveLength(1)
    expect(prescriptions[0].items[0].quantity_dispensed).toBe(4)
  })
})

// ── Cancelling ──────────────────────────────────────────────────────────────

describe('cancelling a prescription', () => {
  it('a doctor cancels with a reason, and is told nothing was dispensed or charged', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)
    await user.click(await screen.findByRole('button', { name: /Cancel prescription/ }))
    const d = await dialog()

    expect(d).toHaveTextContent('Nothing has been dispensed from this prescription for Ananya Rao')
    expect(d).toHaveTextContent('nothing has been charged for it')
    expect(d).toHaveTextContent('A cancelled prescription cannot be reopened')
    await user.click(within(d).getByRole('button', { name: 'Cancel prescription' }))
    expect(await within(d).findByText('Give a reason for cancelling')).toBeInTheDocument()
    expect(sent('post', '/cancel')).toHaveLength(0)

    await user.type(within(d).getByLabelText(/Reason/), ' Written for the wrong visit ')
    await user.click(within(d).getByRole('button', { name: 'Cancel prescription' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Prescription cancelled for Ananya Rao'))
    const [request] = sent('post', '/cancel')
    expect(request.method).toBe('post')
    expect(fullUrl(request)).toBe('/api/v1/prescriptions/rx1/cancel')
    expect(bodyOf(request)).toEqual({ reason: 'Written for the wrong visit' })
    expect(await screen.findByText('This prescription was cancelled')).toBeInTheDocument()
    expect(screen.getByText(/Written for the wrong visit/)).toBeInTheDocument()
    await waitFor(() => expect(actionNames()).toEqual([]))
  })

  it('will not send a reason longer than the API takes', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)
    await user.click(await screen.findByRole('button', { name: /Cancel prescription/ }))
    const d = await dialog()
    fireEvent.change(within(d).getByLabelText(/Reason/), { target: { value: 'x'.repeat(501) } })
    await user.click(within(d).getByRole('button', { name: 'Cancel prescription' }))

    expect(await within(d).findByText('Keep the reason under 500 characters')).toBeInTheDocument()
    expect(sent('post', '/cancel')).toHaveLength(0)
  })

  it('tells the user when something was dispensed before they cancelled', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)
    await user.click(await screen.findByRole('button', { name: /Cancel prescription/ }))
    const d = await dialog()
    prescriptions[0].items[0].quantity_dispensed = 4
    prescriptions[0].status = 'partially_dispensed'
    await user.type(within(d).getByLabelText(/Reason/), 'No longer needed')
    await user.click(within(d).getByRole('button', { name: 'Cancel prescription' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Cannot cancel a prescription that is partially dispensed.'))
    // The screen catches up with the server: partly dispensed, and no longer cancellable.
    await waitFor(() => expect(header().getByText('Partly dispensed')).toBeInTheDocument())
    await waitFor(() => expect(actionNames()).toEqual([]))
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('shows a generic message, not the server\'s text, when cancelling fails', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    intercept = (c) => (c.url?.endsWith('/cancel') ? fail(500, 'boom') : undefined)
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)
    await user.click(await screen.findByRole('button', { name: /Cancel prescription/ }))
    const d = await dialog()
    await user.type(within(d).getByLabelText(/Reason/), 'No longer needed')
    await user.click(within(d).getByRole('button', { name: 'Cancel prescription' }))

    expect(await within(d).findByRole('alert')).toHaveTextContent("Couldn't cancel the prescription. Please try again.")
    expect(document.body.textContent).not.toMatch(/boom/)
  })

  it('cancels once however many times it is confirmed', async () => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    const user = userEvent.setup()
    renderAt('/pharmacy/prescriptions/rx1', DOCTOR)
    await user.click(await screen.findByRole('button', { name: /Cancel prescription/ }))
    const d = await dialog()
    await user.type(within(d).getByLabelText(/Reason/), 'No longer needed')
    const release = hold((c) => !!c.url?.endsWith('/cancel'))
    const form = within(d).getByRole('button', { name: 'Cancel prescription' }).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Cancelling…' })).toBeDisabled()
    await waitFor(() => expect(sent('post', '/cancel')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sent('post', '/cancel')).toHaveLength(1)
  })
})

// ── Writing a prescription ──────────────────────────────────────────────────

describe('writing a prescription from a visit', () => {
  const lineGroup = (d: HTMLElement, n: number) => within(d).getByRole('group', { name: `Medicine ${n}` })
  const writeButton = (d: HTMLElement) => within(d).getByRole('button', { name: 'Write prescription' })
  const openPrescribe = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole('button', { name: 'Prescribe' }))
    return dialog()
  }
  const pick = async (user: ReturnType<typeof userEvent.setup>, group: HTMLElement, name: RegExp) =>
    user.click(await within(group).findByRole('button', { name }))
  /** Fill the fields every line needs. */
  const dose = async (user: ReturnType<typeof userEvent.setup>, group: HTMLElement, quantity = '15') => {
    await user.type(within(group).getByLabelText(/^Dosage/), '1 tablet')
    await user.type(within(group).getByLabelText(/^Frequency/), 'daily')
    await user.type(within(group).getByLabelText(/^Quantity/), quantity)
  }

  it('sends a catalog line by id and a typed line by name, with the visit and nothing else', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)

    expect(within(d).getByRole('heading', { name: 'Write prescription' })).toBeInTheDocument()
    // The visit is named: its patient, its doctor and when it is.
    expect(d).toHaveTextContent(
      `For Ananya Rao, from the visit with Dr. Priya Sharma on ${formatDateTime('2026-10-05T04:00:00Z')}.`,
    )
    expect(d).toHaveTextContent('Dr. Priya Sharma is recorded as the prescriber')
    expect(d).toHaveTextContent('Nothing is charged until the pharmacy dispenses it.')

    const first = lineGroup(d, 1)
    // The last line cannot be removed.
    expect(within(d).queryByRole('button', { name: /^Remove medicine/ })).not.toBeInTheDocument()
    await pick(user, first, /Paracetamol/)
    // Only active medicines are asked for; the retired one is not offered.
    expect(fullUrl(sent('get', '/medicines')[0])).toBe('/api/v1/medicines')
    expect(wire(sent('get', '/medicines')[0])).toEqual({ is_active: true, page: 1, page_size: 8 })
    expect(within(d).queryByText(/Ranitidine/)).not.toBeInTheDocument()
    await user.type(within(first).getByLabelText(/^Dosage/), ' 1 tablet ')
    await user.type(within(first).getByLabelText(/^Frequency/), 'three times daily')
    await user.type(within(first).getByLabelText(/^Duration/), '5')
    await user.type(within(first).getByLabelText(/^Quantity/), '15')
    await user.type(within(first).getByLabelText(/^Instructions/), 'After food.')

    await user.click(within(d).getByRole('button', { name: /Add medicine/ }))
    const second = lineGroup(d, 2)
    await user.click(within(second).getByRole('radio', { name: 'Not in the catalog' }))
    expect(within(second).getByText('Recorded on the prescription, but it cannot be dispensed here.')).toBeInTheDocument()
    await user.type(within(second).getByLabelText(/^Medicine name/), 'Vitamin D drops')
    await user.type(within(second).getByLabelText(/^Dosage/), '5 drops')
    await user.type(within(second).getByLabelText(/^Frequency/), 'daily')
    await user.type(within(second).getByLabelText(/^Quantity/), '1')
    await user.type(within(d).getByLabelText(/^Notes/), ' Review in five days. ')
    expect(sentTo('post', '/prescriptions')).toHaveLength(0)

    await user.click(writeButton(d))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Prescription written — 2 medicines for Ananya Rao'))
    const [request] = sentTo('post', '/prescriptions')
    expect(request.method).toBe('post')
    expect(fullUrl(request)).toBe('/api/v1/prescriptions')
    // The patient and the prescriber are the visit's: sending either is a 422, as is
    // a line with both an id and a name. Numbers go as numbers; blanks are left out.
    expect(bodyOf(request)).toEqual({
      appointment_id: 'a1',
      notes: 'Review in five days.',
      items: [
        { medicine_id: PARA.id, dosage: '1 tablet', frequency: 'three times daily', duration_days: 5, instructions: 'After food.', quantity: 15 },
        { medicine_name: 'Vitamin D drops', dosage: '5 drops', frequency: 'daily', quantity: 1 },
      ],
    })
    expect(prescriptions).toHaveLength(1)
    expect(prescriptions[0].items.map((i) => i.medicine_name)).toEqual(['Paracetamol 500 mg', 'Vitamin D drops'])
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    // A doctor cannot read stock, and the form never asks for it.
    expect(sent('get', '/stock')).toHaveLength(0)
    expect(sent('get', '/batches')).toHaveLength(0)
    // Nothing is dispensed or billed by writing a prescription.
    expect(sent('get', '/invoices')).toHaveLength(0)
  })

  it('words the toast for a single medicine', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    await pick(user, lineGroup(d, 1), /Amoxicillin/)
    await dose(user, lineGroup(d, 1), '21')
    await user.click(writeButton(d))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Prescription written — 1 medicine for Ananya Rao'))
    expect(bodyOf(sentTo('post', '/prescriptions')[0])).toEqual({
      appointment_id: 'a1',
      items: [{ medicine_id: AMOX.id, dosage: '1 tablet', frequency: 'daily', quantity: 21 }],
    })
  })

  it('needs exactly one of a catalog medicine and a typed name on every line', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    const first = lineGroup(d, 1)
    await within(first).findByRole('button', { name: /Paracetamol/ })

    await user.click(writeButton(d))
    expect(await within(first).findByText('Choose a medicine')).toBeInTheDocument()
    expect(within(first).getByText('Enter the dosage')).toBeInTheDocument()
    expect(within(first).getByText('Enter how often it is taken')).toBeInTheDocument()
    expect(within(first).getByText('Enter the quantity')).toBeInTheDocument()

    await user.click(within(first).getByRole('radio', { name: 'Not in the catalog' }))
    expect(within(first).queryByText('Choose a medicine')).not.toBeInTheDocument()
    await user.click(writeButton(d))
    expect(await within(first).findByText("Enter the medicine's name")).toBeInTheDocument()
    expect(within(first).getByLabelText(/^Medicine name/)).toBeInvalid()
    expect(sentTo('post', '/prescriptions')).toHaveLength(0)
  })

  it('never sends a typed name left behind on a line that went back to the catalog', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    const first = lineGroup(d, 1)
    await user.click(within(first).getByRole('radio', { name: 'Not in the catalog' }))
    // Longer than the API takes — and about to be hidden, so it must not block the form.
    fireEvent.change(within(first).getByLabelText(/^Medicine name/), { target: { value: 'x'.repeat(201) } })
    await user.click(within(first).getByRole('radio', { name: 'From the catalog' }))
    await pick(user, first, /Atorvastatin/)
    await dose(user, first, '30')
    await user.click(writeButton(d))

    await waitFor(() => expect(sentTo('post', '/prescriptions')).toHaveLength(1))
    expect(bodyOf(sentTo('post', '/prescriptions')[0])).toEqual({
      appointment_id: 'a1',
      items: [{ medicine_id: ATOR.id, dosage: '1 tablet', frequency: 'daily', quantity: 30 }],
    })
  })

  it('checks whole numbers and lengths before asking the server', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    const first = lineGroup(d, 1)
    await user.click(within(first).getByRole('radio', { name: 'Not in the catalog' }))
    fireEvent.change(within(first).getByLabelText(/^Medicine name/), { target: { value: 'x'.repeat(201) } })
    fireEvent.change(within(first).getByLabelText(/^Dosage/), { target: { value: 'x'.repeat(101) } })
    await user.type(within(first).getByLabelText(/^Frequency/), 'daily')
    await user.type(within(first).getByLabelText(/^Duration/), '0')
    await user.type(within(first).getByLabelText(/^Quantity/), '1.5')
    fireEvent.change(within(d).getByLabelText(/^Notes/), { target: { value: 'x'.repeat(2001) } })
    await user.click(writeButton(d))

    expect(await within(first).findByText('Keep the name under 200 characters')).toBeInTheDocument()
    expect(within(first).getByText('Keep the dosage under 100 characters')).toBeInTheDocument()
    expect(within(first).getByText('Whole days, 1–3650')).toBeInTheDocument()
    expect(within(first).getByText('Whole units, 1–1,000,000')).toBeInTheDocument()
    expect(within(d).getByText('Keep the notes under 2,000 characters')).toBeInTheDocument()
    expect(sentTo('post', '/prescriptions')).toHaveLength(0)
  })

  it('will not let a catalog medicine be chosen twice, and removes a line but never the last', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    await pick(user, lineGroup(d, 1), /Paracetamol/)
    await user.click(within(d).getByRole('button', { name: /Add medicine/ }))

    const taken = await within(lineGroup(d, 2)).findByRole('button', { name: /Paracetamol.*already added/ })
    expect(taken).toBeDisabled()
    expect(within(lineGroup(d, 2)).getByRole('button', { name: /Amoxicillin/ })).toBeEnabled()

    await user.click(within(d).getByRole('button', { name: 'Remove medicine 2' }))
    expect(within(d).queryByRole('group', { name: 'Medicine 2' })).not.toBeInTheDocument()
    expect(within(d).queryByRole('button', { name: /^Remove medicine/ })).not.toBeInTheDocument()
    // The first line keeps its medicine.
    expect(within(lineGroup(d, 1)).getByRole('button', { name: 'Change medicine (Paracetamol)' })).toBeInTheDocument()
  })

  it('shows the API\'s duplicate-medicine and one-of refusals where they belong', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    await pick(user, lineGroup(d, 1), /Paracetamol/)
    await dose(user, lineGroup(d, 1))

    // Request validation answers with a list; a rule on the whole list names `items`.
    intercept = (c) =>
      c.url === '/prescriptions' && c.method === 'post'
        ? invalid({ field: 'items', message: 'Value error, Each medicine may appear only once in a prescription.' })
        : undefined
    await user.click(writeButton(d))
    expect(await within(d).findByText('Each medicine may appear only once in a prescription.')).toBeInTheDocument()

    // A rule on one line names the line, not a field of it.
    intercept = (c) =>
      c.url === '/prescriptions' && c.method === 'post'
        ? invalid({ field: 'items.0', message: 'Value error, Give either medicine_id or medicine_name, not both and not neither.' })
        : undefined
    await user.click(writeButton(d))
    expect(
      await within(lineGroup(d, 1)).findByText('Give either medicine_id or medicine_name, not both and not neither.'),
    ).toBeInTheDocument()
    expect(d).not.toHaveTextContent('Value error')
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(prescriptions).toHaveLength(0)
  })

  it('shows why a visit was refused, in the dialog', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    await pick(user, lineGroup(d, 1), /Paracetamol/)
    await dose(user, lineGroup(d, 1))
    // The visit was cancelled at the front desk after the queue was loaded.
    visits[0].status = 'cancelled'
    await user.click(writeButton(d))

    expect(await within(d).findByRole('alert')).toHaveTextContent('A prescription cannot be written for a visit that is cancelled.')
    expect(toastError).toHaveBeenCalledWith('A prescription cannot be written for a visit that is cancelled.')
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(prescriptions).toHaveLength(0)
    // The dialog stays open with what was typed.
    expect(within(lineGroup(d, 1)).getByLabelText(/^Quantity/)).toHaveValue('15')
  })

  it('puts a refused medicine under the line that named it', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    await pick(user, lineGroup(d, 1), /Paracetamol/)
    await dose(user, lineGroup(d, 1))
    await user.click(within(d).getByRole('button', { name: /Add medicine/ }))
    await pick(user, lineGroup(d, 2), /Amoxicillin/)
    await dose(user, lineGroup(d, 2), '21')
    // Retired by an admin after it was picked.
    medicines.find((m) => m.id === AMOX.id)!.is_active = false
    await user.click(writeButton(d))

    expect(
      await within(lineGroup(d, 2)).findByText("Medicine 'AMOX-500' is inactive and cannot be prescribed."),
    ).toBeInTheDocument()
    expect(within(lineGroup(d, 1)).queryByText(/inactive/)).not.toBeInTheDocument()
    expect(prescriptions).toHaveLength(0)
  })

  it.each([
    [403, 'Permission denied. Required: pharmacy.prescription.create.', 'Permission denied. Required: pharmacy.prescription.create.'],
    [500, 'boom', "Couldn't write the prescription. Please try again."],
  ])('shows the right message for a %i', async (status, message, shown) => {
    intercept = (c) => (c.url === '/prescriptions' && c.method === 'post' ? fail(status, message) : undefined)
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    await pick(user, lineGroup(d, 1), /Paracetamol/)
    await dose(user, lineGroup(d, 1))
    await user.click(writeButton(d))

    expect(await within(d).findByRole('alert')).toHaveTextContent(shown)
    expect(document.body.textContent).not.toMatch(/boom/)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('writes one prescription however many times the form is submitted', async () => {
    const user = userEvent.setup()
    renderPrescribe(DOCTOR)
    const d = await openPrescribe(user)
    await pick(user, lineGroup(d, 1), /Paracetamol/)
    await dose(user, lineGroup(d, 1))
    const release = hold((c) => c.url === '/prescriptions' && c.method === 'post')
    const form = writeButton(d).closest('form') as HTMLFormElement

    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(d).findByRole('button', { name: 'Writing…' })).toBeDisabled()
    await waitFor(() => expect(sentTo('post', '/prescriptions')).toHaveLength(1))
    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(sentTo('post', '/prescriptions')).toHaveLength(1)
    expect(prescriptions).toHaveLength(1)
  })

  it('stops offering more lines at the fifty the API takes', async () => {
    renderPrescribe(DOCTOR)
    fireEvent.click(screen.getByRole('button', { name: 'Prescribe' }))
    const d = await dialog()
    const add = within(d).getByRole('button', { name: /Add medicine/ })

    for (let n = 1; n < 50; n += 1) fireEvent.click(add)

    expect(within(d).getByRole('group', { name: 'Medicine 50' })).toBeInTheDocument()
    expect(add).toBeDisabled()
    expect(within(d).getByText('A prescription can hold at most 50 medicines.')).toBeInTheDocument()
  }, 20_000)

  it('asks for no catalog when the user cannot read it, and takes each medicine by name', async () => {
    const user = userEvent.setup()
    renderPrescribe(['pharmacy.prescription.create', 'appointment.read'])
    const d = await openPrescribe(user)
    const first = lineGroup(d, 1)

    expect(within(first).queryByRole('radio')).not.toBeInTheDocument()
    expect(within(d).getByText(/cannot read the medicine catalog/)).toBeInTheDocument()
    await user.type(within(first).getByLabelText(/^Medicine name/), 'Zinc syrup')
    await dose(user, first, '1')
    await user.click(writeButton(d))

    await waitFor(() => expect(sentTo('post', '/prescriptions')).toHaveLength(1))
    expect(bodyOf(sentTo('post', '/prescriptions')[0])).toEqual({
      appointment_id: 'a1',
      items: [{ medicine_name: 'Zinc syrup', dosage: '1 tablet', frequency: 'daily', quantity: 1 }],
    })
    expect(sent('get', '/medicines')).toHaveLength(0)
  })
})

// ── Integration ─────────────────────────────────────────────────────────────

describe('prescriptions in the rest of the app', () => {
  it('shows a patient\'s prescriptions on their record, to those who may read them', async () => {
    prescriptions = [
      rx('rx1', [line('l1', PARA, 10), line('l2', 'Vitamin D drops', 1)], { status: 'partially_dispensed' }),
      rx('rx2', [line('l3', AMOX, 21)], FOR_RAVI),
    ]
    renderAt('/nowhere', DOCTOR, <PatientPrescriptions patientId="p1" />)

    const section = await screen.findByRole('region', { name: 'Prescriptions' })
    const row = await within(section).findByRole('link', { name: /Paracetamol 500 mg, Vitamin D drops/ })
    expect(row).toHaveAttribute('href', '/pharmacy/prescriptions/rx1')
    expect(within(row).getByText('Partly dispensed')).toBeInTheDocument()
    expect(within(section).queryByText(/Amoxicillin/)).not.toBeInTheDocument()
    expect(within(section).getByRole('link', { name: /Open in Pharmacy/ })).toHaveAttribute(
      'href',
      '/pharmacy/prescriptions?patient_id=p1',
    )
    const [request] = sentTo('get', '/prescriptions')
    expect(fullUrl(request)).toBe('/api/v1/prescriptions')
    expect(wire(request)).toEqual({ patient_id: 'p1', page_size: 5 })
  })

  it('says so when the patient has no prescriptions, or they cannot be loaded', async () => {
    renderAt('/nowhere', DOCTOR, <PatientPrescriptions patientId="p1" />)
    expect(await screen.findByText('No prescriptions for this patient yet.')).toBeInTheDocument()
  })

  it('does not show the server\'s text when the record\'s prescriptions fail to load', async () => {
    intercept = (c) => (c.url === '/prescriptions' ? fail(500, 'boom') : undefined)
    renderAt('/nowhere', DOCTOR, <PatientPrescriptions patientId="p1" />)

    expect(await screen.findByText("This patient's prescriptions couldn't be loaded.")).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/boom/)
  })

  it.each([
    ['a receptionist', RECEPTIONIST],
    ['a nurse', NURSE],
    ['an inventory manager', INVENTORY_MANAGER],
  ])('shows nothing on the record to %s, and asks nothing', async (_who, permissions) => {
    prescriptions = [rx('rx1', [line('l1', PARA, 10)])]
    renderAt('/nowhere', permissions, <PatientPrescriptions patientId="p1" />)

    await settle()
    expect(screen.queryByRole('region', { name: 'Prescriptions' })).not.toBeInTheDocument()
    expect(sent('get', '/prescriptions')).toHaveLength(0)
  })
})
