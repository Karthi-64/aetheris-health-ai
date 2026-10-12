import { type ReactNode, useId, useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Controller, useFieldArray, useWatch, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
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
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { FormDialog } from '@/components/forms/FormDialog'
import { MedicinePicker } from '@/components/pharmacy/MedicinePicker'
import { units } from '@/components/pharmacy/pharmacyPresentation'
import {
  useCancelPurchaseOrder,
  useCreatePurchaseOrder,
  useReceivePurchaseOrder,
  useSendPurchaseOrder,
  useVendors,
  type PurchaseOrder,
  type PurchaseOrderItem,
  type PurchaseOrderStatus,
  type Vendor,
} from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { formatMoney } from '@/lib/format'
import {
  EMPTY_ORDER,
  EMPTY_ORDER_LINE,
  MAX_ORDER_LINES,
  MAX_RECEIPT_ROWS,
  insertionIndex,
  lineReceipt,
  previewOrderTotal,
  purchaseOrderSchema,
  receiptDefaults,
  receiptRow,
  receiptSchema,
  shortLines,
  shortfallKey,
  toCreateBody,
  toReceiptBody,
  type LineReceipt,
  type PurchaseOrderValues,
  type ReceiptValues,
} from './purchaseOrderForm'

const confirmSchema = z.object({})
type ConfirmValues = z.infer<typeof confirmSchema>

/** The most `GET /vendors` returns in one page (§1.6); more is a 422, not a clamp. */
const VENDOR_PAGE = 100

/** Statuses the cancel endpoint accepts (§9.8): anything not yet received. */
const CANCELLABLE: ReadonlySet<PurchaseOrderStatus> = new Set(['draft', 'sent'])

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/** A medicine named so that two strengths of one name stay apart. */
const lineName = (line: PurchaseOrderItem) => `${line.medicine_name} (${line.medicine_sku})`

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
 * Every vendor the filter matches, handed to `children`.
 *
 * `GET /vendors` gives at most 100 a page and has no search (§1.6, §9.7), so
 * a picker that stopped at one page could never reach the 101st vendor. Each
 * page is asked for by one instance of this component, which renders the next
 * instance while the total says there is more; the last one renders
 * `children`. That moves `children` down a level as each page lands, so
 * whatever they render is mounted afresh then — keep it disabled while
 * `isPending`, so nothing a user has open is taken from under them.
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
export function VendorFilter({ value, all, onChange }: { value: string; all: string; onChange: (value: string) => void }) {
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
function VendorSelect({ form }: { form: UseFormReturn<PurchaseOrderValues> }) {
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
              There is no active vendor to order from. Add one, or switch one back on, under{' '}
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

function OrderLines({ form, busy }: { form: UseFormReturn<PurchaseOrderValues>; busy: boolean }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: 'items' })
  const lines = useWatch({ control: form.control, name: 'items' })
  const { errors } = form.formState
  const listError = errors.items?.root?.message ?? errors.items?.message
  const preview = previewOrderTotal(lines)
  const complete = lines.length - preview.incomplete
  const full = fields.length >= MAX_ORDER_LINES

  return (
    <div className="space-y-3">
      <p className="font-label text-label-caps text-on-surface-variant">Medicines to order</p>
      {listError && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}

      {fields.map((field, index) => {
        const e = errors.items?.[index]
        const n = index + 1
        // The API takes a medicine once per order (a 422 on `items` otherwise).
        const taken = lines.flatMap((line, at) => (at !== index && line.medicine_id ? [line.medicine_id] : []))
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
              name={`items.${index}.medicine_id`}
              render={({ field: f }) => (
                <Field label="Medicine" required error={e?.medicine_id?.message}>
                  {(p) => (
                    // The picker takes no ref, so the form is handed a way to
                    // focus whichever control it is showing. Without one, a
                    // refusal of `items.<i>.medicine_id` on a long order would
                    // leave the refused line wherever it was, perhaps off-screen.
                    <div
                      ref={(wrapper) => {
                        if (wrapper) {
                          f.ref({ focus: () => wrapper.querySelector<HTMLElement>('input, button')?.focus() })
                        }
                      }}
                    >
                      <MedicinePicker
                        id={p.id}
                        value={f.value}
                        invalid={p['aria-invalid']}
                        excludeIds={taken}
                        onChange={(medicine) => f.onChange(medicine?.id ?? '')}
                      />
                    </div>
                  )}
                </Field>
              )}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Quantity" required hint="Whole units" error={e?.quantity?.message}>
                {(p) => (
                  <Input
                    inputMode="numeric"
                    autoComplete="off"
                    {...p}
                    {...form.register(`items.${index}.quantity`)}
                  />
                )}
              </Field>
              {/* Never prefilled: a medicine's catalog price is what a patient
                  is charged, not what the vendor charges the hospital. */}
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
          <Plus className="size-4" /> Add a medicine
        </Button>
        {full && (
          <p className="font-body text-outline text-xs">An order holds at most {MAX_ORDER_LINES} lines.</p>
        )}
      </div>
      <p className="font-body text-outline text-xs" aria-live="polite">
        {complete === 0
          ? 'Enter a quantity and a price to preview the total.'
          : `Estimated total ${formatMoney(preview.amount)}${
              preview.incomplete > 0 ? ` for the ${plural(complete, 'line', 'lines')} filled in so far` : ''
            } — a preview. The order shows the total the server works out.`}
      </p>
    </div>
  )
}

/**
 * `busy` is true while the request is in flight. The lines cannot be added or
 * removed then: a refusal names a line by its position in what was sent.
 */
function OrderFields({ form, busy }: { form: UseFormReturn<PurchaseOrderValues>; busy: boolean }) {
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

/**
 * What "New order" opens for a user who may draft orders but cannot read the
 * vendor list or the catalog. It is not the form: a form whose vendor and
 * lines cannot be shown has nothing to submit, and its button would only fail
 * on fields nobody can see. No seeded role is in this position; a custom one
 * can be.
 */
function CannotDraftDialog({ trigger }: { trigger: ReactNode }) {
  return (
    <Dialog>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New purchase order</DialogTitle>
          <DialogDescription>An order is drafted for one vendor, from the medicine catalog.</DialogDescription>
        </DialogHeader>
        <Alert variant="warning" title="This order can't be drafted from your account">
          Drafting an order needs both the vendor list and the medicine catalog, and your role cannot
          read at least one of them. Ask an administrator for access.
        </Alert>
        <DialogFooter>
          <DialogClose asChild>
            <Button type="button" variant="ghost">
              Close
            </Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function DraftPurchaseOrderDialog({ trigger }: { trigger: ReactNode }) {
  const navigate = useNavigate()
  const create = useCreatePurchaseOrder()

  return (
    <FormDialog<PurchaseOrderValues>
      trigger={trigger}
      title="New purchase order"
      description="Drafts an order for medicines from one vendor. A draft cannot be edited afterwards — to change it, cancel it and draft another — so check the lines before you save."
      resolver={zodResolver(purchaseOrderSchema)}
      defaults={() => EMPTY_ORDER}
      submitLabel="Draft order"
      pendingLabel="Drafting…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't confirm that the order was drafted. Check the purchase order list before trying again, so it isn't drafted twice."
      onSubmit={async (values) => {
        const order = await create.mutateAsync(toCreateBody(values))
        navigate(`/pharmacy/purchase-orders/${order.id}`)
        return `Drafted ${order.po_number}`
      }}
    >
      {(form) => <OrderFields form={form} busy={create.isPending} />}
    </FormDialog>
  )
}

/**
 * Draft a purchase order (`POST /purchase-orders`, docs/18-API_CONTRACTS.md
 * §9.8). The server numbers it and works out its totals.
 *
 * This write has no protection of its own against being sent twice — two
 * requests make two drafts — so the dialog's submit guard is what prevents a
 * duplicate, and a failure the API does not explain is reported as "not
 * confirmed" rather than as "not drafted".
 *
 * The vendor list and the medicine picker are requests of their own, each
 * with its own permission. Neither is mounted for a user whose role cannot
 * make it.
 */
export function CreatePurchaseOrderDialog({ trigger }: { trigger: ReactNode }) {
  const { can } = usePermissions()
  if (!can('pharmacy.vendor.read') || !can('pharmacy.medicine.read')) {
    return <CannotDraftDialog trigger={trigger} />
  }
  return <DraftPurchaseOrderDialog trigger={trigger} />
}

// ── Sending and cancelling ──────────────────────────────────────────────────

/**
 * Mark a draft as sent (`POST /purchase-orders/{id}/send`, no body). The
 * server only records the status and the time: nothing goes to the vendor,
 * and the dialog says so rather than let "sent" imply it.
 */
export function SendPurchaseOrderDialog({ order }: { order: PurchaseOrder }) {
  const send = useSendPurchaseOrder(order.id)
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
 * Cancel a draft or a sent order (`POST /purchase-orders/{id}/cancel`). The
 * endpoint takes no body, so no reason is asked for: there is nowhere to keep
 * one.
 */
export function CancelPurchaseOrderDialog({ order }: { order: PurchaseOrder }) {
  const cancel = useCancelPurchaseOrder(order.id)
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

/**
 * In words, how what is entered for a line compares with what was ordered.
 * Plain text, not a live region: every line has one, and they would all speak
 * at each keystroke in a quantity. The shortfall summary speaks for them.
 */
function LineVerdict({ line, receipt }: { line: PurchaseOrderItem; receipt: LineReceipt }) {
  const ordered = units(line.quantity)
  return (
    <p className="font-body text-body-sm text-on-surface flex flex-wrap items-center gap-x-2 gap-y-1">
      {receipt.state === 'none' ? (
        <>
          <Badge variant="warning">Left out</Badge>
          Nothing arrived: this line is left out of the receipt, and its {ordered} cannot be received
          later.
        </>
      ) : receipt.state === 'unknown' ? (
        <span className="text-on-surface-variant">
          Enter a quantity for each batch to compare with the {ordered} ordered.
        </span>
      ) : receipt.state === 'short' ? (
        <>
          <Badge variant="warning">Short</Badge>
          {receipt.entered.toLocaleString()} of {ordered} entered — {units(line.quantity - receipt.entered)}{' '}
          fewer than ordered, which cannot be received later.
        </>
      ) : receipt.state === 'over' ? (
        <>
          <Badge variant="accent">Over</Badge>
          {units(receipt.entered)} entered — {units(receipt.entered - line.quantity)} more than the{' '}
          {line.quantity.toLocaleString()} ordered.
        </>
      ) : (
        <>
          <Badge variant="success">Complete</Badge>
          All {ordered} ordered are entered.
        </>
      )}
    </p>
  )
}

function ReceiptRowFields({
  form,
  index,
  label,
  heading,
  orderPrice,
  busy,
  onRemove,
}: {
  form: UseFormReturn<ReceiptValues>
  /** The row's position in the flat list — and so in the request. */
  index: number
  label: string
  heading: string
  orderPrice: string
  busy: boolean
  /** Absent for a line's only row: that one goes with "Nothing arrived". */
  onRemove?: () => void
}) {
  const e = form.formState.errors.items?.[index]
  // Refusals the API pins on the row itself rather than on one of its inputs.
  const rowError = e?.po_item_id?.message ?? e?.root?.message ?? e?.message

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
        <Field label="Batch number" required hint="Saved in capitals" error={e?.batch_number?.message}>
          {(p) => (
            <Input
              className="uppercase"
              autoComplete="off"
              {...p}
              {...form.register(`items.${index}.batch_number`)}
            />
          )}
        </Field>
        {/* No earliest date is set: the server judges expiry on the
            hospital's calendar, and its refusal is shown under this field. */}
        <Field label="Expiry date" required error={e?.expiry_date?.message}>
          {(p) => <Input type="date" {...p} {...form.register(`items.${index}.expiry_date`)} />}
        </Field>
        <Field label="Quantity" required hint="Units in this batch" error={e?.quantity?.message}>
          {(p) => (
            <Input inputMode="numeric" autoComplete="off" {...p} {...form.register(`items.${index}.quantity`)} />
          )}
        </Field>
        <Field
          label="Cost per unit"
          hint={`Blank uses the order price, ${formatMoney(orderPrice)}`}
          error={e?.cost_per_unit?.message}
        >
          {(p) => (
            <Input
              inputMode="decimal"
              autoComplete="off"
              {...p}
              {...form.register(`items.${index}.cost_per_unit`)}
            />
          )}
        </Field>
      </div>
      {rowError && (
        <p className="font-body text-error text-xs" role="alert">
          {rowError}
        </p>
      )}
    </div>
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
  order: PurchaseOrder
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
      <div className="neo-pressed bg-surface space-y-1.5 rounded-xl px-4 py-3">
        <p className="font-body text-body-sm text-on-surface">
          {fields.length} of {MAX_RECEIPT_ROWS} batches entered. One receipt takes at most{' '}
          {MAX_RECEIPT_ROWS} batches across the whole order, and there is no second receipt.
          {full ? ' This one is full.' : ''}
        </p>
        <p className="font-body text-outline text-xs">
          A batch number the medicine already has in stock is topped up and keeps its existing cost:
          a cost entered for it here is not applied.
        </p>
      </div>

      {listError && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}

      {order.items.map((line) => {
        const name = lineName(line)
        const mine = fields.flatMap((field, index) => (field.po_item_id === line.id ? [{ field, index }] : []))
        const receipt = lineReceipt(
          line.quantity,
          mine.map(({ index }) => rows[index]?.quantity ?? ''),
        )
        return (
          <fieldset
            key={line.id}
            className="border-outline-variant/30 min-w-0 space-y-3 border-t pt-4 first:border-t-0 first:pt-0"
          >
            <legend className="font-display text-primary pr-3 text-base font-bold [overflow-wrap:anywhere]">
              {line.medicine_name}{' '}
              <span className="text-outline font-mono text-xs font-normal">{line.medicine_sku}</span>
            </legend>
            <p className="font-body text-outline text-xs">
              Ordered {units(line.quantity)} at {formatMoney(line.unit_price)} each.
            </p>

            {mine.map(({ field, index }, at) => (
              <ReceiptRowFields
                key={field.id}
                form={form}
                index={index}
                label={`${name}, batch ${at + 1}`}
                heading={`Batch ${at + 1}`}
                orderPrice={line.unit_price}
                busy={busy}
                onRemove={mine.length > 1 ? () => remove(index) : undefined}
              />
            ))}

            <LineVerdict line={line} receipt={receipt} />

            <div className="flex flex-wrap gap-2">
              {mine.length === 0 ? (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  aria-label={`Record a delivery of ${name}`}
                  disabled={full || busy}
                  onClick={() => addRow(line.id, String(line.quantity))}
                >
                  <Undo2 className="size-4" /> Record a delivery
                </Button>
              ) : (
                <>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    aria-label={`Add another batch of ${name}`}
                    title={full ? `A receipt holds at most ${MAX_RECEIPT_ROWS} batches` : undefined}
                    disabled={full || busy}
                    onClick={() => addRow(line.id)}
                  >
                    <Plus className="size-4" /> Add another batch
                  </Button>
                  {/* A quantity of 0 is a 422: a line nothing arrived for is
                      sent as no rows at all. */}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    aria-label={`Nothing arrived for ${name}`}
                    disabled={busy}
                    onClick={() => remove(mine.map(({ index }) => index))}
                  >
                    <PackageX className="size-4" /> Nothing arrived
                  </Button>
                </>
              )}
            </div>
          </fieldset>
        )
      })}

      {/* The dialog's one live region, there from the start so that what
          appears in it is announced. Polite: the summary changes while a
          quantity is being typed, and must not cut that short — which is why
          the alert inside is not left to announce itself. */}
      <div aria-live="polite" className="space-y-2">
        {shortfall && (
          <>
            <Alert role={undefined} variant="warning" title="This receipt is short of the order">
              {plural(short.length, 'line is', 'lines are')} short or left out:{' '}
              {short.map((line) => line.medicine_name).join(', ')}. An order is received once. Receiving
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
 * Receive a sent order (`POST /purchase-orders/{id}/receive`, §9.8) — the one
 * receipt it will ever have.
 *
 * Every order line is listed with one or more batch rows. A line may be split
 * across batches, and a line nothing arrived for is left out entirely. The
 * server does not compare what arrived with what was ordered and closes the
 * order whatever is sent, so the form says where the receipt is short and
 * will not go ahead on a short one until that is confirmed.
 *
 * Batches and prices are not chosen here beyond what the delivery note says:
 * the server creates or tops up each batch and judges every expiry date.
 */
export function ReceivePurchaseOrderDialog({ order }: { order: PurchaseOrder }) {
  const receive = useReceivePurchaseOrder(order.id)
  const resolver = useMemo(() => zodResolver(receiptSchema(order.items)), [order.items])

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
          From <LongName>{order.vendor_name}</LongName>. Enter each batch that arrived, as on the delivery note. Every
          batch goes into stock at once and the order is closed — it is received once, in one go.
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
        const sent = toReceiptBody(values)
        const received = await receive.mutateAsync(sent)
        return `${received.po_number} received — ${plural(sent.length, 'batch', 'batches')} added to stock`
      }}
    >
      {(form) => <ReceiptFields form={form} order={order} busy={receive.isPending} />}
    </FormDialog>
  )
}

/**
 * The steps an order offers in its current status, each shown only to a user
 * holding the permission its endpoint requires (§9.1, §9.8) — a pharmacist
 * can receive but not send or cancel. A convenience: the server enforces both
 * the status and the permission.
 */
export function PurchaseOrderActions({ order }: { order: PurchaseOrder }) {
  const { can } = usePermissions()
  const canSend = order.status === 'draft' && can('pharmacy.po.update')
  const canReceive = order.status === 'sent' && can('pharmacy.po.receive')
  const canCancel = CANCELLABLE.has(order.status) && can('pharmacy.po.update')
  if (!canSend && !canReceive && !canCancel) return null

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {canSend && <SendPurchaseOrderDialog order={order} />}
      {canReceive && <ReceivePurchaseOrderDialog order={order} />}
      {canCancel && <CancelPurchaseOrderDialog order={order} />}
    </div>
  )
}
