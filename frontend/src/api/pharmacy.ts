import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import { billingKeys } from '@/api/billing'
import { ApiError, type ListQueryOptions, type Paginated } from '@/api/types'

/**
 * Pharmacy API — typed hooks over the real backend contract
 * (`backend/app/api/v1/medicines.py`, `prescriptions.py`, `vendors.py`,
 * `purchase_orders.py`, `backend/app/schemas/pharmacy.py`;
 * docs/18-API_CONTRACTS.md §9).
 *
 * Not built on the server, so not here: interaction warnings, AI
 * substitution, reversing a dispense, editing a prescription or a purchase
 * order (§9.9, §12).
 *
 * None of these writes takes an `Idempotency-Key`, so nothing here retries a
 * write and every screen that calls one guards against a second submit.
 */

// ── Medicines, batches and stock (§9.3) ─────────────────────────────────────

/** A catalog medicine (`MedicineResponse`). */
export interface Medicine {
  id: string
  /** Stock code. Uppercased by the server, unique per hospital, never changed. */
  sku: string
  name: string
  generic_name: string | null
  strength: string | null
  form: string | null
  atc_code: string | null
  /** Selling price per unit, a decimal string. */
  unit_price: string
  requires_prescription: boolean
  is_active: boolean
  created_at: string
  updated_at: string
}

/** Body for `POST /medicines`. */
export interface CreateMedicineInput {
  sku: string
  name: string
  generic_name?: string | null
  strength?: string | null
  form?: string | null
  atc_code?: string | null
  unit_price: string
  requires_prescription: boolean
}

/** Body for `PATCH /medicines/{id}`. The SKU cannot be changed. */
export type UpdateMedicineInput = Partial<Omit<CreateMedicineInput, 'sku'>> & { is_active?: boolean }

export interface MedicineListParams {
  /** The start of the name or generic name, or an exact SKU. */
  q?: string
  is_active?: boolean
  page?: number
  page_size?: number
}

/**
 * One batch of a medicine (`BatchResponse`). The four computed fields are the
 * server's, worked out on the hospital's local date — use them rather than
 * comparing dates in the browser.
 */
export interface MedicineBatch {
  id: string
  medicine_id: string
  batch_number: string
  /** `YYYY-MM-DD`: the last day the batch may be dispensed. */
  expiry_date: string
  /** Purchase cost per unit, a decimal string. */
  cost_per_unit: string
  initial_quantity: number
  quantity_on_hand: number
  is_recalled: boolean
  /** Negative once expired. */
  days_to_expiry: number
  is_expired: boolean
  /** Within 30 days of expiry. */
  expires_soon: boolean
  /** In stock, not expired and not recalled. */
  is_dispensable: boolean
}

/** `GET /medicines/{id}/stock` (`MedicineStockResponse`). Batches come earliest expiry first. */
export interface MedicineStock {
  medicine: Medicine
  /** Units physically held, in any state. */
  quantity_on_hand: number
  /** Units that can be dispensed today — the figure to show as "in stock". */
  dispensable_quantity: number
  expiring_soon_quantity: number
  expired_quantity: number
  recalled_quantity: number
  batches: MedicineBatch[]
}

/** Body for `POST /medicines/{id}/batches`. An existing batch number is topped up. */
export interface ReceiveBatchInput {
  batch_number: string
  expiry_date: string
  quantity: number
  cost_per_unit: string
}

/** Body for `POST /medicines/{id}/batches/{batch_id}/adjust`. */
export interface AdjustStockInput {
  /** Signed: negative removes units, positive adds them. Never zero. */
  quantity_change: number
  reason: 'adjusted' | 'expired'
  note: string
}

// ── Prescriptions and dispensing (§9.4, §9.5) ───────────────────────────────

export type PrescriptionStatus = 'active' | 'partially_dispensed' | 'dispensed' | 'cancelled'

