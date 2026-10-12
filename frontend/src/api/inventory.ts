import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import { notificationKeys } from '@/api/notifications'
import { ApiError, type ListQueryOptions, type Paginated } from '@/api/types'

/**
 * Inventory API — typed hooks over the real backend contract
 * (`backend/app/api/v1/inventory.py`, `backend/app/schemas/inventory.py`,
 * `backend/app/services/inventory_service.py`, `inventory_po_service.py`;
 * docs/18-API_CONTRACTS.md §10). All 19 routes live under `/inventory`.
 *
 * Not built on the server, so not here: forecasting (`GET /inventory/forecast`
 * is a 404, §10.8), deleting an item or a location, editing or deleting a
 * purchase order, a reorder point per location, stock counts, and any charge
 * to a patient's bill.
 *
 * Quantities and money are decimal strings on the wire ("300.00"), in both
 * directions — some consumables are issued in fractions of a unit. Nothing here
 * turns one into a number.
 *
 * None of these writes takes an `Idempotency-Key`, so nothing here retries a
 * write and every screen that calls one guards against a second submit.
 */

// ── Items (§10.3) ───────────────────────────────────────────────────────────

/** An inventory item (`InventoryItemResponse`). */
export interface InventoryItem {
  id: string
  /** Stock code. Uppercased by the server, unique per hospital, never changed. */
  sku: string
  name: string
  /** Free-form grouping, e.g. "Disposables". */
  category: string | null
  /** What one unit is, e.g. "piece" or "box of 100". */
  unit_of_measure: string
  /** Whether stock is kept per batch with an expiry. Fixed when the item is created. */
  is_batch_tracked: boolean
  /** Usable stock at or below this is low. A whole number; null means the item never alerts. */
  reorder_point: number | null
  /** The level a reorder should restore. A whole number, never below the reorder point. */
  target_stock: number | null
  /** An inactive item cannot be consumed, transferred or ordered. */
  is_active: boolean
  created_at: string
  updated_at: string
}

/**
 * The part of an item every screen that names one already holds — a stock row
 * and an order line both carry it — enough to show an item as chosen without
 * fetching it.
 */
export type InventoryItemRef = Pick<InventoryItem, 'id' | 'sku' | 'name' | 'unit_of_measure'>

/** Body for `POST /inventory/items`. Unknown fields are a 422. */
export interface CreateInventoryItemInput {
  sku: string
  name: string
  category?: string | null
  /** Defaults to "unit" on the server when left out. */
  unit_of_measure?: string
  is_batch_tracked?: boolean
  reorder_point?: number | null
  target_stock?: number | null
}

/**
 * Body for `PATCH /inventory/items/{id}`. The SKU and `is_batch_tracked` cannot
 * be changed. `name`, `unit_of_measure` and `is_active` may be left out but not
 * sent as null; `category`, `reorder_point` and `target_stock` may be cleared
 * with null.
 */
export interface UpdateInventoryItemInput {
  name?: string
  category?: string | null
  unit_of_measure?: string
  reorder_point?: number | null
  target_stock?: number | null
  is_active?: boolean
}

export interface InventoryItemListParams {
  /** The start of the name, or an exact SKU. */
  q?: string
  /** An exact category. */
  category?: string
  is_active?: boolean
  page?: number
  page_size?: number
}

// ── Locations (§10.3) ───────────────────────────────────────────────────────

export type InventoryLocationKind = 'ward' | 'ot' | 'icu' | 'store'

/** A place that holds stock (`LocationResponse`). It carries no timestamps. */
export interface InventoryLocation {
  id: string
  name: string
  /** Short code. Uppercased by the server, unique per hospital, never changed. */
  code: string
  kind: InventoryLocationKind
  /** Stock can be used up in or moved out of an inactive location, but not sent to one. */
  is_active: boolean
}

/** Body for `POST /inventory/locations`. `kind` defaults to `store` on the server. */
export interface CreateInventoryLocationInput {
  name: string
  code: string
  kind?: InventoryLocationKind
}

/** Body for `PATCH /inventory/locations/{id}`. The code cannot be changed; no field may be null. */
export interface UpdateInventoryLocationInput {
  name?: string
  kind?: InventoryLocationKind
  is_active?: boolean
}

