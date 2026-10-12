import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import { billingKeys } from '@/api/billing'
import { notificationKeys } from '@/api/notifications'
import { ApiError, type ListQueryOptions, type Paginated } from '@/api/types'

/**
 * Laboratory API — typed hooks over the real backend contract
 * (`backend/app/api/v1/tests_catalog.py`, `backend/app/api/v1/lab_orders.py`,
 * `backend/app/schemas/lab.py`; docs/18-API_CONTRACTS.md §8).
 *
 * Not built on the server, so not here: the PDF report and AI explain (§8.8).
 */

export type LabResultType = 'numeric' | 'text'
export type LabOrderPriority = 'routine' | 'urgent' | 'stat'
export type LabOrderStatus =
  | 'ordered'
  | 'collected'
  | 'in_progress'
  | 'results_entered'
  | 'released'
  | 'cancelled'
/** The server's verdict on a numeric result. `null` on an item means no range applied. */
export type LabResultFlag = 'normal' | 'low' | 'high' | 'critical'

/** One band of a test's reference range. Bounds are decimal strings. */
export interface ReferenceRange {
  sex: 'male' | 'female' | 'any'
  age_min?: number | null
  age_max?: number | null
  low?: string | null
  high?: string | null
  critical_low?: string | null
  critical_high?: string | null
}

/** A catalog test (`LabTestResponse`). */
export interface LabTest {
  id: string
  code: string
  name: string
  category: string | null
  unit: string | null
  result_type: LabResultType
  reference_ranges: ReferenceRange[]
  turnaround_hours: number | null
  /** Decimal string. */
  price: string
  is_active: boolean
  created_at: string
  updated_at: string
}

/** Body for `POST /tests-catalog`. `code` and `result_type` cannot be changed afterwards. */
export interface CreateLabTestInput {
  code: string
  name: string
  category?: string | null
  unit?: string | null
  result_type: LabResultType
  reference_ranges: ReferenceRange[]
  turnaround_hours?: number | null
  price: string
}

/** Body for `PATCH /tests-catalog/{id}`. `reference_ranges` replaces the whole list. */
export type UpdateLabTestInput = Partial<Omit<CreateLabTestInput, 'code' | 'result_type'>> & {
  is_active?: boolean
}

export interface LabTestListParams {
  /** Name prefix, or an exact code. */
  q?: string
  /** Exact category. */
  category?: string
  is_active?: boolean
  page?: number
  page_size?: number
}

/** A correction made to a released result. Oldest first on the item. */
export interface LabResultAmendment {
  id: string
  previous_value: string | null
  new_value: string
  previous_flag: LabResultFlag | null
  new_flag: LabResultFlag | null
  reason: string
  amended_by: string
  amended_at: string
}

/** One test on an order (`LabOrderItemResponse`). */
export interface LabOrderItem {
  id: string
  test_id: string
  test_code: string
  test_name: string
  result_type: LabResultType
  price: string
  sample_id: string | null
  sample_collected_at: string | null
  result_value: string | null
  result_unit: string | null
  result_flag: LabResultFlag | null
  /** The bounds the result was judged against, as decimal strings. */
  reference_low: string | null
  reference_high: string | null
  result_entered_at: string | null
  released_at: string | null
  notes: string | null
  amendments: LabResultAmendment[]
}

/** A lab order (`LabOrderResponse`) — the same shape from every order endpoint. */
export interface LabOrder {
  id: string
  appointment_id: string | null
  patient_id: string
  patient_name: string
  patient_mrn: string
  doctor_id: string
  doctor_name: string
  ordered_at: string
  priority: LabOrderPriority
  status: LabOrderStatus
  notes: string | null
  collected_at: string | null
  results_entered_at: string | null
  released_at: string | null
  released_by: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  /** The draft invoice the order's tests were charged to. */
  invoice_id: string | null
  turnaround_minutes: number | null
  has_abnormal: boolean
  has_critical: boolean
  items: LabOrderItem[]
}

export interface LabOrderListParams {
  status?: LabOrderStatus
  priority?: LabOrderPriority
  patient_id?: string
  doctor_id?: string
  appointment_id?: string
  page?: number
  page_size?: number
}

/** Body for `POST /lab-orders`. The patient and doctor come from the appointment. */
export interface CreateLabOrderInput {
  appointment_id: string
  test_ids: string[]
  priority: LabOrderPriority
  notes?: string
}

export interface LabResultInput {
  item_id: string
  value: string
  notes?: string
}

export const labKeys = {
  all: ['lab'] as const,
  tests: () => [...labKeys.all, 'tests'] as const,
  testList: (params: LabTestListParams) => [...labKeys.tests(), 'list', params] as const,
  orders: () => [...labKeys.all, 'orders'] as const,
  orderList: (params: LabOrderListParams) => [...labKeys.orders(), 'list', params] as const,
  order: (id: string) => [...labKeys.orders(), 'detail', id] as const,
}