/** One prescribed line (`PrescriptionItemResponse`). */
export interface PrescriptionItem {
  id: string
  /** Null for a free-text line — a medicine the pharmacy does not stock. */
  medicine_id: string | null
  medicine_name: string
  dosage: string
  frequency: string
  duration_days: number | null
  instructions: string | null
  quantity: number
  quantity_dispensed: number
  quantity_remaining: number
  /** Units that can be dispensed today. Null on a free-text line. */
  available_quantity: number | null
}

/** A prescription (`PrescriptionResponse`). */
export interface Prescription {
  id: string
  appointment_id: string
  patient_id: string
  patient_name: string
  patient_mrn: string
  doctor_id: string
  doctor_name: string
  status: PrescriptionStatus
  notes: string | null
  prescribed_at: string
  cancelled_at: string | null
  cancel_reason: string | null
  items: PrescriptionItem[]
}

/**
 * One line of `POST /prescriptions`. Exactly one of `medicine_id` (a catalog
 * medicine) and `medicine_name` (free text) is sent; both or neither is a 422.
 */
export interface PrescriptionLineInput {
  medicine_id?: string
  medicine_name?: string
  dosage: string
  frequency: string
  duration_days?: number
  instructions?: string
  quantity: number
}

/** Body for `POST /prescriptions`. The patient and doctor come from the appointment. */
export interface CreatePrescriptionInput {
  appointment_id: string
  notes?: string
  items: PrescriptionLineInput[]
}

export interface PrescriptionListParams {
  status?: PrescriptionStatus
  patient_id?: string
  doctor_id?: string
  appointment_id?: string
  page?: number
  page_size?: number
}

/**
 * Body for `POST /prescriptions/{id}/dispense`. With no `items` everything
 * outstanding is dispensed. Naming lines dispenses part; if anything is then
 * left outstanding, `notes` is required.
 */
export interface DispenseInput {
  items?: { prescription_item_id: string; quantity: number }[]
  notes?: string
}

/** One batch drawn on by a dispense (`DispenseItemResponse`). */
export interface DispenseItem {
  id: string
  prescription_item_id: string
  medicine_id: string
  medicine_name: string
  batch_id: string
  batch_number: string
  expiry_date: string
  quantity: number
  unit_price: string
  total: string
}

/** A dispense (`DispenseResponse`). One item per batch, so a medicine may appear twice. */
export interface Dispense {
  id: string
  prescription_id: string
  dispensed_at: string
  dispensed_by: string | null
  /** Sum of the lines, a decimal string — worked out by the server. */
  total_amount: string
  notes: string | null
  /** The draft invoice the dispense was charged to. */
  invoice_id: string | null
  items: DispenseItem[]
  /** Things the server wants the pharmacist to see, e.g. a batch near expiry. */
  warnings: string[]
}

/** One medicine the server could not supply in full (the 409 from dispense). */
export interface DispenseShortage {
  prescription_item_id: string
  medicine: string
  requested: number
  available: number
}

/**
 * The shortages named by a refused dispense. The API answers 409 with
 * `errors.shortages` and dispenses nothing (§9.5); any other failure, or a 409
 * of another shape, yields an empty list.
 */
export function shortagesOf(err: unknown): DispenseShortage[] {
  if (!(err instanceof ApiError) || err.status !== 409) return []
  const list = (err.details as { shortages?: unknown } | null | undefined)?.shortages
  if (!Array.isArray(list)) return []
  return list.filter(
    (s): s is DispenseShortage =>
      typeof (s as DispenseShortage)?.prescription_item_id === 'string' &&
      typeof (s as DispenseShortage)?.medicine === 'string' &&
      typeof (s as DispenseShortage)?.requested === 'number' &&
      typeof (s as DispenseShortage)?.available === 'number',
  )
}

// ── Vendors and purchase orders (§9.7, §9.8) ────────────────────────────────

/** A vendor (`VendorResponse`). */
export interface Vendor {
  id: string
  name: string
  contact: string | null
  address: string | null
  tax_id: string | null
  is_active: boolean
  created_at: string
}