export interface InventoryLocationListParams {
  is_active?: boolean
}

// ── Stock and the ledger (§10.4, §10.5) ─────────────────────────────────────

/** One batch of one item at one location (`StockRowResponse`). */
export interface StockRow {
  id: string
  item_id: string
  item_sku: string
  item_name: string
  unit_of_measure: string
  location_id: string
  location_code: string
  location_name: string
  /** Null for an item that is not batch-tracked. */
  batch_number: string | null
  /** `YYYY-MM-DD`: the last day the batch may be used. Null when there is none. */
  expiry_date: string | null
  /** Units held, a decimal string. */
  quantity: string
  /**
   * Past its expiry on the hospital's local date: still on the shelf, never
   * used. The server's judgement — do not compare dates in the browser.
   */
  is_expired: boolean
}

export interface StockListParams {
  item_id?: string
  location_id?: string
  /** The server's default is true: rows holding nothing are left out. */
  in_stock_only?: boolean
  page?: number
  page_size?: number
}

/**
 * An active item's hospital-wide stock against its reorder point
 * (`ItemStockSummaryResponse`). `is_low` and `suggested_order_quantity` are the
 * server's — neither is worked out in the browser.
 */
export interface ItemStockSummary {
  item: InventoryItem
  /** Units held anywhere, in any state. A decimal string. */
  quantity_on_hand: string
  /** Units held that have not expired — the figure to show as "in stock". */
  usable_quantity: string
  /** Usable stock is at or below the item's reorder point. */
  is_low: boolean
  /** What would bring usable stock back to the target. "0.00" when not low. */
  suggested_order_quantity: string
}

export interface StockSummaryParams {
  /** Only items at or below their reorder point. */
  low_stock?: boolean
  /** The start of the name, or an exact SKU. */
  q?: string
  /** An exact category. */
  category?: string
  page?: number
  page_size?: number
}

export type MovementReason =
  | 'received'
  | 'consumed'
  | 'transferred_in'
  | 'transferred_out'
  | 'adjusted'
  | 'expired'

/**
 * One entry in the stock ledger (`MovementResponse`). It names its item and
 * location by id only — a screen that shows names looks them up.
 */
export interface StockMovement {
  id: string
  item_id: string
  location_id: string
  stock_id: string
  batch_number: string | null
  /** Signed decimal string: positive adds units, negative removes them. */
  quantity_change: string
  reason: MovementReason
  /** The department that used it, on a consume that named one. */
  department_id: string | null
  /** What caused it, e.g. "adjustment". */
  reference_type: string | null
  /** The two halves of one transfer share this. */
  reference_id: string | null
  note: string | null
  moved_at: string
  moved_by: string | null
}

export interface MovementListParams {
  item_id?: string
  location_id?: string
  reason?: MovementReason
  page?: number
  page_size?: number
}

/**
 * Body for `POST /inventory/consume`. Stock is taken earliest expiry first and
 * never from an expired batch; `batch_number` restricts it to one batch.
 */
export interface ConsumeStockInput {
  item_id: string
  location_id: string
  /** Positive, at most two decimal places. */
  quantity: string
  department_id?: string
  batch_number?: string
  note?: string
}

/** Body for `POST /inventory/transfer`. The two locations must differ (a 422 otherwise). */
export interface TransferStockInput {
  item_id: string
  from_location_id: string
  to_location_id: string
  /** Positive, at most two decimal places. */
  quantity: string
  batch_number?: string
  note?: string
}

/** The reasons `POST /inventory/adjust` accepts. No other ledger reason can be sent. */
export type AdjustStockReason = 'adjusted' | 'expired'

/**
 * Body for `POST /inventory/adjust`. A batch-tracked item needs `batch_number`
 * and an untracked one must not have it (422 either way). A positive change may
 * create the stock row — how an opening balance is entered — and `expiry_date`
 * is the expiry of a batch created that way.
 */
export interface AdjustStockInput {
  item_id: string
  location_id: string
  /** Signed: negative removes units, positive adds them. Never zero; never positive for `expired`. */
  quantity_change: string
  reason: AdjustStockReason
  /** Required: why the count is changing. */
  note: string
  batch_number?: string
  expiry_date?: string
}

