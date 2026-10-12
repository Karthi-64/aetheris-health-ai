import { useRef } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import { newIdempotencyKey } from '@/api/idempotency'
import { dashboardKeys, reportKeys } from '@/api/reports'
import { ApiError, type ListQueryOptions, type Paginated } from '@/api/types'

/**
 * Billing API — typed hooks over the contract in `docs/18-API_CONTRACTS.md` §6
 * (`backend/app/api/v1/invoices.py`, `services.py`, `schemas/billing.py`).
 *
 * Every amount is a decimal **string** and every total is computed by the
 * server (§6.2). Nothing here adds, subtracts or rounds money: screens render
 * the strings the API returns and send back the strings the user typed.
 */

export type InvoiceStatus = 'draft' | 'issued' | 'partially_paid' | 'paid' | 'void' | 'refunded'

export type PaymentMethod = 'cash' | 'card' | 'upi' | 'bank_transfer' | 'insurance'

/** A billable catalog entry (`ServiceResponse`). */
export interface Service {
  id: string
  code: string
  name: string
  category: string | null
  price: string
  taxable: boolean
  is_active: boolean
  created_at: string
  updated_at: string
}

/** One invoice line (`InvoiceItemResponse`). */
export interface InvoiceItem {
  id: string
  /** Null for an ad-hoc line. */
  service_id: string | null
  description: string
  quantity: string
  unit_price: string
  /** Percentage, e.g. `"18.00"`. */
  tax_rate: string
  line_total: string
  position: number
}

/** List row (`InvoiceSummaryResponse`) — no lines. */
export interface InvoiceSummary {
  id: string
  /** Null while the invoice is a draft. */
  invoice_number: string | null
  patient_id: string
  patient_name: string
  appointment_id: string | null
  status: InvoiceStatus
  currency: string
  total: string
  amount_paid: string
  amount_refunded: string
  balance_due: string
  discount_pending_approval: boolean
  issued_at: string | null
  created_at: string
}

/** Full record (`InvoiceResponse`). */
export interface Invoice extends InvoiceSummary {
  hospital_id: string
  items: InvoiceItem[]
  subtotal: string
  tax_amount: string
  discount_amount: string
  discount_reason: string | null
  discount_approved_by: string | null
  notes: string | null
  voided_at: string | null
  void_reason: string | null
  updated_at: string
}

export interface Payment {
  id: string
  invoice_id: string
  amount: string
  method: PaymentMethod
  reference: string | null
  notes: string | null
  received_by: string
  received_at: string
}

export interface Refund {
  id: string
  invoice_id: string
  amount: string
  method: PaymentMethod
  reason: string
  reference: string | null
  refunded_by: string
  refunded_at: string
}

/**
 * A line in a create/update body. It is one of two shapes and nothing in
 * between (§6.4): a catalog line takes its price and tax from the service, an
 * ad-hoc line carries its own.
 */
export type InvoiceLineInput =
  | { service_id: string; description?: string; quantity: string }
  | { description: string; quantity: string; unit_price: string; taxable: boolean }

export interface CreateInvoiceInput {
  patient_id: string
  appointment_id?: string
  items: InvoiceLineInput[]
  notes?: string
}

/** `PATCH /invoices/{id}` — drafts only. `items` replaces the whole line set. */
export interface UpdateInvoiceInput {
  items?: InvoiceLineInput[]
  notes?: string
  discount_amount?: string
  discount_reason?: string
}

export interface RecordPaymentInput {
  amount: string
  method: PaymentMethod
  reference?: string
  notes?: string
}

export interface RecordRefundInput {
  amount: string
  method: PaymentMethod
  reason: string
  reference?: string
}

export interface InvoiceListParams {
  patient_id?: string
  status?: InvoiceStatus
  /** `true` returns the drafts whose discount awaits approval. */
  discount_pending?: boolean
  /** `YYYY-MM-DD`, inclusive, in the hospital's timezone. */
  issued_from?: string
  issued_to?: string
  page?: number
  page_size?: number
}

export interface ServiceListParams {
  q?: string
  is_active?: boolean
  page?: number
  page_size?: number
}

export const billingKeys = {
  all: ['invoices'] as const,
  list: (params: InvoiceListParams) => [...billingKeys.all, 'list', params] as const,
  detail: (id: string) => [...billingKeys.all, 'detail', id] as const,
  payments: (id: string) => [...billingKeys.all, 'detail', id, 'payments'] as const,
  refunds: (id: string) => [...billingKeys.all, 'detail', id, 'refunds'] as const,
  services: (params: ServiceListParams) => ['services', 'list', params] as const,
}

// ── Queries ─────────────────────────────────────────────────────────────────