/** Body for `POST /vendors`. The name is unique per hospital. */
export interface CreateVendorInput {
  name: string
  contact?: string | null
  address?: string | null
  tax_id?: string | null
}

/** Body for `PATCH /vendors/{id}`. */
export type UpdateVendorInput = Partial<CreateVendorInput> & { is_active?: boolean }

export interface VendorListParams {
  is_active?: boolean
  page?: number
  page_size?: number
}

export type PurchaseOrderStatus = 'draft' | 'sent' | 'received' | 'cancelled'

/** One ordered line (`PurchaseOrderItemResponse`). */
export interface PurchaseOrderItem {
  id: string
  medicine_id: string
  medicine_sku: string
  medicine_name: string
  quantity: number
  unit_price: string
  total: string
}

/** A purchase order (`PurchaseOrderResponse`). */
export interface PurchaseOrder {
  id: string
  po_number: string
  vendor_id: string
  vendor_name: string
  status: PurchaseOrderStatus
  notes: string | null
  /** When it was sent. */
  ordered_at: string | null
  received_at: string | null
  /** Sum of the lines, a decimal string — worked out by the server. */
  total_amount: string
  created_at: string
  items: PurchaseOrderItem[]
}

/** Body for `POST /purchase-orders`. Creates a draft; a medicine may appear once. */
export interface CreatePurchaseOrderInput {
  vendor_id: string
  notes?: string
  items: { medicine_id: string; quantity: number; unit_price: string }[]
}

export interface PurchaseOrderListParams {
  status?: PurchaseOrderStatus
  vendor_id?: string
  page?: number
  page_size?: number
}

/**
 * One received batch of `POST /purchase-orders/{id}/receive`. An order line
 * may be split across several batches; `cost_per_unit` defaults to the order
 * line's price when left out.
 */
export interface ReceiptLineInput {
  po_item_id: string
  batch_number: string
  expiry_date: string
  quantity: number
  cost_per_unit?: string
}

// ── Query keys ──────────────────────────────────────────────────────────────

export const pharmacyKeys = {
  all: ['pharmacy'] as const,
  medicines: () => [...pharmacyKeys.all, 'medicines'] as const,
  medicineList: (params: MedicineListParams) => [...pharmacyKeys.medicines(), 'list', params] as const,
  medicine: (id: string) => [...pharmacyKeys.medicines(), 'detail', id] as const,
  /** Every stock query: stock changes with any receipt, adjustment, recall or dispense. */
  stock: () => [...pharmacyKeys.all, 'stock'] as const,
  stockFor: (medicineId: string) => [...pharmacyKeys.stock(), medicineId] as const,
  prescriptions: () => [...pharmacyKeys.all, 'prescriptions'] as const,
  prescriptionList: (params: PrescriptionListParams) =>
    [...pharmacyKeys.prescriptions(), 'list', params] as const,
  pending: (params: { page?: number; page_size?: number }) =>
    [...pharmacyKeys.prescriptions(), 'pending', params] as const,
  prescription: (id: string) => [...pharmacyKeys.prescriptions(), 'detail', id] as const,
  dispenses: (id: string) => [...pharmacyKeys.prescriptions(), 'detail', id, 'dispenses'] as const,
  vendors: () => [...pharmacyKeys.all, 'vendors'] as const,
  vendorList: (params: VendorListParams) => [...pharmacyKeys.vendors(), 'list', params] as const,
  purchaseOrders: () => [...pharmacyKeys.all, 'purchase-orders'] as const,
  purchaseOrderList: (params: PurchaseOrderListParams) =>
    [...pharmacyKeys.purchaseOrders(), 'list', params] as const,
  purchaseOrder: (id: string) => [...pharmacyKeys.purchaseOrders(), 'detail', id] as const,
}

/**
 * The server rejected a write because the record was not in the state the
 * screen showed (already sent, already cancelled, gone). What is on screen is
 * stale, so it is refetched even though the call failed.
 */
function isStale(err: unknown): boolean {
  return err instanceof ApiError && [400, 404, 409].includes(err.status ?? 0)
}