/**
 * What consume, transfer and adjust answer with (`StockChangeResponse`): the
 * ledger entries the request wrote and the item's stock afterwards. Show these
 * as the result — `summary.is_low` tells the person who made the movement.
 */
export interface StockChange {
  movements: StockMovement[]
  summary: ItemStockSummary
}

/** What the server found when it refused a consume or transfer for lack of stock. */
export interface StockShortage {
  /** Units asked for, a decimal string. */
  requested: string
  /** Usable units at that location, a decimal string. */
  available: string
}

/**
 * The shortage named by a refused consume or transfer. The API answers 409
 * with `errors.requested` and `errors.available` and changes nothing (§10.5);
 * any other failure, or a 409 of another shape, yields null.
 */
export function stockShortageOf(err: unknown): StockShortage | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const detail = err.details as Partial<StockShortage> | null | undefined
  if (typeof detail?.requested !== 'string' || typeof detail?.available !== 'string') return null
  return { requested: detail.requested, available: detail.available }
}

// ── Purchase orders (§10.7) ─────────────────────────────────────────────────

/** The same lifecycle as Pharmacy's orders (§9.8), but separate orders, numbered `IPO-…`. */
export type InventoryOrderStatus = 'draft' | 'sent' | 'received' | 'cancelled'

/** One ordered line (`InventoryPurchaseOrderItemResponse`). */
export interface InventoryOrderItem {
  id: string
  item_id: string
  item_sku: string
  item_name: string
  /** Units ordered, a decimal string. What arrived is not reported here. */
  quantity: string
  /** Purchase price per unit, a decimal string. */
  unit_price: string
  /** Line total, a decimal string — worked out by the server. */
  total: string
}

/** An inventory purchase order (`InventoryPurchaseOrderResponse`). */
export interface InventoryOrder {
  id: string
  po_number: string
  /** One of Pharmacy's vendors (`GET /vendors`, §9.7). */
  vendor_id: string
  vendor_name: string
  status: InventoryOrderStatus
  notes: string | null
  /** When it was sent. */
  ordered_at: string | null
  received_at: string | null
  /** The location the goods went into. Null until received. */
  received_location_id: string | null
  /** Sum of the lines, a decimal string — worked out by the server. */
  total_amount: string
  created_at: string
  items: InventoryOrderItem[]
}

/** One line of `POST /inventory/purchase-orders`. */
export interface InventoryOrderLineInput {
  item_id: string
  /** Positive, at most two decimal places. */
  quantity: string
  /** Zero or more, at most two decimal places. */
  unit_price: string
}

/** Body for `POST /inventory/purchase-orders`. Creates a draft of 1–50 lines; an item may appear once. */
export interface CreateInventoryOrderInput {
  vendor_id: string
  notes?: string | null
  items: InventoryOrderLineInput[]
}

export interface InventoryOrderListParams {
  status?: InventoryOrderStatus
  vendor_id?: string
  page?: number
  page_size?: number
}

/**
 * One received line of `POST /inventory/purchase-orders/{id}/receive`. An order
 * line may be split across batches, each batch listed once per line. A
 * batch-tracked item needs `batch_number`; an untracked one must not have it.
 */
export interface InventoryReceiptLineInput {
  po_item_id: string
  /** Positive, at most two decimal places. It may differ from what was ordered. */
  quantity: string
  batch_number?: string
  expiry_date?: string
}

/** Body for `POST /inventory/purchase-orders/{id}/receive`. Everything goes into one active location. */
export interface ReceiveInventoryOrderInput {
  location_id: string
  items: InventoryReceiptLineInput[]
}

// ── Query keys ──────────────────────────────────────────────────────────────

