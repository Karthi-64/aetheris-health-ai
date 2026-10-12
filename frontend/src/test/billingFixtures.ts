import type { Invoice, InvoiceSummary, Payment, Refund, Service } from '@/api/billing'

/** Test data and helpers shared by the billing tests. */

// Role → billing permission sets, as seeded (docs/18-API_CONTRACTS.md §6.11).
export const ADMIN = [
  'patient.read',
  'appointment.read',
  'service.read',
  'invoice.read',
  'invoice.read.own',
  'invoice.create',
  'invoice.update',
  'invoice.issue',
  'invoice.approve_discount',
  'invoice.void',
  'invoice.refund',
  'invoice.payment.record',
  'invoice.payment.record.cash',
]
export const BILLING_STAFF = [
  'patient.read',
  'service.read',
  'invoice.read',
  'invoice.create',
  'invoice.update',
  'invoice.issue',
  'invoice.payment.record',
]
export const RECEPTIONIST = [
  'patient.read',
  'appointment.read',
  'service.read',
  'invoice.read',
  'invoice.payment.record.cash',
]
export const DOCTOR = ['patient.read', 'appointment.read', 'invoice.read.own']

export { signIn, signOut } from '@/test/auth'

export const SERVICES: Service[] = [
  {
    id: 'svc-ecg',
    code: 'ECG',
    name: 'ECG (12-lead)',
    category: 'Diagnostics',
    price: '450.00',
    taxable: false,
    is_active: true,
    created_at: '2026-10-01T14:17:10Z',
    updated_at: '2026-10-01T14:17:10Z',
  },
  {
    id: 'svc-cbc',
    code: 'CBC',
    name: 'Complete blood count',
    category: 'Laboratory',
    price: '350.00',
    taxable: false,
    is_active: true,
    created_at: '2026-10-01T14:17:10Z',
    updated_at: '2026-10-01T14:17:10Z',
  },
]

/** A draft with one catalog line and one custom line. Totals are what the server would send. */
export function draftInvoice(overrides: Partial<Invoice> = {}): Invoice {
  return {
    id: 'inv-1',
    hospital_id: 'h1',
    invoice_number: null,
    patient_id: 'pat-1',
    patient_name: 'Thomas George',
    appointment_id: 'appt-1',
    status: 'draft',
    currency: 'INR',
    items: [
      {
        id: 'li-1',
        service_id: 'svc-ecg',
        description: 'ECG (12-lead)',
        quantity: '1.00',
        unit_price: '450.00',
        tax_rate: '0.00',
        line_total: '450.00',
        position: 0,
      },
      {
        id: 'li-2',
        service_id: null,
        description: 'Crepe bandage',
        quantity: '2.00',
        unit_price: '75.00',
        tax_rate: '0.00',
        line_total: '150.00',
        position: 1,
      },
    ],
    subtotal: '600.00',
    tax_amount: '0.00',
    discount_amount: '0.00',
    discount_reason: null,
    discount_pending_approval: false,
    discount_approved_by: null,
    total: '600.00',
    amount_paid: '0.00',
    amount_refunded: '0.00',
    balance_due: '600.00',
    notes: 'Counter items',
    issued_at: null,
    voided_at: null,
    void_reason: null,
    created_at: '2026-10-01T14:17:11Z',
    updated_at: '2026-10-01T14:17:11Z',
    ...overrides,
  }
}

export const issuedInvoice = (overrides: Partial<Invoice> = {}): Invoice =>
  draftInvoice({
    invoice_number: 'INV-2026-000007',
    status: 'issued',
    issued_at: '2026-10-02T05:05:00Z',
    ...overrides,
  })

export function summaryOf(invoice: Invoice): InvoiceSummary {
  return {
    id: invoice.id,
    invoice_number: invoice.invoice_number,
    patient_id: invoice.patient_id,
    patient_name: invoice.patient_name,
    appointment_id: invoice.appointment_id,
    status: invoice.status,
    currency: invoice.currency,
    total: invoice.total,
    amount_paid: invoice.amount_paid,
    amount_refunded: invoice.amount_refunded,
    balance_due: invoice.balance_due,
    discount_pending_approval: invoice.discount_pending_approval,
    issued_at: invoice.issued_at,
    created_at: invoice.created_at,
  }
}

export const payment = (overrides: Partial<Payment> = {}): Payment => ({
  id: 'pay-1',
  invoice_id: 'inv-1',
  amount: '200.00',
  method: 'card',
  reference: 'CARD-TXN-8899',
  notes: null,
  received_by: 'u1',
  received_at: '2026-10-02T05:10:00Z',
  ...overrides,
})

export const refund = (overrides: Partial<Refund> = {}): Refund => ({
  id: 'ref-1',
  invoice_id: 'inv-1',
  amount: '100.00',
  method: 'cash',
  reason: 'Sample could not be processed',
  reference: null,
  refunded_by: 'u1',
  refunded_at: '2026-10-03T14:30:00Z',
  ...overrides,
})