// ── Medicines ───────────────────────────────────────────────────────────────

/** The medicine catalog. Needs `pharmacy.medicine.read`. For a picker pass `is_active: true`. */
export function useMedicines(params: MedicineListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<Medicine>>({
    enabled: options.enabled ?? true,
    queryKey: pharmacyKeys.medicineList(params),
    queryFn: () => http.getPaginated<Medicine>('/medicines', { params }),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  })
}

/** Add a medicine to the catalog. 409 when the SKU is already used in this hospital. */
export function useCreateMedicine() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateMedicineInput) => http.post<Medicine>('/medicines', input),
    onSuccess: () => qc.invalidateQueries({ queryKey: pharmacyKeys.medicines() }),
  })
}

/** Edit a catalog medicine. A new price applies to future dispenses only. */
export function useUpdateMedicine(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateMedicineInput) => http.patch<Medicine>(`/medicines/${id}`, input),
    onSuccess: () => {
      // The stock response embeds the medicine, and a purchase order shows its
      // lines' current name and SKU rather than a copy taken when it was drafted.
      qc.invalidateQueries({ queryKey: pharmacyKeys.stockFor(id) })
      qc.invalidateQueries({ queryKey: pharmacyKeys.purchaseOrders() })
      return qc.invalidateQueries({ queryKey: pharmacyKeys.medicines() })
    },
  })
}

// ── Stock ───────────────────────────────────────────────────────────────────

/**
 * A medicine with its stock totals and batches. Needs `pharmacy.batch.read` —
 * a doctor, who can read the catalog, cannot read stock.
 */
export function useMedicineStock(medicineId: string | undefined, options: ListQueryOptions = {}) {
  return useQuery<MedicineStock>({
    enabled: (options.enabled ?? true) && !!medicineId,
    queryKey: pharmacyKeys.stockFor(medicineId ?? ''),
    queryFn: () => http.get<MedicineStock>(`/medicines/${medicineId}/stock`),
    // A dispense at another counter changes this at any moment.
    staleTime: 0,
  })
}

/**
 * Shared plumbing for the three writes on a medicine's stock. Each changes what
 * can be dispensed, so every stock query and every prescription (whose lines
 * carry `available_quantity`) is refetched.
 */
function useStockWrite<TInput>(medicineId: string, call: (input: TInput) => Promise<MedicineBatch>) {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: pharmacyKeys.prescriptions() })
    return qc.invalidateQueries({ queryKey: pharmacyKeys.stock() })
  }
  return useMutation({
    mutationFn: call,
    onSuccess: refresh,
    onError: (err) => {
      if (isStale(err)) qc.invalidateQueries({ queryKey: pharmacyKeys.stockFor(medicineId) })
    },
  })
}

/** Take stock in directly (`POST /medicines/{id}/batches`). */
export function useReceiveBatch(medicineId: string) {
  return useStockWrite<ReceiveBatchInput>(medicineId, (input) =>
    http.post<MedicineBatch>(`/medicines/${medicineId}/batches`, input),
  )
}

/** Recall a batch, or lift a recall (`PATCH /medicines/{id}/batches/{batch_id}`). */
export function useSetBatchRecalled(medicineId: string) {
  return useStockWrite<{ batchId: string; is_recalled: boolean }>(medicineId, ({ batchId, is_recalled }) =>
    http.patch<MedicineBatch>(`/medicines/${medicineId}/batches/${batchId}`, { is_recalled }),
  )
}

/** Correct a batch's count, with a reason and a note for the ledger. */
export function useAdjustStock(medicineId: string) {
  return useStockWrite<AdjustStockInput & { batchId: string }>(medicineId, ({ batchId, ...body }) =>
    http.post<MedicineBatch>(`/medicines/${medicineId}/batches/${batchId}/adjust`, body),
  )
}

// ── Prescriptions ───────────────────────────────────────────────────────────