export const inventoryKeys = {
  all: ['inventory'] as const,
  items: () => [...inventoryKeys.all, 'items'] as const,
  itemList: (params: InventoryItemListParams) => [...inventoryKeys.items(), 'list', params] as const,
  item: (id: string) => [...inventoryKeys.items(), 'detail', id] as const,
  locations: () => [...inventoryKeys.all, 'locations'] as const,
  locationList: (params: InventoryLocationListParams) =>
    [...inventoryKeys.locations(), 'list', params] as const,
  /** Every stock-row query: rows change with any movement or receipt. */
  stock: () => [...inventoryKeys.all, 'stock'] as const,
  stockList: (params: StockListParams) => [...inventoryKeys.stock(), 'list', params] as const,
  /** Kept apart from `stock`: the per-item totals are a different endpoint. */
  summary: () => [...inventoryKeys.all, 'summary'] as const,
  summaryList: (params: StockSummaryParams) => [...inventoryKeys.summary(), 'list', params] as const,
  movements: () => [...inventoryKeys.all, 'movements'] as const,
  movementList: (params: MovementListParams) => [...inventoryKeys.movements(), 'list', params] as const,
  purchaseOrders: () => [...inventoryKeys.all, 'purchase-orders'] as const,
  purchaseOrderList: (params: InventoryOrderListParams) =>
    [...inventoryKeys.purchaseOrders(), 'list', params] as const,
  purchaseOrder: (id: string) => [...inventoryKeys.purchaseOrders(), 'detail', id] as const,
}

/**
 * The server rejected a write because the record was not in the state the
 * screen showed (already sent, already received, gone, the shelf emptier than
 * listed). What is on screen is stale, so it is refetched even though the call
 * failed.
 */
function isStale(err: unknown): boolean {
  return err instanceof ApiError && [400, 404, 409].includes(err.status ?? 0)
}

// ── Items ───────────────────────────────────────────────────────────────────

/** The item catalog, by name. Needs `inventory.item.read`. For a picker pass `is_active: true`. */
export function useItems(params: InventoryItemListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<InventoryItem>>({
    enabled: options.enabled ?? true,
    queryKey: inventoryKeys.itemList(params),
    queryFn: () => http.getPaginated<InventoryItem>('/inventory/items', { params }),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  })
}

/** One item. Needs `inventory.item.read`. */
export function useItem(id: string | undefined, options: ListQueryOptions = {}) {
  return useQuery<InventoryItem>({
    enabled: (options.enabled ?? true) && !!id,
    queryKey: inventoryKeys.item(id ?? ''),
    queryFn: () => http.get<InventoryItem>(`/inventory/items/${id}`),
    staleTime: 30_000,
  })
}

/** Add an item. Needs `inventory.item.create`. 409 when the SKU is already used in this hospital. */
export function useCreateItem() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateInventoryItemInput) => http.post<InventoryItem>('/inventory/items', input),
    onSuccess: (item) => {
      qc.setQueryData(inventoryKeys.item(item.id), item)
      // A new active item appears in the per-item summary at once, holding nothing.
      qc.invalidateQueries({ queryKey: inventoryKeys.summary() })
      return qc.invalidateQueries({ queryKey: inventoryKeys.items() })
    },
  })
}

/**
 * Edit an item, or switch it off. Needs `inventory.item.update`. A target
 * below the reorder point is a 422 naming `target_stock`.
 */
export function useUpdateItem(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateInventoryItemInput) =>
      http.patch<InventoryItem>(`/inventory/items/${id}`, input),
    onSuccess: (item) => {
      qc.setQueryData(inventoryKeys.item(id), item)
      // A stock row and an order line repeat the item's current name and unit,
      // and the summary both embeds the item and judges `is_low` by its reorder
      // point — and lists active items only.
      qc.invalidateQueries({ queryKey: inventoryKeys.stock() })
      qc.invalidateQueries({ queryKey: inventoryKeys.summary() })
      qc.invalidateQueries({ queryKey: inventoryKeys.purchaseOrders() })
      return qc.invalidateQueries({ queryKey: inventoryKeys.items() })
    },
    onError: (err) => {
      if (isStale(err)) qc.invalidateQueries({ queryKey: inventoryKeys.items() })
    },
  })
}

// ── Locations ───────────────────────────────────────────────────────────────

/**
 * The hospital's stock locations, by name. Needs `inventory.location.read`.
 * The endpoint is not paginated: the answer is the whole list. For a picker of
 * places stock can be sent to pass `is_active: true`.
 */
export function useLocations(params: InventoryLocationListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<InventoryLocation[]>({
    enabled: options.enabled ?? true,
    queryKey: inventoryKeys.locationList(params),
    queryFn: () => http.get<InventoryLocation[]>('/inventory/locations', { params }),
    staleTime: 30_000,
  })
}

