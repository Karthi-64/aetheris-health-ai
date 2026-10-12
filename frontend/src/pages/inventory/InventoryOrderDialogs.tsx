import { type ReactNode, useCallback, useEffect, useId, useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Controller, useFieldArray, useWatch, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import { z } from 'zod'
import { PackageCheck, PackageX, Plus, RotateCw, Send, Trash2, Undo2, XCircle } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { FormDialog } from '@/components/forms/FormDialog'
import { ItemPicker } from '@/components/inventory/ItemPicker'
import { quantityLabel } from '@/components/inventory/inventoryPresentation'
import {
  inventoryKeys,
  useCancelInventoryOrder,
  useCreateInventoryOrder,
  useItem,
  useLocations,
  useReceiveInventoryOrder,
  useSendInventoryOrder,
  type InventoryItem,
  type InventoryLocation,
  type InventoryOrder,
  type InventoryOrderItem,
  type InventoryOrderStatus,
} from '@/api/inventory'
import { useVendors, type Vendor } from '@/api/pharmacy'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatMoney } from '@/lib/format'
import {
  EMPTY_ORDER,
  EMPTY_ORDER_LINE,
  MAX_ORDER_LINES,
  MAX_RECEIPT_ROWS,
  insertionIndex,
  inventoryOrderSchema,
  lineReceipt,
  receiptDefaults,
  receiptRow,
  receiptSchema,
  shortLines,
  shortfallKey,
  toCreateBody,
  toReceiptBody,
  type InventoryOrderValues,
  type LineReceipt,
  type ReceiptValues,
  type TrackingLookup,
} from './inventoryOrderForm'

const confirmSchema = z.object({})
type ConfirmValues = z.infer<typeof confirmSchema>

/** The most `GET /vendors` returns in one page (§1.6); more is a 422, not a clamp. */
const VENDOR_PAGE = 100

/** Statuses the cancel endpoint accepts (§10.7): anything not yet received. */
const CANCELLABLE: ReadonlySet<InventoryOrderStatus> = new Set(['draft', 'sent'])

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** An item named so that two sizes of one name stay apart. */
const lineName = (line: InventoryOrderItem) => `${line.item_name} (${line.item_sku})`

/**
 * A name of up to 200 characters with no space in it would run out of a
 * dialog's description; this lets it break anywhere instead.
 */
function LongName({ children }: { children: string }) {
  return <span className="[overflow-wrap:anywhere]">{children}</span>
}

// ── Every vendor, not the first hundred ─────────────────────────────────────

const NO_VENDORS: Vendor[] = []
const ACTIVE_VENDORS = { is_active: true } as const
const EVERY_VENDOR = {} as const

interface VendorsLoaded {
  /** Every page loaded so far, in the API's name order. */
  vendors: Vendor[]
  /** True until the last page has answered. */
  isPending: boolean
  isError: boolean
  retry: () => void
}

/**
 * Every vendor the filter matches, handed to `children`. Vendors are
 * Pharmacy's (`GET /vendors`, docs/18-API_CONTRACTS.md §9.7, §10.7): inventory
 * has none of its own, so this asks with `pharmacy.vendor.read` and must be
 * mounted only for a user who holds it.
 *
 * The list gives at most 100 a page and has no search, so a picker that
 * stopped at one page could never reach the 101st vendor. Each page is asked
 * for by one instance of this component, which renders the next instance while
 * the total says there is more; the last one renders `children`. Keep what
 * they render disabled while `isPending`, since it is mounted afresh as each
 * page lands.
 */
function AllVendors({
  filter,
  page = 1,
  loaded = NO_VENDORS,
  children,
}: {
  filter: { is_active?: boolean }
  page?: number
  loaded?: Vendor[]
  children: (state: VendorsLoaded) => ReactNode
}) {
  const { data, isError, refetch } = useVendors({ ...filter, page, page_size: VENDOR_PAGE })
  const vendors = data ? [...loaded, ...data.items] : loaded

  // An empty page ends the walk whatever the total says, so it cannot run on.
  if (data && data.items.length > 0 && vendors.length < data.pagination.total) {
    return (
      <AllVendors filter={filter} page={page + 1} loaded={vendors}>
        {children}
      </AllVendors>
    )
  }
  return children({ vendors, isPending: !data && !isError, isError, retry: () => void refetch() })
}

/**
 * The vendor filter of the order list. Inactive vendors are offered too: their
 * past orders are still there to find. Mounted only for a user who may read
 * the vendor list, because it is what asks for it.
 */