/** Every prescription, newest first. Needs `pharmacy.prescription.read`. */
export function usePrescriptions(params: PrescriptionListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<Prescription>>({
    enabled: options.enabled ?? true,
    queryKey: pharmacyKeys.prescriptionList(params),
    queryFn: () => http.getPaginated<Prescription>('/prescriptions', { params }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

/**
 * The dispensing queue: active and partially dispensed, longest-waiting first.
 * The endpoint takes paging only — a patient or doctor filter sent here is
 * silently ignored, so filtered views use {@link usePrescriptions}.
 */
export function usePendingPrescriptions(
  params: { page?: number; page_size?: number } = {},
  options: ListQueryOptions = {},
) {
  return useQuery<Paginated<Prescription>>({
    enabled: options.enabled ?? true,
    queryKey: pharmacyKeys.pending(params),
    queryFn: () => http.getPaginated<Prescription>('/prescriptions/pending', { params }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

/** One prescription with its lines and what is available for each today. */
export function usePrescription(id: string | undefined) {
  return useQuery<Prescription>({
    enabled: !!id,
    queryKey: pharmacyKeys.prescription(id ?? ''),
    queryFn: () => http.get<Prescription>(`/prescriptions/${id}`),
    staleTime: 15_000,
  })
}

/** A prescription's dispenses, oldest first. */
export function usePrescriptionDispenses(id: string | undefined, options: ListQueryOptions = {}) {
  return useQuery<Dispense[]>({
    enabled: (options.enabled ?? true) && !!id,
    queryKey: pharmacyKeys.dispenses(id ?? ''),
    queryFn: () => http.get<Dispense[]>(`/prescriptions/${id}/dispenses`),
    staleTime: 15_000,
  })
}

/** Write a prescription for a visit (§9.4). Nothing is charged until it is dispensed. */
export function useCreatePrescription() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreatePrescriptionInput) => http.post<Prescription>('/prescriptions', input),
    onSuccess: (prescription) => {
      qc.setQueryData(pharmacyKeys.prescription(prescription.id), prescription)
      return qc.invalidateQueries({ queryKey: pharmacyKeys.prescriptions() })
    },
  })
}

/** Cancel a prescription nothing has been dispensed from. A reason is required. */
export function useCancelPrescription(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (reason: string) => http.post<Prescription>(`/prescriptions/${id}/cancel`, { reason }),
    onSuccess: (prescription) => {
      qc.setQueryData(pharmacyKeys.prescription(id), prescription)
      return qc.invalidateQueries({ queryKey: pharmacyKeys.prescriptions() })
    },
    onError: (err) => {
      if (isStale(err)) qc.invalidateQueries({ queryKey: pharmacyKeys.prescriptions() })
    },
  })
}

/**
 * Dispense a prescription (§9.5).
 *
 * The server picks the batches (first to expire first), prices the lines and
 * charges them to the visit's draft invoice in the same transaction — none of
 * that is done here. After a dispense the prescription, every stock figure and
 * the invoice lists are refetched. A refusal (a shortage is a 409 that
 * dispenses nothing) means the availability on screen was out of date, so the
 * prescription and stock are refetched then too.
 */
export function useDispensePrescription(id: string) {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: pharmacyKeys.stock() })
    return qc.invalidateQueries({ queryKey: pharmacyKeys.prescriptions() })
  }
  return useMutation({
    mutationFn: (input: DispenseInput) =>
      // No body at all dispenses everything outstanding.
      http.post<Dispense>(`/prescriptions/${id}/dispense`, input.items || input.notes ? input : undefined),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: billingKeys.all })
      return refresh()
    },
    onError: (err) => {
      // A 422 here can mean the outstanding quantity changed since the screen loaded.
      if (isStale(err) || (err instanceof ApiError && err.status === 422)) refresh()
    },
  })
}

// ── Vendors ─────────────────────────────────────────────────────────────────

/** Vendors. Needs `pharmacy.vendor.read`. For a picker pass `is_active: true`. */
export function useVendors(params: VendorListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<Vendor>>({
    enabled: options.enabled ?? true,
    queryKey: pharmacyKeys.vendorList(params),
    queryFn: () => http.getPaginated<Vendor>('/vendors', { params }),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  })
}