/** Add a location. Needs `inventory.location.create`. 409 when the code is already used in this hospital. */
export function useCreateLocation() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateInventoryLocationInput) =>
      http.post<InventoryLocation>('/inventory/locations', input),
    onSuccess: () => qc.invalidateQueries({ queryKey: inventoryKeys.locations() }),
  })
}

/** Edit a location, or switch it off. Needs `inventory.location.update`. There is no delete. */
export function useUpdateLocation(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateInventoryLocationInput) =>
      http.patch<InventoryLocation>(`/inventory/locations/${id}`, input),
    onSuccess: () => {
      // A stock row repeats its location's current name.
      qc.invalidateQueries({ queryKey: inventoryKeys.stock() })
      return qc.invalidateQueries({ queryKey: inventoryKeys.locations() })
    },
    onError: (err) => {
      if (isStale(err)) qc.invalidateQueries({ queryKey: inventoryKeys.locations() })
    },
  })
}

// ── Stock ───────────────────────────────────────────────────────────────────

/**
 * Stock rows — one per batch of an item at a location — ordered by item,
 * location, then expiry. Needs `inventory.stock.read`.
 */
export function useStock(params: StockListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<StockRow>>({
    enabled: options.enabled ?? true,
    queryKey: inventoryKeys.stockList(params),
    queryFn: () => http.getPaginated<StockRow>('/inventory/stock', { params }),
    // Another ward recording what it used changes this at any moment.
    staleTime: 0,
    placeholderData: keepPreviousData,
  })
}

/**
 * Each active item's hospital-wide stock against its reorder point. Needs
 * `inventory.stock.read`. `low_stock: true` is the reorder list: only items the
 * server judges low, each with its suggested order quantity.
 */
export function useStockSummary(params: StockSummaryParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<ItemStockSummary>>({
    enabled: options.enabled ?? true,
    queryKey: inventoryKeys.summaryList(params),
    queryFn: () => http.getPaginated<ItemStockSummary>('/inventory/stock/summary', { params }),
    staleTime: 0,
    placeholderData: keepPreviousData,
  })
}

/** The stock ledger, newest first. Needs `inventory.stock.read`. Every change to stock is one entry. */
export function useMovements(params: MovementListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<StockMovement>>({
    enabled: options.enabled ?? true,
    queryKey: inventoryKeys.movementList(params),
    queryFn: () => http.getPaginated<StockMovement>('/inventory/movements', { params }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

/**
 * Shared plumbing for the three writes on stock. Each answers with the ledger
 * entries it wrote and the item's stock afterwards, and changes the stock
 * rows, the per-item summary and the ledger, so all three are refetched. A
 * refusal — a shortage is a 409 that changes nothing, a vanished item or
 * location a 422 — means the figures on screen were out of date, so the rows
 * and the summary are refetched then too.
 */
function useStockWrite<TInput>(path: string, alerts: boolean) {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: inventoryKeys.summary() })
    return qc.invalidateQueries({ queryKey: inventoryKeys.stock() })
  }
  return useMutation({
    mutationFn: (input: TInput) => http.post<StockChange>(path, input),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: inventoryKeys.movements() })
      // Crossing the reorder point raises `inventory.low_stock` for the people
      // who can order — who may be the person who just made the movement (§10.6).
      if (alerts) qc.invalidateQueries({ queryKey: notificationKeys.all })
      return refresh()
    },
    onError: () => {
      // A refusal means the screen may be stale; a 5xx or a dropped connection
      // means the write may have landed. Either way, read stock and the ledger again.
      qc.invalidateQueries({ queryKey: inventoryKeys.movements() })
      refresh()
    },
  })
}

/**
 * Record units used at a location (`POST /inventory/consume`, §10.5). Needs
 * `inventory.consume`. If the location does not hold enough usable stock the
 * answer is a 409 and nothing changes — see {@link stockShortageOf}.
 */
export function useConsumeStock() {
  return useStockWrite<ConsumeStockInput>('/inventory/consume', true)
}

/**
 * Move units from one location to another (`POST /inventory/transfer`). Needs
 * `inventory.transfer`. Batches keep their number and expiry; the answer holds
 * two ledger entries per batch moved. A short source is a 409 that changes
 * nothing; an inactive destination is a 400. A transfer never raises a
 * low-stock alert: nothing leaves the hospital.
 */