/** The test catalog, ordered by name. For an order form pass `is_active: true`. */
export function useLabTests(params: LabTestListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<LabTest>>({
    enabled: options.enabled ?? true,
    queryKey: labKeys.testList(params),
    queryFn: () => http.getPaginated<LabTest>('/tests-catalog', { params }),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  })
}

/** Add a test to the catalog. 409 when the code is already used in this hospital. */
export function useCreateLabTest() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateLabTestInput) => http.post<LabTest>('/tests-catalog', input),
    onSuccess: () => qc.invalidateQueries({ queryKey: labKeys.tests() }),
  })
}

/** Edit a catalog test. Never changes an order already placed. */
export function useUpdateLabTest(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateLabTestInput) => http.patch<LabTest>(`/tests-catalog/${id}`, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: labKeys.tests() }),
  })
}

/** The lab worklist, newest first. */
export function useLabOrders(params: LabOrderListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<LabOrder>>({
    enabled: options.enabled ?? true,
    queryKey: labKeys.orderList(params),
    queryFn: () => http.getPaginated<LabOrder>('/lab-orders', { params }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

/** One lab order with its items, results and amendments. */
export function useLabOrder(id: string | undefined) {
  return useQuery<LabOrder>({
    enabled: !!id,
    queryKey: labKeys.order(id ?? ''),
    queryFn: () => http.get<LabOrder>(`/lab-orders/${id}`),
    staleTime: 15_000,
  })
}

/**
 * Shared plumbing for the writes on one order. Each endpoint answers with the
 * whole order, which goes straight into the detail cache so the screen shows
 * the server's status and flags; the lists are refetched because a row repeats
 * them.
 *
 * A 400, 404 or 409 means the order was not in the state the screen showed —
 * someone else collected, released or cancelled it — so the order is refetched
 * even though the call failed.
 */
function useOrderWrite<TInput>(
  orderId: string,
  call: (input: TInput) => Promise<LabOrder>,
  after?: () => void,
) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: call,
    onSuccess: (order) => {
      qc.setQueryData(labKeys.order(orderId), order)
      after?.()
      return qc.invalidateQueries({ queryKey: labKeys.orders() })
    },
    onError: (err) => {
      if (err instanceof ApiError && [400, 404, 409].includes(err.status ?? 0)) {
        qc.invalidateQueries({ queryKey: labKeys.orders() })
      }
    },
  })
}

/**
 * Order tests for a visit (docs/18-API_CONTRACTS.md §8.4).
 *
 * The server charges each test to the visit's draft invoice in the same
 * transaction (§8.7), so the invoice lists are refetched as well — nothing is
 * billed from here.
 */
export function useCreateLabOrder() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateLabOrderInput) => http.post<LabOrder>('/lab-orders', input),
    onSuccess: (order) => {
      qc.setQueryData(labKeys.order(order.id), order)
      qc.invalidateQueries({ queryKey: billingKeys.all })
      return qc.invalidateQueries({ queryKey: labKeys.orders() })
    },
  })
}

/**
 * Collect every outstanding sample on an order. With no body the server
 * generates the sample ids.
 */
export function useCollectSamples(orderId: string) {
  return useOrderWrite<void>(orderId, () => http.post<LabOrder>(`/lab-orders/${orderId}/collect`))
}

/**
 * Enter (or re-enter, before release) results. The server flags each numeric
 * value against the range for the patient; a critical value notifies the
 * ordering doctor at once, so the unread count is refetched.
 */
export function useEnterResults(orderId: string) {
  const qc = useQueryClient()
  return useOrderWrite<LabResultInput[]>(
    orderId,
    (results) => http.post<LabOrder>(`/lab-orders/${orderId}/enter-results`, { results }),
    () => qc.invalidateQueries({ queryKey: notificationKeys.unreadCount }),
  )
}

/** Release a fully-resulted order. The ordering doctor is notified. */
export function useReleaseLabOrder(orderId: string) {
  const qc = useQueryClient()
  return useOrderWrite<void>(
    orderId,
    () => http.post<LabOrder>(`/lab-orders/${orderId}/release`),
    () => qc.invalidateQueries({ queryKey: notificationKeys.unreadCount }),
  )
}

/** Cancel an order that has not been released. A reason is required. */
export function useCancelLabOrder(orderId: string) {
  return useOrderWrite<string>(orderId, (reason) =>
    http.post<LabOrder>(`/lab-orders/${orderId}/cancel`, { reason }),
  )
}

/**
 * Correct a released result (§8.6). The old value is kept in the item's
 * `amendments`, and the ordering doctor is notified.
 */
export function useAmendLabResult(orderId: string) {
  const qc = useQueryClient()
  return useOrderWrite<{ itemId: string; new_value: string; reason: string }>(
    orderId,
    ({ itemId, ...body }) =>
      http.post<LabOrder>(`/lab-orders/${orderId}/items/${itemId}/amend`, body),
    () => qc.invalidateQueries({ queryKey: notificationKeys.unreadCount }),
  )
}