/** Add a vendor. 409 when the name is already used in this hospital. */
export function useCreateVendor() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateVendorInput) => http.post<Vendor>('/vendors', input),
    onSuccess: () => qc.invalidateQueries({ queryKey: pharmacyKeys.vendors() }),
  })
}

/** Edit a vendor, or switch it off. There is no delete. */
export function useUpdateVendor(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateVendorInput) => http.patch<Vendor>(`/vendors/${id}`, input),
    onSuccess: () => {
      // An order repeats its vendor's name.
      qc.invalidateQueries({ queryKey: pharmacyKeys.purchaseOrders() })
      return qc.invalidateQueries({ queryKey: pharmacyKeys.vendors() })
    },
  })
}

// ── Purchase orders ─────────────────────────────────────────────────────────

/** Purchase orders. Needs `pharmacy.po.read`. */
export function usePurchaseOrders(params: PurchaseOrderListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<PurchaseOrder>>({
    enabled: options.enabled ?? true,
    queryKey: pharmacyKeys.purchaseOrderList(params),
    queryFn: () => http.getPaginated<PurchaseOrder>('/purchase-orders', { params }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

/** One purchase order with its lines. */
export function usePurchaseOrder(id: string | undefined) {
  return useQuery<PurchaseOrder>({
    enabled: !!id,
    queryKey: pharmacyKeys.purchaseOrder(id ?? ''),
    queryFn: () => http.get<PurchaseOrder>(`/purchase-orders/${id}`),
    staleTime: 15_000,
  })
}

/** Draft a purchase order. It cannot be edited afterwards — cancel it and draft another. */
export function useCreatePurchaseOrder() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreatePurchaseOrderInput) => http.post<PurchaseOrder>('/purchase-orders', input),
    onSuccess: (order) => {
      qc.setQueryData(pharmacyKeys.purchaseOrder(order.id), order)
      return qc.invalidateQueries({ queryKey: pharmacyKeys.purchaseOrders() })
    },
  })
}

/**
 * Shared plumbing for the steps on one purchase order. Each answers with the
 * whole order, which goes into the detail cache; the lists are refetched. A
 * refusal that means the order had already moved on refetches it too.
 */
function usePurchaseOrderStep<TInput>(
  id: string,
  call: (input: TInput) => Promise<PurchaseOrder>,
  after?: () => void,
) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: call,
    onSuccess: (order) => {
      qc.setQueryData(pharmacyKeys.purchaseOrder(id), order)
      after?.()
      return qc.invalidateQueries({ queryKey: pharmacyKeys.purchaseOrders() })
    },
    onError: (err) => {
      if (isStale(err)) qc.invalidateQueries({ queryKey: pharmacyKeys.purchaseOrders() })
    },
  })
}

/** Mark a draft as sent (`draft → sent`). Records the status and time only; nothing is transmitted to the vendor. */
export function useSendPurchaseOrder(id: string) {
  return usePurchaseOrderStep<void>(id, () => http.post<PurchaseOrder>(`/purchase-orders/${id}/send`))
}

/** Cancel an order that has not been received. */
export function useCancelPurchaseOrder(id: string) {
  return usePurchaseOrderStep<void>(id, () => http.post<PurchaseOrder>(`/purchase-orders/${id}/cancel`))
}

/**
 * Receive a sent order (`sent → received`). Every batch becomes stock at once,
 * so stock and prescription availability are refetched. An order is received
 * once, in one go.
 */
export function useReceivePurchaseOrder(id: string) {
  const qc = useQueryClient()
  return usePurchaseOrderStep<ReceiptLineInput[]>(
    id,
    (items) => http.post<PurchaseOrder>(`/purchase-orders/${id}/receive`, { items }),
    () => {
      qc.invalidateQueries({ queryKey: pharmacyKeys.stock() })
      qc.invalidateQueries({ queryKey: pharmacyKeys.prescriptions() })
    },
  )
}