export function useTransferStock() {
  return useStockWrite<TransferStockInput>('/inventory/transfer', false)
}

/**
 * Correct a count or write off expired stock (`POST /inventory/adjust`). Needs
 * `inventory.adjust`. Taking a stock row below zero is a 400.
 */
export function useAdjustStock() {
  return useStockWrite<AdjustStockInput>('/inventory/adjust', true)
}

// ── Purchase orders ─────────────────────────────────────────────────────────

/** Inventory purchase orders, newest first. Needs `inventory.po.read`. */
export function useInventoryOrders(params: InventoryOrderListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<InventoryOrder>>({
    enabled: options.enabled ?? true,
    queryKey: inventoryKeys.purchaseOrderList(params),
    queryFn: () => http.getPaginated<InventoryOrder>('/inventory/purchase-orders', { params }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

/** One purchase order with its lines. Needs `inventory.po.read`. */
export function useInventoryOrder(id: string | undefined, options: ListQueryOptions = {}) {
  return useQuery<InventoryOrder>({
    enabled: (options.enabled ?? true) && !!id,
    queryKey: inventoryKeys.purchaseOrder(id ?? ''),
    queryFn: () => http.get<InventoryOrder>(`/inventory/purchase-orders/${id}`),
    staleTime: 15_000,
  })
}

/**
 * Draft a purchase order. Needs `inventory.po.create` — and choosing its vendor
 * needs `pharmacy.vendor.read`, since vendors are Pharmacy's. It cannot be
 * edited afterwards — cancel it and draft another.
 */
export function useCreateInventoryOrder() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: CreateInventoryOrderInput) =>
      http.post<InventoryOrder>('/inventory/purchase-orders', input),
    onSuccess: (order) => {
      qc.setQueryData(inventoryKeys.purchaseOrder(order.id), order)
      return qc.invalidateQueries({ queryKey: inventoryKeys.purchaseOrders() })
    },
  })
}

/**
 * Shared plumbing for the steps on one purchase order. Each answers with the
 * whole order, which goes into the detail cache; the lists are refetched. A
 * refusal that means the order had already moved on refetches it too.
 */
function useInventoryOrderStep<TInput>(
  id: string,
  call: (input: TInput) => Promise<InventoryOrder>,
  after?: () => void,
) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: call,
    onSuccess: (order) => {
      qc.setQueryData(inventoryKeys.purchaseOrder(id), order)
      after?.()
      return qc.invalidateQueries({ queryKey: inventoryKeys.purchaseOrders() })
    },
    onError: (err) => {
      if (isStale(err)) qc.invalidateQueries({ queryKey: inventoryKeys.purchaseOrders() })
    },
  })
}

/**
 * Mark a draft as sent (`draft → sent`). Needs `inventory.po.update`. Records
 * the status and time only; nothing is transmitted to the vendor.
 */
export function useSendInventoryOrder(id: string) {
  return useInventoryOrderStep<void>(id, () =>
    http.post<InventoryOrder>(`/inventory/purchase-orders/${id}/send`),
  )
}

/** Cancel a draft or sent order. Needs `inventory.po.update`. A received order cannot be cancelled. */
export function useCancelInventoryOrder(id: string) {
  return useInventoryOrderStep<void>(id, () =>
    http.post<InventoryOrder>(`/inventory/purchase-orders/${id}/cancel`),
  )
}

/**
 * Receive a sent order into one location (`sent → received`). Needs
 * `inventory.po.receive`. Every line becomes stock at once, so the stock rows,
 * the summary and the ledger are refetched. An order is received once, in one
 * go: a line left off the receipt can never be received later.
 */
export function useReceiveInventoryOrder(id: string) {
  const qc = useQueryClient()
  return useInventoryOrderStep<ReceiveInventoryOrderInput>(
    id,
    (input) => http.post<InventoryOrder>(`/inventory/purchase-orders/${id}/receive`, input),
    () => {
      qc.invalidateQueries({ queryKey: inventoryKeys.stock() })
      qc.invalidateQueries({ queryKey: inventoryKeys.summary() })
      qc.invalidateQueries({ queryKey: inventoryKeys.movements() })
    },
  )
}