/** List invoices, newest first. A doctor's list is narrowed to their own visits by the server. */
export function useInvoices(params: InvoiceListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<InvoiceSummary>>({
    enabled: options.enabled ?? true,
    queryKey: billingKeys.list(params),
    queryFn: () => http.getPaginated<InvoiceSummary>('/invoices', { params }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

export function useInvoice(id: string) {
  return useQuery<Invoice>({
    enabled: !!id,
    queryKey: billingKeys.detail(id),
    queryFn: () => http.get<Invoice>(`/invoices/${id}`),
    staleTime: 15_000,
  })
}

/** Payments oldest-first. A plain list — the endpoint sends no pagination metadata (§6.6). */
export function useInvoicePayments(id: string, options: ListQueryOptions = {}) {
  return useQuery<Payment[]>({
    enabled: (options.enabled ?? true) && !!id,
    queryKey: billingKeys.payments(id),
    queryFn: () => http.get<Payment[]>(`/invoices/${id}/payments`),
    staleTime: 15_000,
  })
}

/** Refunds oldest-first, as a plain list (§6.8). */
export function useInvoiceRefunds(id: string, options: ListQueryOptions = {}) {
  return useQuery<Refund[]>({
    enabled: (options.enabled ?? true) && !!id,
    queryKey: billingKeys.refunds(id),
    queryFn: () => http.get<Refund[]>(`/invoices/${id}/refunds`),
    staleTime: 15_000,
  })
}

/** The services catalog. Requires `service.read`; pass `enabled: false` for users without it. */
export function useServices(params: ServiceListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<Service>>({
    enabled: options.enabled ?? true,
    queryKey: billingKeys.services(params),
    queryFn: () => http.getPaginated<Service>('/services', { params }),
    staleTime: 60_000,
  })
}

// ── Mutations ───────────────────────────────────────────────────────────────

/**
 * Shared mutation plumbing. Every billing write changes what the lists, the
 * detail and the payment history show, so all invoice queries are refetched —
 * and the mutation settles only once they have, so a screen never offers an
 * action against totals the server has already moved past.
 *
 * A 400, 404 or 409 means the invoice was not in the state the screen showed
 * (already paid, already approved, gone), so the same refetch runs on those.
 */
function useInvoiceMutation<TInput, TResult>(mutationFn: (input: TInput) => Promise<TResult>) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: () => {
      // Every billing write also moves a dashboard tile or a report figure.
      qc.invalidateQueries({ queryKey: dashboardKeys.all })
      qc.invalidateQueries({ queryKey: reportKeys.all })
      return qc.invalidateQueries({ queryKey: billingKeys.all })
    },
    onError: (err) => {
      if (err instanceof ApiError && [400, 404, 409].includes(err.status ?? 0)) {
        qc.invalidateQueries({ queryKey: billingKeys.all })
      }
    },
  })
}

/** Create a draft invoice. 409 when the appointment already has a live invoice. */
export function useCreateInvoice() {
  return useInvoiceMutation((input: CreateInvoiceInput) => http.post<Invoice>('/invoices', input))
}

/** Edit a draft: lines, notes or discount. */
export function useUpdateInvoice(id: string) {
  return useInvoiceMutation((input: UpdateInvoiceInput) =>
    http.patch<Invoice>(`/invoices/${id}`, input),
  )
}

/** Issue a draft: freezes the totals and assigns the invoice number. No body. */
export function useIssueInvoice(id: string) {
  return useInvoiceMutation<void, Invoice>(() => http.post<Invoice>(`/invoices/${id}/issue`))
}

/** Approve a pending discount. No body; 409 when there is nothing to approve. */
export function useApproveDiscount(id: string) {
  return useInvoiceMutation<void, Invoice>(() =>
    http.post<Invoice>(`/invoices/${id}/approve-discount`),
  )
}

/** Void an issued, unpaid invoice. The reason is required. */
export function useVoidInvoice(id: string) {
  return useInvoiceMutation((reason: string) =>
    http.post<Invoice>(`/invoices/${id}/void`, { reason }),
  )
}

/**
 * The `Idempotency-Key` for a payment or refund (§6.6, §6.8).
 *
 * One key per attempt: resubmitting the same amount by the same method after a
 * timeout reuses it, so the server replays the original record (200) instead of
 * taking the money twice. The server treats a known key with a different
 * amount or method as a 409, so either changing starts a new attempt — as does
 * a success, since a second payment on the same invoice is a new payment.
 */
function useAttemptKey() {
  const attempt = useRef<{ fingerprint: string; key: string } | null>(null)
  return {
    keyFor(fingerprint: string): string {
      if (attempt.current?.fingerprint !== fingerprint) {
        attempt.current = { fingerprint, key: newIdempotencyKey() }
      }
      return attempt.current.key
    },
    reset() {
      attempt.current = null
    },
  }
}

/** Record a payment. Overpayment is a 400; a cash-only user gets 403 for other methods. */
export function useRecordPayment(id: string) {
  const attempt = useAttemptKey()
  return useInvoiceMutation(async (input: RecordPaymentInput) => {
    const key = attempt.keyFor(`${input.amount}|${input.method}`)
    const result = await http.post<{ payment: Payment; invoice: InvoiceSummary }>(
      `/invoices/${id}/payments`,
      input,
      { headers: { 'Idempotency-Key': key } },
    )
    attempt.reset()
    return result
  })
}

/** Refund money already paid. Refunding everything closes the invoice as `refunded`. */
export function useRecordRefund(id: string) {
  const attempt = useAttemptKey()
  return useInvoiceMutation(async (input: RecordRefundInput) => {
    const key = attempt.keyFor(`${input.amount}|${input.method}`)
    const result = await http.post<{ refund: Refund; invoice: InvoiceSummary }>(
      `/invoices/${id}/refund`,
      input,
      { headers: { 'Idempotency-Key': key } },
    )
    attempt.reset()
    return result
  })
}