export function InventoryVendorFilter({
  value,
  all,
  onChange,
}: {
  value: string
  all: string
  onChange: (value: string) => void
}) {
  return (
    <AllVendors filter={EVERY_VENDOR}>
      {({ vendors, isPending, isError, retry }) => (
        <>
          <Select value={value} onValueChange={onChange} disabled={isPending}>
            <SelectTrigger aria-label="Filter by vendor" className="w-56 rounded-full py-2.5">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={all}>All vendors</SelectItem>
              {vendors.map((vendor) => (
                <SelectItem key={vendor.id} value={vendor.id}>
                  {vendor.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {/* Without this the filter would sit at "All vendors" with nothing
              to say that the vendors themselves never arrived. */}
          {isError && (
            <Button type="button" variant="outline" size="sm" className="rounded-full" onClick={retry}>
              <RotateCw className="size-4" />
              {vendors.length > 0 ? 'Some vendors didn’t load — retry' : 'Vendors didn’t load — retry'}
            </Button>
          )}
        </>
      )}
    </AllVendors>
  )
}

// ── Drafting ────────────────────────────────────────────────────────────────

/**
 * The vendor an order goes to. Only active vendors are asked for: an inactive
 * one is refused by the API with a 422 on `vendor_id`.
 */
function VendorSelect({ form }: { form: UseFormReturn<InventoryOrderValues> }) {
  return (
    <AllVendors filter={ACTIVE_VENDORS}>
      {({ vendors, isPending, isError, retry }) => (
        <div className="space-y-2">
          <Controller
            control={form.control}
            name="vendor_id"
            render={({ field }) => (
              <Field label="Vendor" required error={form.formState.errors.vendor_id?.message}>
                {(p) => (
                  <Select
                    value={field.value}
                    onValueChange={field.onChange}
                    disabled={isPending || vendors.length === 0}
                  >
                    <SelectTrigger
                      id={p.id}
                      ref={field.ref}
                      aria-invalid={p['aria-invalid']}
                      aria-describedby={p['aria-describedby']}
                    >
                      <SelectValue placeholder={isPending ? 'Loading vendors…' : 'Select a vendor'} />
                    </SelectTrigger>
                    <SelectContent>
                      {vendors.map((vendor) => (
                        <SelectItem key={vendor.id} value={vendor.id}>
                          {vendor.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </Field>
            )}
          />
          {isError ? (
            <div className="flex flex-wrap items-center gap-3">
              <p className="font-body text-body-sm text-on-surface-variant">
                {vendors.length > 0
                  ? 'Not every vendor could be loaded: some are missing from this list.'
                  : "The vendor list couldn't be loaded."}
              </p>
              <Button type="button" variant="outline" size="sm" onClick={retry}>
                <RotateCw className="size-4" /> Retry
              </Button>
            </div>
          ) : !isPending && vendors.length === 0 ? (
            <p className="font-body text-body-sm text-on-surface-variant">
              There is no active vendor to order from. Vendors are shared with Pharmacy: add one, or
              switch one back on, under{' '}
              <Link to="/pharmacy/vendors" className="text-secondary hover:underline">
                Vendors
              </Link>
              .
            </p>
          ) : null}
        </div>
      )}
    </AllVendors>
  )
}

function OrderLines({ form, busy }: { form: UseFormReturn<InventoryOrderValues>; busy: boolean }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: 'items' })
  const lines = useWatch({ control: form.control, name: 'items' })
  const { errors } = form.formState
  const listError = errors.items?.root?.message ?? errors.items?.message
  const full = fields.length >= MAX_ORDER_LINES

  return (
    <div className="space-y-3">
      <p className="font-label text-label-caps text-on-surface-variant">Items to order</p>
      {listError && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}

      {fields.map((field, index) => {
        const e = errors.items?.[index]
        const n = index + 1
        // The API takes an item once per order (a 422 on `items` otherwise).
        const taken = lines.flatMap((line, at) => (at !== index && line.item_id ? [line.item_id] : []))
        return (
          <div
            key={field.id}
            role="group"
            aria-label={`Line ${n}`}
            className="neo-pressed bg-surface min-w-0 space-y-3 rounded-xl p-3"
          >
            <div className="flex items-center justify-between gap-3">
              <p className="font-label text-label-caps text-on-surface-variant">Line {n}</p>
              {/* An order needs at least one line, so the last one cannot go. */}
              {fields.length > 1 && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="text-error hover:text-error"
                  aria-label={`Remove line ${n}`}
                  disabled={busy}
                  onClick={() => remove(index)}
                >
                  <Trash2 className="size-4" /> Remove
                </Button>
              )}
            </div>
            <Controller
              control={form.control}
              name={`items.${index}.item_id`}
              render={({ field: f }) => (
                <Field label="Item" required error={e?.item_id?.message}>
                  {(p) => (
                    // The picker takes no ref, so the form is handed a way to
                    // focus whichever control it is showing. Without one, a
                    // refusal of `items.<i>.item_id` on a long order would
                    // leave the refused line wherever it was, perhaps off-screen.
                    <div
                      ref={(wrapper) => {
                        if (wrapper) {
                          f.ref({ focus: () => wrapper.querySelector<HTMLElement>('input, button')?.focus() })
                        }
                      }}
                    >
                      <ItemPicker
                        id={p.id}
                        value={f.value}
                        invalid={p['aria-invalid']}
                        excludeIds={taken}
                        onChange={(item) => f.onChange(item?.id ?? '')}
                      />
                    </div>
                  )}
                </Field>
              )}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="Quantity"
                required
                hint="In the item's own unit. A fraction such as 2.5 is allowed."
                error={e?.quantity?.message}
              >
                {(p) => (
                  <Input
                    inputMode="decimal"
                    autoComplete="off"
                    {...p}
                    {...form.register(`items.${index}.quantity`)}
                  />
                )}
              </Field>
              {/* Never prefilled: an item carries no price, so there is nothing
                  to copy — it is what the vendor quoted for this order. */}
              <Field
                label="Purchase price per unit"
                required
                hint="As agreed with the vendor. 0 is allowed."
                error={e?.unit_price?.message}
              >
                {(p) => (
                  <Input
                    inputMode="decimal"
                    autoComplete="off"
                    placeholder="0.00"
                    {...p}
                    {...form.register(`items.${index}.unit_price`)}
                  />
                )}
              </Field>
            </div>
          </div>
        )
      })}

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={full || busy}
          onClick={() => append(EMPTY_ORDER_LINE)}
        >
          <Plus className="size-4" /> Add an item
        </Button>
        {full && (
          <p className="font-body text-outline text-xs">An order holds at most {MAX_ORDER_LINES} lines.</p>
        )}
      </div>
      {/* No running total is shown: the server works out each line's total and
          the order's, and the order's own page shows them. */}
      <p className="font-body text-outline text-xs">
        The order's totals are worked out by the server and shown once it is drafted.
      </p>
    </div>
  )
}

/**
 * `busy` is true while the request is in flight. The lines cannot be added or
 * removed then: a refusal names a line by its position in what was sent.
 */
function OrderFields({ form, busy }: { form: UseFormReturn<InventoryOrderValues>; busy: boolean }) {
  return (
    <>
      <VendorSelect form={form} />
      <OrderLines form={form} busy={busy} />
      <Field label="Notes" hint="Optional, e.g. a delivery instruction" error={form.formState.errors.notes?.message}>
        {(p) => <Textarea rows={2} {...p} {...form.register('notes')} />}
      </Field>
    </>
  )
}

function DraftInventoryOrderDialog({ trigger }: { trigger: ReactNode }) {
  const navigate = useNavigate()
  const { can } = usePermissions()
  const create = useCreateInventoryOrder()
  // Cancelling is `inventory.po.update`, which drafting does not require: a
  // role that can draft but not cancel is not told to do what it cannot.
  const wayOut = can('inventory.po.update')
    ? 'to change it, cancel it and draft another'
    : 'to change it, someone who manages purchasing has to cancel it, and another is drafted'

  return (
    <FormDialog<InventoryOrderValues>
      trigger={trigger}
      title="New purchase order"
      description={`Drafts an order for inventory items from one vendor. A draft cannot be edited afterwards — ${wayOut} — so check the lines before you save.`}
      resolver={zodResolver(inventoryOrderSchema)}
      defaults={() => EMPTY_ORDER}
      submitLabel="Draft order"
      pendingLabel="Drafting…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't confirm that the order was drafted. Check the purchase order list before trying again, so it isn't drafted twice."
      onSubmit={async (values) => {
        const order = await create.mutateAsync(toCreateBody(values))
        navigate(`/inventory/purchase-orders/${order.id}`)
        return `Drafted ${order.po_number}`
      }}
    >
      {(form) => <OrderFields form={form} busy={create.isPending} />}
    </FormDialog>
  )
}

/**
 * Draft a purchase order (`POST /inventory/purchase-orders`,
 * docs/18-API_CONTRACTS.md §10.7). The server numbers it (`IPO-…`) and works
 * out its totals.
 *
 * This write has no protection of its own against being sent twice — two
 * requests make two drafts — so the dialog's submit guard is what prevents a
 * duplicate, and a failure the API does not explain is reported as "not
 * confirmed" rather than as "not drafted".
 *
 * The vendor list and the item picker are requests of their own, each with its
 * own permission (`pharmacy.vendor.read`, `inventory.item.read`). The order
 * list offers this only to a user holding both; for anyone else it renders
 * nothing, so neither request can be made on their behalf.
 */
export function CreateInventoryOrderDialog({ trigger }: { trigger: ReactNode }) {
  const { can } = usePermissions()
  if (!can('inventory.po.create') || !can('pharmacy.vendor.read') || !can('inventory.item.read')) return null
  return <DraftInventoryOrderDialog trigger={trigger} />
}

// ── Sending and cancelling ──────────────────────────────────────────────────

/**
 * Mark a draft as sent (`POST /inventory/purchase-orders/{id}/send`, no body).
 * The server only records the status and the time: nothing goes to the vendor,
 * and the dialog says so rather than let "sent" imply it.
 */
export function SendInventoryOrderDialog({ order }: { order: InventoryOrder }) {
  const send = useSendInventoryOrder(order.id)
  return (
    <FormDialog<ConfirmValues>
      trigger={
        <Button size="sm">
          <Send className="size-4" /> Mark as sent
        </Button>
      }
      title="Mark this order as sent?"
      description={
        <>
          Records that {order.po_number} has gone to <LongName>{order.vendor_name}</LongName>, and when. Aetheris does
          not transmit the order — send it to the vendor yourself. Once sent it can be received or
          cancelled.
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Mark as sent"
      pendingLabel="Saving…"
      dismissLabel="Not yet"
      fallbackError="Couldn't mark the order as sent. Please try again."
      onSubmit={async () => {
        const sent = await send.mutateAsync()
        return `${sent.po_number} marked as sent`
      }}
    >
      {() => null}
    </FormDialog>
  )
}

/**
 * Cancel a draft or a sent order (`POST /inventory/purchase-orders/{id}/cancel`).
 * The endpoint takes no body, so no reason is asked for: there is nowhere to
 * keep one.
 */
export function CancelInventoryOrderDialog({ order }: { order: InventoryOrder }) {
  const cancel = useCancelInventoryOrder(order.id)
  return (
    <FormDialog<ConfirmValues>
      trigger={
        <Button variant="outline" size="sm" className="text-error hover:text-error">
          <XCircle className="size-4" /> Cancel order
        </Button>
      }
      title="Cancel this order?"
      description={
        <>
          {order.po_number} to <LongName>{order.vendor_name}</LongName> is cancelled for good: a cancelled order cannot
          be reopened, sent or received. No reason is asked for, because the order has nowhere to
          keep one.
          {order.status === 'sent' ? ' Aetheris does not tell the vendor — let them know yourself.' : ''}
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Cancel order"
      pendingLabel="Cancelling…"
      destructive
      dismissLabel="Keep order"
      fallbackError="Couldn't cancel the order. Please try again."
      onSubmit={async () => {
        const cancelled = await cancel.mutateAsync()
        return `${cancelled.po_number} cancelled`
      }}
    >
      {() => null}
    </FormDialog>
  )
}

// ── Receiving ───────────────────────────────────────────────────────────────

/** Only an active location can receive stock (a 400 otherwise, §10.7). */
const ACTIVE_LOCATIONS = { is_active: true } as const

/**
 * Where the delivery goes: one active location for the whole receipt
 * (`location_id`). The list is `GET /inventory/locations`, which needs
 * `inventory.location.read`; Receive is not offered without it.
 */
function LocationSelect({ form }: { form: UseFormReturn<ReceiptValues> }) {
  const { data, isError, refetch } = useLocations(ACTIVE_LOCATIONS)
  const locations = data ?? []
  const isPending = !data && !isError
  const chosen = useWatch({ control: form.control, name: 'location_id' })
  const gone = !!chosen && !!data && !data.some((location) => location.id === chosen)

  // A location switched off (400) or removed (422) while the form was open
  // drops out of the list when it is asked for again. The form would still
  // hold its id — shown as an empty trigger, and sent again for the same
  // refusal — so the choice is cleared and has to be made again. A reason the
  // server already put under the field is kept.
  useEffect(() => {
    if (!gone) return
    form.setValue('location_id', '', { shouldDirty: true })
    if (!form.getFieldState('location_id').error) {
      form.setError('location_id', {
        message: 'The location chosen can no longer receive stock. Choose another.',
      })
    }
  }, [gone, form])

  return (
    <div className="space-y-2">
      <Controller
        control={form.control}
        name="location_id"
        render={({ field }) => (
          <Field
            label="Receive into"
            required
            hint="Everything on this receipt goes into this one location."
            error={form.formState.errors.location_id?.message}
          >
            {(p) => (
              <Select
                value={field.value}
                onValueChange={field.onChange}
                disabled={isPending || locations.length === 0}
              >
                <SelectTrigger
                  id={p.id}
                  ref={field.ref}
                  aria-invalid={p['aria-invalid']}
                  aria-describedby={p['aria-describedby']}
                >
                  <SelectValue placeholder={isPending ? 'Loading locations…' : 'Select a location'} />
                </SelectTrigger>
                <SelectContent>
                  {locations.map((location) => (
                    <SelectItem key={location.id} value={location.id}>
                      {location.name} ({location.code})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>
        )}
      />
      {isError ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="font-body text-body-sm text-on-surface-variant">The location list couldn't be loaded.</p>
          <Button type="button" variant="outline" size="sm" onClick={() => void refetch()}>
            <RotateCw className="size-4" /> Retry
          </Button>
        </div>
      ) : !isPending && locations.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">
          There is no active location to receive into. Add one, or switch one back on, under{' '}
          <Link to="/inventory/locations" className="text-secondary hover:underline">
            Locations
          </Link>
          .
        </p>
      ) : null}
    </div>
  )
}

/**
 * In words, how what is entered for a line compares with what was ordered.
 * Plain text, not a live region: every line has one, and they would all speak
 * at each keystroke in a quantity. The shortfall summary speaks for them.
 */
function LineVerdict({ ordered, unit, receipt }: { ordered: string; unit?: string; receipt: LineReceipt }) {
  const wanted = quantityLabel(ordered, unit)
  return (
    <p className="font-body text-body-sm text-on-surface flex flex-wrap items-center gap-x-2 gap-y-1">
      {receipt.state === 'none' ? (
        <>
          <Badge variant="warning">Left out</Badge>
          Nothing arrived: this line is left out of the receipt, and the {wanted} ordered cannot be
          received later.
        </>
      ) : receipt.state === 'unknown' ? (
        <span className="text-on-surface-variant">
          Enter a quantity for each row to compare with the {wanted} ordered.
        </span>
      ) : receipt.state === 'short' ? (
        <>
          <Badge variant="warning">Short</Badge>
          {quantityLabel(receipt.entered)} of {wanted} entered — {quantityLabel(receipt.difference, unit)} fewer
          than ordered, which cannot be received later.
        </>
      ) : receipt.state === 'over' ? (
        <>
          <Badge variant="accent">Over</Badge>
          {quantityLabel(receipt.entered, unit)} entered — {quantityLabel(receipt.difference, unit)} more than
          the {quantityLabel(ordered)} ordered.
        </>
      ) : (
        <>
          <Badge variant="success">Complete</Badge>
          All {wanted} ordered are entered.
        </>
      )}
    </p>
  )
}

/**
 * "Never expires" holds only for a batch that is new to the location: one
 * already held there keeps the expiry it was first recorded with when the
 * receipt gives none (`get_or_create_stock` inserts with ON CONFLICT DO NOTHING).
 */
const EXPIRY_HINT =
  'Optional. A batch received without one is never treated as expired, unless this location already holds the batch with an expiry, which it keeps.'

/**
 * What is known about a line's item when its row is drawn. The order line does
 * not say whether its item is batch-tracked, so the item's own record is read.
 */
type Tracking =
  /** Batch-tracked: every row needs a batch number, and may carry an expiry. */
  | 'tracked'
  /** Not batch-tracked: a batch number is a 422, so neither input is shown. */
  | 'untracked'
  /** The item's record is on its way. */
  | 'loading'
  /** Not readable (no `inventory.item.read`) or failed to load: the server decides. */
  | 'unknown'

function ReceiptRowFields({
  form,
  index,
  label,
  heading,
  tracking,
  busy,
  onRemove,
}: {
  form: UseFormReturn<ReceiptValues>
  /** The row's position in the flat list — and so in the request. */
  index: number
  label: string
  heading: string
  tracking: Tracking
  busy: boolean
  /** Absent for a line's only row: that one goes with "Nothing arrived". */
  onRemove?: () => void
}) {
  const e = form.formState.errors.items?.[index]
  // Refusals the API pins on the row itself rather than on one of its inputs.
  const rowError = e?.po_item_id?.message ?? e?.root?.message ?? e?.message
  // An untracked item's row has no batch or expiry input, yet the server may
  // still name one (the item's record changed, or was read stale): the reason
  // must not vanish with the input it would sit under.
  const hidden = tracking === 'untracked' || tracking === 'loading'
  const hiddenError = hidden ? (e?.batch_number?.message ?? e?.expiry_date?.message) : undefined

  return (
    <div role="group" aria-label={label} className="neo-pressed bg-surface space-y-3 rounded-xl p-3">
      <div className="flex items-center justify-between gap-3">
        <p className="font-label text-label-caps text-on-surface-variant">{heading}</p>
        {onRemove && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="text-error hover:text-error"
            aria-label={`Remove ${label}`}
            disabled={busy}
            onClick={onRemove}
          >
            <Trash2 className="size-4" /> Remove
          </Button>
        )}
      </div>
      {/* One column on a phone: two leave a date input about 150px. */}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="Quantity received"
          required
          hint="What arrived. It may differ from what was ordered."
          error={e?.quantity?.message}
        >
          {(p) => (
            <Input inputMode="decimal" autoComplete="off" {...p} {...form.register(`items.${index}.quantity`)} />
          )}
        </Field>
        {!hidden && (
          <>
            <Field
              label="Batch number"
              required={tracking === 'tracked'}
              hint={
                tracking === 'tracked'
                  ? 'Saved in capitals'
                  : 'Needed only if this item is batch-tracked. Saved in capitals.'
              }
              error={e?.batch_number?.message}
            >
              {(p) => (
                <Input
                  className="uppercase"
                  autoComplete="off"
                  {...p}
                  {...form.register(`items.${index}.batch_number`)}
                />
              )}
            </Field>
            {/* Optional, as on the server. No earliest date is set: the server
                judges expiry on the hospital's calendar, and its refusal is
                shown under this field. */}
            <Field
              label="Expiry date"
              hint={
                tracking === 'tracked'
                  ? EXPIRY_HINT
                  : `${EXPIRY_HINT} Recorded only with a batch number: an item that is not batch-tracked keeps no expiry.`
              }
              error={e?.expiry_date?.message}
            >
              {(p) => <Input type="date" {...p} {...form.register(`items.${index}.expiry_date`)} />}
            </Field>
          </>
        )}
      </div>
      {(rowError || hiddenError) && (
        <p className="font-body text-error text-xs" role="alert">
          {rowError ?? hiddenError}
        </p>
      )}
    </div>
  )
}

interface PlacedRow {
  /** The field array's key for the row. */
  key: string
  /** Its position in the flat list. */
  index: number
  quantity: string
}

/**
 * One order line of the receipt, with its rows. It reads the line's item
 * (`GET /inventory/items/{id}`, `inventory.item.read`) for the two things the
 * order line leaves out: whether the item is batch-tracked, and its unit. A
 * user who cannot read items is asked for nothing; their rows offer an
 * optional batch number and the server's refusal says if one was needed.
 */
function ReceiptLineFields({
  form,
  line,
  rows,
  full,
  busy,
  onAdd,
  onRemove,
}: {
  form: UseFormReturn<ReceiptValues>
  line: InventoryOrderItem
  rows: PlacedRow[]
  /** The receipt already holds its fifty rows. */
  full: boolean
  busy: boolean
  onAdd: (quantity?: string) => void
  onRemove: (indexes: number[]) => void
}) {
  const { can } = usePermissions()
  const canReadItems = can('inventory.item.read')
  const { data: item, isError, refetch } = useItem(line.item_id, { enabled: canReadItems })
  const tracking: Tracking = item
    ? item.is_batch_tracked
      ? 'tracked'
      : 'untracked'
    : canReadItems && !isError
      ? 'loading'
      : 'unknown'
  const name = lineName(line)
  const unit = item?.unit_of_measure
  const receipt = lineReceipt(
    line.quantity,
    rows.map((row) => row.quantity),
  )
  // Only a batch-tracked item can arrive as more than one row: two rows of an
  // untracked item would be the same (line, no batch) twice, which is a 422.
  const canSplit = tracking === 'tracked' || tracking === 'unknown'

  return (
    <fieldset className="border-outline-variant/30 min-w-0 space-y-3 border-t pt-4 first:border-t-0 first:pt-0">
      <legend className="font-display text-primary pr-3 text-base font-bold [overflow-wrap:anywhere]">
        {line.item_name} <span className="text-outline font-mono text-xs font-normal">{line.item_sku}</span>
      </legend>
      <p className="font-body text-outline text-xs">
        Ordered {quantityLabel(line.quantity, unit)} at {formatMoney(line.unit_price)} each.{' '}
        {tracking === 'tracked'
          ? 'Batch-tracked: each batch that arrived is entered with its number.'
          : tracking === 'untracked'
            ? 'Not batch-tracked: no batch number or expiry is recorded for it.'
            : tracking === 'loading'
              ? 'Checking whether this item is batch-tracked…'
              : canReadItems
                ? "Couldn't check whether this item is batch-tracked. If it is, give a batch number; the server refuses the receipt otherwise."
                : 'Your role cannot read the item list, so whether this item is batch-tracked is not shown. If it is, give a batch number; the server refuses the receipt otherwise.'}
      </p>
      {canReadItems && isError && (
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={`Check ${name} again`}
          onClick={() => void refetch()}
        >
          <RotateCw className="size-4" /> Check again
        </Button>
      )}

      {rows.map((row, at) => (
        <ReceiptRowFields
          key={row.key}
          form={form}
          index={row.index}
          // An untracked line can hold two rows — added while its item could
          // not be checked — and the form refuses the second: they are
          // numbered so the refused one, and its Remove, can be told apart.
          label={canSplit ? `${name}, batch ${at + 1}` : rows.length > 1 ? `${name}, row ${at + 1}` : `${name}, received`}
          heading={canSplit ? `Batch ${at + 1}` : rows.length > 1 ? `Row ${at + 1}` : 'Received'}
          tracking={tracking}
          busy={busy}
          onRemove={rows.length > 1 ? () => onRemove([row.index]) : undefined}
        />
      ))}

      <LineVerdict ordered={line.quantity} unit={unit} receipt={receipt} />

      <div className="flex flex-wrap gap-2">
        {rows.length === 0 ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={`Record a delivery of ${name}`}
            disabled={full || busy}
            onClick={() => onAdd(quantityLabel(line.quantity))}
          >
            <Undo2 className="size-4" /> Record a delivery
          </Button>
        ) : (
          <>
            {canSplit && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                aria-label={`Add another batch of ${name}`}
                title={full ? `A receipt holds at most ${MAX_RECEIPT_ROWS} rows` : undefined}
                disabled={full || busy}
                onClick={() => onAdd()}
              >
                <Plus className="size-4" /> Add another batch
              </Button>
            )}
            {/* A quantity of 0 is a 422: a line nothing arrived for is sent as
                no rows at all. */}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label={`Nothing arrived for ${name}`}
              disabled={busy}
              onClick={() => onRemove(rows.map((row) => row.index))}
            >
              <PackageX className="size-4" /> Nothing arrived
            </Button>
          </>
        )}
      </div>
    </fieldset>
  )
}

/**
 * `busy` is true while the receipt is in flight. Rows cannot be added or
 * removed then: the server names a refused row by its position in what was
 * sent, and that has to still be the row's position here when the answer lands.
 */
function ReceiptFields({
  form,
  order,
  busy,
}: {
  form: UseFormReturn<ReceiptValues>
  order: InventoryOrder
  busy: boolean
}) {
  const { fields, insert, remove } = useFieldArray({ control: form.control, name: 'items' })
  const rows = useWatch({ control: form.control, name: 'items' })
  const { errors } = form.formState
  const listError = errors.items?.root?.message ?? errors.items?.message
  const full = fields.length >= MAX_RECEIPT_ROWS
  const lineIds = order.items.map((line) => line.id)
  const short = shortLines(order.items, rows)
  const shortfall = shortfallKey(order.items, rows)
  const acknowledgeErrorId = useId()

  const addRow = (lineId: string, quantity?: string) =>
    insert(
      insertionIndex(
        fields.map((f) => f.po_item_id),
        lineIds,
        lineId,
      ),
      receiptRow(lineId, quantity),
    )

  return (
    <div className="space-y-4">
      <LocationSelect form={form} />

      <div className="neo-pressed bg-surface space-y-1.5 rounded-xl px-4 py-3">
        <p className="font-body text-body-sm text-on-surface">
          {fields.length} of {MAX_RECEIPT_ROWS} rows entered. One receipt takes at most {MAX_RECEIPT_ROWS}{' '}
          rows across the whole order, and there is no second receipt.
          {full ? ' This one is full.' : ''}
        </p>
        <p className="font-body text-outline text-xs">
          A batch number the location already holds is topped up and keeps its recorded expiry: a
          different expiry entered for it here is refused.
        </p>
      </div>

      {listError && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}

      {order.items.map((line) => (
        <ReceiptLineFields
          key={line.id}
          form={form}
          line={line}
          rows={fields.flatMap((field, index) =>
            field.po_item_id === line.id ? [{ key: field.id, index, quantity: rows[index]?.quantity ?? '' }] : [],
          )}
          full={full}
          busy={busy}
          onAdd={(quantity) => addRow(line.id, quantity)}
          onRemove={(indexes) => remove(indexes)}
        />
      ))}

      {/* The dialog's one live region, there from the start so that what
          appears in it is announced. Polite: the summary changes while a
          quantity is being typed, and must not cut that short — which is why
          the alert inside is not left to announce itself. */}
      <div aria-live="polite" className="space-y-2">
        {shortfall && (
          <>
            <Alert role={undefined} variant="warning" title="This receipt is short of the order">
              {plural(short.length, 'line is', 'lines are')} short or left out:{' '}
              {short.map((line) => line.item_name).join(', ')}. An order is received once. Receiving
              closes it, and what is missing here cannot be received on it afterwards.
            </Alert>
            {/* Ticked only for the shortfall as it stands: change what is short
                and it has to be confirmed again. */}
            <Controller
              control={form.control}
              name="acknowledged"
              render={({ field }) => (
                <label className="font-body text-body-sm text-on-surface flex items-start gap-2">
                  <Checkbox
                    ref={field.ref}
                    className="mt-0.5"
                    checked={field.value === shortfall}
                    onCheckedChange={(v) => field.onChange(v === true ? shortfall : '')}
                    aria-invalid={!!errors.acknowledged}
                    aria-describedby={errors.acknowledged?.message ? acknowledgeErrorId : undefined}
                  />
                  I understand the rest of this order cannot be received later
                </label>
              )}
            />
            {errors.acknowledged?.message && (
              <p id={acknowledgeErrorId} className="font-body text-error text-xs" role="alert">
                {errors.acknowledged.message}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

/**
 * Receive a sent order (`POST /inventory/purchase-orders/{id}/receive`, §10.7;
 * `inventory_po_service.py`, `receive_purchase_order`) — the one receipt it
 * will ever have.
 *
 * Everything goes into one active location. Every order line is listed with
 * its rows: a batch-tracked item may be split across batches, each needing a
 * batch number; an untracked item is one row with no batch. A line nothing
 * arrived for is left out entirely. The server does not compare what arrived
 * with what was ordered and closes the order whatever is sent, so the form
 * says where the receipt is short and will not go ahead on a short one until
 * that is confirmed.
 *
 * Needs `inventory.po.receive`, and `inventory.location.read` for the
 * location list — the caller offers it only with both.
 */
export function ReceiveInventoryOrderDialog({ order }: { order: InventoryOrder }) {
  const qc = useQueryClient()
  const receive = useReceiveInventoryOrder(order.id)
  // Read from the cache the line rows fill, at the moment of submitting: the
  // form is validated and sent by what the rows were showing.
  const isTracked: TrackingLookup = useCallback(
    (itemId) => qc.getQueryData<InventoryItem>(inventoryKeys.item(itemId))?.is_batch_tracked,
    [qc],
  )
  const resolver = useMemo(() => zodResolver(receiptSchema(order.items, isTracked)), [order.items, isTracked])

  return (
    <FormDialog<ReceiptValues>
      trigger={
        <Button size="sm">
          <PackageCheck className="size-4" /> Receive
        </Button>
      }
      title={`Receive ${order.po_number}`}
      description={
        <>
          From <LongName>{order.vendor_name}</LongName>. Enter what arrived, as on the delivery note. Everything goes
          into one location at once and the order is closed — it is received once, in one go, and a
          receipt cannot be undone.
        </>
      }
      resolver={resolver}
      defaults={() => receiptDefaults(order)}
      submitLabel="Receive and close order"
      pendingLabel="Receiving…"
      dismissLabel="Not yet"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't confirm that the order was received. Try again: an order is received only once, so nothing is added to stock twice."
      onSubmit={async (values) => {
        try {
          const received = await receive.mutateAsync(toReceiptBody(values, order.items, isTracked))
          // Named from the server's answer, not from what was picked.
          const into = qc
            .getQueryData<InventoryLocation[]>(inventoryKeys.locationList(ACTIVE_LOCATIONS))
            ?.find((location) => location.id === received.received_location_id)
          return into ? `${received.po_number} received into ${into.name}` : `${received.po_number} received`
        } catch (err) {
          // A location switched off (400) or gone (422) since the list was
          // loaded: the list on screen is stale, so it is asked for again.
          if (err instanceof ApiError && (err.status === 400 || err.status === 422)) {
            void qc.invalidateQueries({ queryKey: inventoryKeys.locations() })
          }
          throw err
        }
      }}
    >
      {(form) => <ReceiptFields form={form} order={order} busy={receive.isPending} />}
    </FormDialog>
  )
}

/**
 * The steps an order offers in its current status, each shown only to a user
 * holding the permission its endpoint requires (§10.1, §10.7). A convenience:
 * the server enforces both the status and the permission.
 *
 * Receiving also needs the location list. A user who may receive but cannot
 * read locations is told so instead of being handed a form with no way to say
 * where the goods go.
 */
export function InventoryOrderActions({ order }: { order: InventoryOrder }) {
  const { can } = usePermissions()
  const canSend = order.status === 'draft' && can('inventory.po.update')
  const mayReceive = order.status === 'sent' && can('inventory.po.receive')
  const canReceive = mayReceive && can('inventory.location.read')
  const canCancel = CANCELLABLE.has(order.status) && can('inventory.po.update')
  if (!canSend && !mayReceive && !canCancel) return null

  return (
    <div className="flex min-w-0 flex-col items-end gap-2">
      <div className="flex flex-wrap items-center justify-end gap-2">
        {canSend && <SendInventoryOrderDialog order={order} />}
        {canReceive && <ReceiveInventoryOrderDialog order={order} />}
        {canCancel && <CancelInventoryOrderDialog order={order} />}
      </div>
      {mayReceive && !canReceive && (
        <p className="font-body text-on-surface-variant max-w-sm text-right text-xs">
          Receiving puts the goods into a location, and your role cannot read the location list. Ask
          an administrator for access to receive this order.
        </p>
      )}
    </div>
  )
}
