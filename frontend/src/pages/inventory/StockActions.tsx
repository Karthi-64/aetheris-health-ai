import { useEffect, useId, type ReactNode, type Ref } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Controller, useWatch, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Checkbox } from '@/components/ui/checkbox'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { FormDialog } from '@/components/forms/FormDialog'
import { FormRefusal } from '@/components/forms/formRefusal'
import { useDepartments } from '@/api/departments'
import {
  inventoryKeys,
  stockShortageOf,
  useAdjustStock,
  useConsumeStock,
  useLocations,
  useTransferStock,
  type InventoryItem,
  type StockRow,
} from '@/api/inventory'
import { ExpiredBadge } from '@/components/inventory/InventoryBadges'
import { ItemPicker } from '@/components/inventory/ItemPicker'
import {
  ADJUST_REASON,
  ADJUST_REASONS,
  plainDateLabel,
  quantityLabel,
} from '@/components/inventory/inventoryPresentation'
import { usePermissions } from '@/hooks/usePermissions'
import {
  ADJUST_DIRECTIONS,
  UNCONFIRMED,
  adjustDefaults,
  adjustResult,
  adjustSchema,
  asksForExpiry,
  consumeDefaults,
  consumeResult,
  consumeSchema,
  consumeStatement,
  itemRefOf,
  outcomeUnknown,
  stillOnRow,
  toAdjustBody,
  toConsumeBody,
  toTransferBody,
  transferDefaults,
  transferResult,
  transferSchema,
  type AdjustValues,
  type ConsumeValues,
  type TransferValues,
} from './stockActionForms'

/*
 * The three writes on stock (docs/18-API_CONTRACTS.md §10.5): recording what
 * was used, moving stock between locations, and correcting a count. None takes
 * an Idempotency-Key and each repeats if sent twice — a second consume takes
 * the units again — so each is a FormDialog, which ignores a submit while one
 * is in flight. For the same reason a failure that does not say whether the
 * write was applied (a 5xx, a lost connection) is never answered with "try
 * again": the dialog says to check the ledger first, and the lists behind it
 * are read again.
 *
 * Opened from a stock row a dialog starts on that row's item, location and
 * batch; opened from the page header it starts empty. Either way what is sent
 * is what the form holds, and what is reported afterwards is what the server
 * answered.
 */

const LONG_FORM = 'max-h-[90vh] max-w-2xl overflow-y-auto'
const ALL = 'all'
const NONE = 'none'

/**
 * A location chooser over `GET /inventory/locations` (which needs
 * `inventory.location.read`; without it nothing is requested). The endpoint
 * returns the whole list, so there is nothing to search or page.
 *
 * An inactive location is listed and says so: stock can still be used up in
 * one or moved out of it. Only a destination must be active (`activeOnly`).
 */
export function LocationSelect({
  value,
  onChange,
  id,
  label,
  invalid,
  describedBy,
  placeholder = 'Choose a location',
  allLabel,
  activeOnly,
  known,
  triggerRef,
  className,
}: {
  /** The chosen location's id; empty for none. */
  value: string
  onChange: (id: string, name: string) => void
  id?: string
  /** An accessible name, for a chooser that has no visible label. */
  label?: string
  invalid?: boolean
  describedBy?: string
  placeholder?: string
  /** Adds a first option that clears the choice — for a filter. */
  allLabel?: string
  activeOnly?: boolean
  /** A location the caller already knows by name — a stock row's — so it shows before the list arrives. */
  known?: { id: string; name: string }
  triggerRef?: Ref<HTMLButtonElement>
  className?: string
}) {
  const { can } = usePermissions()
  const canRead = can('inventory.location.read')
  const { data, isError } = useLocations({}, { enabled: canRead })
  const listed = (data ?? []).filter((location) => !activeOnly || location.is_active)
  const options = [
    ...(known && !(data ?? []).some((location) => location.id === known.id) ? [{ ...known, is_active: true }] : []),
    ...listed,
  ]

  return (
    <div className="space-y-2">
      <Select
        value={value || (allLabel ? ALL : '')}
        onValueChange={(next) => {
          if (next === ALL) return onChange('', '')
          onChange(next, options.find((location) => location.id === next)?.name ?? '')
        }}
      >
        <SelectTrigger
          id={id}
          ref={triggerRef}
          aria-label={label}
          aria-invalid={invalid}
          aria-describedby={describedBy}
          className={className}
        >
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent>
          {allLabel && <SelectItem value={ALL}>{allLabel}</SelectItem>}
          {options.map((location) => (
            <SelectItem key={location.id} value={location.id}>
              {location.name}
              {location.is_active ? '' : ' (inactive)'}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {!canRead ? (
        <p className="font-body text-outline text-xs">You don't have access to the location list.</p>
      ) : isError ? (
        <p className="font-body text-error text-xs" role="alert">
          The location list couldn't be loaded.
        </p>
      ) : data && listed.length === 0 ? (
        <p className="font-body text-outline text-xs">
          {activeOnly ? 'There are no active locations yet.' : 'There are no locations yet.'}
        </p>
      ) : null}
    </div>
  )
}

/**
 * A 409 from consume or transfer means the location does not hold enough
 * usable stock, and that nothing was written (`InsufficientInventoryError`).
 * The server's sentence is shown as it came; if it ever stops saying that
 * nothing changed, this still does.
 */
async function sayingNothingChanged<T>(write: () => Promise<T>): Promise<T> {
  try {
    return await write()
  } catch (err) {
    if (err instanceof Error && stockShortageOf(err)) {
      throw new FormRefusal(/nothing was changed/i.test(err.message) ? err.message : `${err.message} Nothing was changed.`)
    }
    throw err
  }
}

/**
 * After a failure that leaves the outcome unknown the write may have been
 * applied, so the figures behind the dialog can no longer be trusted: stock,
 * the summary and the ledger are read again, and the ledger is where the user
 * is sent to look. (The shared hooks refresh after a success and after a
 * refusal, not after this.)
 */
function useRereadWhenUnsure() {
  const qc = useQueryClient()
  return async function rereadWhenUnsure<T>(write: () => Promise<T>): Promise<T> {
    try {
      return await write()
    } catch (err) {
      if (outcomeUnknown(err)) {
        void qc.invalidateQueries({ queryKey: inventoryKeys.stock() })
        void qc.invalidateQueries({ queryKey: inventoryKeys.summary() })
        void qc.invalidateQueries({ queryKey: inventoryKeys.movements() })
      }
      throw err
    }
  }
}

/** The hint under a quantity: the item's unit, and that a fraction is accepted. */
const quantityHint = (unit: string) =>
  unit ? `In “${unit}”. Part of a unit is allowed, to two decimal places` : 'Part of a unit is allowed, to two decimal places'

const BATCH_HINT =
  'Optional. Left empty, stock is taken earliest expiry first; with a batch, from that batch only. Saved in capitals'

// ── Consume ─────────────────────────────────────────────────────────────────

/** Mounted only for a user who may list departments (`department.read`), so nobody else asks for them. */
function DepartmentField({ form }: { form: UseFormReturn<ConsumeValues> }) {
  const { data, isError } = useDepartments()
  return (
    <Controller
      control={form.control}
      name="department_id"
      render={({ field }) => (
        <Field
          label="Department"
          hint={
            isError
              ? "The departments couldn't be loaded. The use can be recorded without one"
              : 'Optional. The department that used it'
          }
          error={form.formState.errors.department_id?.message}
        >
          {(p) => (
            <Select value={field.value || NONE} onValueChange={(next) => field.onChange(next === NONE ? '' : next)}>
              <SelectTrigger id={p.id} ref={field.ref} aria-invalid={p['aria-invalid']} aria-describedby={p['aria-describedby']}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>No department</SelectItem>
                {(data ?? []).map((department) => (
                  <SelectItem key={department.id} value={department.id}>
                    {department.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </Field>
      )}
    />
  )
}

function ConsumeFields({
  form,
  row,
  failure,
}: {
  form: UseFormReturn<ConsumeValues>
  row?: StockRow
  /** Why the last attempt failed, if it did. */
  failure: unknown
}) {
  const { can } = usePermissions()
  const { errors } = form.formState
  const confirmErrorId = useId()

  // The tick stood for one request. If that request may have gone through, a
  // second one is a second removal and needs its own tick — the dialog must
  // not be one click away from sending it again.
  useEffect(() => {
    if (failure && outcomeUnknown(failure)) form.setValue('confirmed', '')
  }, [failure, form])
  const [item_id, item_name, unit_of_measure, batch_tracked, location_id, location_name, quantity, batch_number] =
    useWatch({
      control: form.control,
      name: [
        'item_id',
        'item_name',
        'unit_of_measure',
        'batch_tracked',
        'location_id',
        'location_name',
        'quantity',
        'batch_number',
      ],
    })
  const statement = consumeStatement({
    item_id,
    item_name,
    unit_of_measure,
    batch_tracked,
    location_id,
    location_name,
    quantity,
    batch_number,
  })

  const pick = (item: InventoryItem | null) => {
    form.setValue('item_id', item?.id ?? '', { shouldDirty: true, shouldValidate: form.formState.isSubmitted })
    form.setValue('item_name', item?.name ?? '')
    form.setValue('unit_of_measure', item?.unit_of_measure ?? '')
    form.setValue('batch_tracked', item?.is_batch_tracked ?? false)
    // A batch number belongs to the item it was typed for.
    form.setValue('batch_number', '')
  }

  return (
    <>
      <Field label="Item" required error={errors.item_id?.message}>
        {(p) => (
          <ItemPicker
            id={p.id}
            value={item_id}
            onChange={pick}
            invalid={p['aria-invalid']}
            initialItem={row ? itemRefOf(row) : null}
          />
        )}
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Controller
          control={form.control}
          name="location_id"
          render={({ field }) => (
            <Field label="Taken from" required error={errors.location_id?.message}>
              {(p) => (
                <LocationSelect
                  id={p.id}
                  triggerRef={field.ref}
                  value={field.value}
                  onChange={(id, name) => {
                    field.onChange(id)
                    form.setValue('location_name', name)
                  }}
                  invalid={p['aria-invalid']}
                  describedBy={p['aria-describedby']}
                  known={row ? { id: row.location_id, name: row.location_name } : undefined}
                />
              )}
            </Field>
          )}
        />
        <Field label="Quantity used" required hint={quantityHint(unit_of_measure)} error={errors.quantity?.message}>
          {(p) => <Input inputMode="decimal" autoComplete="off" {...p} {...form.register('quantity')} />}
        </Field>
      </div>
      {/* An untracked item has no batches: a batch number would match nothing (409), so none is asked for. */}
      {batch_tracked && (
        <Field label="Batch" hint={BATCH_HINT} error={errors.batch_number?.message}>
          {(p) => <Input className="uppercase" autoComplete="off" {...p} {...form.register('batch_number')} />}
        </Field>
      )}
      {can('department.read') && <DepartmentField form={form} />}
      <Field label="Note" hint="Optional. Recorded with the ledger entry" error={errors.note?.message}>
        {(p) => <Textarea rows={2} {...p} {...form.register('note')} />}
      </Field>

      <div className="neo-pressed bg-surface space-y-2 rounded-xl px-4 py-3">
        <Controller
          control={form.control}
          name="confirmed"
          render={({ field }) => (
            <label className="font-body text-body-sm text-on-surface flex items-start gap-2">
              <Checkbox
                ref={field.ref}
                className="mt-0.5"
                disabled={!statement}
                // Ticked only for the sentence on screen: change what will be
                // removed and the tick no longer stands.
                checked={!!statement && field.value === statement}
                onCheckedChange={(v) => field.onChange(v === true && statement ? statement : '')}
                aria-invalid={!!errors.confirmed}
                aria-describedby={errors.confirmed?.message ? confirmErrorId : undefined}
              />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {statement ?? 'Choose the item, the location and the quantity to see what will be removed.'}
              </span>
            </label>
          )}
        />
        {errors.confirmed?.message && (
          <p id={confirmErrorId} className="font-body text-error text-xs" role="alert">
            {errors.confirmed.message}
          </p>
        )}
        <p className="font-body text-outline text-xs">
          Recording a use cannot be undone from here. A mistake is put right with an adjustment.
        </p>
      </div>
    </>
  )
}

/**
 * Record units used at a location (`POST /inventory/consume`, which needs
 * `inventory.consume`). The user ticks a sentence saying exactly what will be
 * removed before anything is sent. If the location does not hold enough usable
 * stock the server answers 409, changes nothing and says so; the stock behind
 * the dialog is read again.
 */
export function ConsumeDialog({ trigger, row }: { trigger: ReactNode; row?: StockRow }) {
  const consume = useConsumeStock()
  const rereadWhenUnsure = useRereadWhenUnsure()
  return (
    <FormDialog<ConsumeValues>
      trigger={trigger}
      title="Record stock used"
      description="Takes units out of one location's stock and writes them to the ledger as used. Stock is taken earliest expiry first, and never from an expired batch."
      resolver={zodResolver(consumeSchema)}
      defaults={() => consumeDefaults(row)}
      submitLabel="Record use"
      pendingLabel="Recording…"
      contentClassName={LONG_FORM}
      fallbackError={UNCONFIRMED.consume}
      onSubmit={async (values) => {
        const change = await sayingNothingChanged(() =>
          rereadWhenUnsure(() => consume.mutateAsync(toConsumeBody(values))),
        )
        return consumeResult(change, { [values.location_id]: values.location_name })
      }}
    >
      {(form) => <ConsumeFields form={form} row={row} failure={consume.error} />}
    </FormDialog>
  )
}

// ── Transfer ────────────────────────────────────────────────────────────────

function TransferFields({ form, row }: { form: UseFormReturn<TransferValues>; row?: StockRow }) {
  const { errors } = form.formState
  const [item_id, unit_of_measure, batch_tracked] = useWatch({
    control: form.control,
    name: ['item_id', 'unit_of_measure', 'batch_tracked'],
  })

  const pick = (item: InventoryItem | null) => {
    form.setValue('item_id', item?.id ?? '', { shouldDirty: true, shouldValidate: form.formState.isSubmitted })
    form.setValue('item_name', item?.name ?? '')
    form.setValue('unit_of_measure', item?.unit_of_measure ?? '')
    form.setValue('batch_tracked', item?.is_batch_tracked ?? false)
    form.setValue('batch_number', '')
  }

  return (
    <>
      <Field label="Item" required error={errors.item_id?.message}>
        {(p) => (
          <ItemPicker
            id={p.id}
            value={item_id}
            onChange={pick}
            invalid={p['aria-invalid']}
            initialItem={row ? itemRefOf(row) : null}
          />
        )}
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Controller
          control={form.control}
          name="from_location_id"
          render={({ field }) => (
            <Field label="From" required error={errors.from_location_id?.message}>
              {(p) => (
                <LocationSelect
                  id={p.id}
                  triggerRef={field.ref}
                  value={field.value}
                  onChange={(id, name) => {
                    field.onChange(id)
                    form.setValue('from_location_name', name)
                    if (form.formState.isSubmitted) void form.trigger('to_location_id')
                  }}
                  invalid={p['aria-invalid']}
                  describedBy={p['aria-describedby']}
                  known={row ? { id: row.location_id, name: row.location_name } : undefined}
                />
              )}
            </Field>
          )}
        />
        <Controller
          control={form.control}
          name="to_location_id"
          render={({ field }) => (
            <Field
              label="To"
              required
              // The API refuses an inactive destination (400), so none is listed.
              hint="Only an active location can receive stock"
              error={errors.to_location_id?.message}
            >
              {(p) => (
                <LocationSelect
                  id={p.id}
                  triggerRef={field.ref}
                  value={field.value}
                  onChange={(id, name) => {
                    field.onChange(id)
                    form.setValue('to_location_name', name)
                  }}
                  invalid={p['aria-invalid']}
                  describedBy={p['aria-describedby']}
                  activeOnly
                />
              )}
            </Field>
          )}
        />
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Quantity to move" required hint={quantityHint(unit_of_measure)} error={errors.quantity?.message}>
          {(p) => <Input inputMode="decimal" autoComplete="off" {...p} {...form.register('quantity')} />}
        </Field>
        {batch_tracked && (
          <Field label="Batch" hint={BATCH_HINT} error={errors.batch_number?.message}>
            {(p) => <Input className="uppercase" autoComplete="off" {...p} {...form.register('batch_number')} />}
          </Field>
        )}
      </div>
      <Field label="Note" hint="Optional. Recorded with both ledger entries" error={errors.note?.message}>
        {(p) => <Textarea rows={2} {...p} {...form.register('note')} />}
      </Field>
    </>
  )
}

/**
 * Move units from one location to another (`POST /inventory/transfer`, which
 * needs `inventory.transfer`). Batches keep their number and expiry. The
 * answer carries the ledger entries written — out of the source, into the
 * destination — and those are what the toast reports; it carries no balance
 * for either place, so none is shown: the refreshed list has them.
 */
export function TransferDialog({ trigger, row }: { trigger: ReactNode; row?: StockRow }) {
  const transfer = useTransferStock()
  const rereadWhenUnsure = useRereadWhenUnsure()
  return (
    <FormDialog<TransferValues>
      trigger={trigger}
      title="Transfer stock"
      description="Moves units of one item from one location to another. Nothing leaves the hospital: each batch keeps its number and expiry, and stock is taken earliest expiry first, never from an expired batch."
      resolver={zodResolver(transferSchema)}
      defaults={() => transferDefaults(row)}
      submitLabel="Transfer"
      pendingLabel="Transferring…"
      contentClassName={LONG_FORM}
      fallbackError={UNCONFIRMED.transfer}
      onSubmit={async (values) => {
        const change = await sayingNothingChanged(() =>
          rereadWhenUnsure(() => transfer.mutateAsync(toTransferBody(values))),
        )
        return transferResult(change, {
          [values.from_location_id]: values.from_location_name,
          [values.to_location_id]: values.to_location_name,
        })
      }}
    >
      {(form) => <TransferFields form={form} row={row} />}
    </FormDialog>
  )
}

// ── Adjust ──────────────────────────────────────────────────────────────────

function AdjustFields({ form, row }: { form: UseFormReturn<AdjustValues>; row?: StockRow }) {
  const { errors } = form.formState
  const [item_id, unit_of_measure, batch_tracked, location_id, batch_number, direction] = useWatch({
    control: form.control,
    name: ['item_id', 'unit_of_measure', 'batch_tracked', 'location_id', 'batch_number', 'direction'],
  })
  const adding = direction === 'add'
  const onRow = !!row && stillOnRow(row, { item_id, location_id, batch_number })

  const pick = (item: InventoryItem | null) => {
    form.setValue('item_id', item?.id ?? '', { shouldDirty: true, shouldValidate: form.formState.isSubmitted })
    form.setValue('item_name', item?.name ?? '')
    form.setValue('unit_of_measure', item?.unit_of_measure ?? '')
    form.setValue('batch_tracked', item?.is_batch_tracked ?? false)
    form.setValue('batch_number', '')
  }

  return (
    <>
      {/* The quantity read with the row, shown only while the form is still about that row. */}
      {row && onRow && (
        <dl className="neo-pressed bg-surface flex flex-wrap gap-x-8 gap-y-2 rounded-xl px-4 py-3">
          <div>
            <dt className="font-label text-label-caps text-on-surface-variant">Held when last read</dt>
            <dd className="font-body text-body-sm text-on-surface font-semibold tabular-nums">
              {quantityLabel(row.quantity, row.unit_of_measure)}
            </dd>
          </div>
          {row.expiry_date && (
            <div>
              <dt className="font-label text-label-caps text-on-surface-variant">Expiry date</dt>
              <dd className="font-body text-body-sm text-on-surface flex flex-wrap items-center gap-2 font-semibold">
                {plainDateLabel(row.expiry_date)}
                {row.is_expired && <ExpiredBadge />}
              </dd>
            </div>
          )}
        </dl>
      )}
      <Field
        label="Item"
        required
        // The API adjusts an inactive item too (what is left of a withdrawn
        // item is corrected or written off), but the search lists active items
        // only — so the way to one is said here rather than left to be found.
        hint={
          item_id
            ? undefined
            : 'The search lists active items. To adjust a withdrawn item, use Adjust on its stock row — tick Include empty rows if it holds nothing'
        }
        error={errors.item_id?.message}
      >
        {(p) => (
          <ItemPicker
            id={p.id}
            value={item_id}
            onChange={pick}
            invalid={p['aria-invalid']}
            initialItem={row ? itemRefOf(row) : null}
          />
        )}
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Controller
          control={form.control}
          name="location_id"
          render={({ field }) => (
            <Field label="Location" required error={errors.location_id?.message}>
              {(p) => (
                <LocationSelect
                  id={p.id}
                  triggerRef={field.ref}
                  value={field.value}
                  onChange={(id, name) => {
                    field.onChange(id)
                    form.setValue('location_name', name)
                  }}
                  invalid={p['aria-invalid']}
                  describedBy={p['aria-describedby']}
                  known={row ? { id: row.location_id, name: row.location_name } : undefined}
                />
              )}
            </Field>
          )}
        />
        {/* The API wants a batch number for a batch-tracked item and refuses one for any other (422). */}
        {batch_tracked && (
          <Field
            label="Batch"
            required
            hint="The batch being corrected. A new number, when adding, starts a new batch. Saved in capitals"
            error={errors.batch_number?.message}
          >
            {(p) => <Input className="uppercase" autoComplete="off" {...p} {...form.register('batch_number')} />}
          </Field>
        )}
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Controller
          control={form.control}
          name="direction"
          render={({ field }) => (
            <Field label="Change" required error={errors.direction?.message}>
              {(p) => (
                <Select
                  value={field.value}
                  onValueChange={(value) => {
                    field.onChange(value)
                    // The API refuses "expired" with an addition, so the reason
                    // goes back to the one an addition can carry — in view,
                    // rather than swapped silently on submit.
                    if (value === 'add') form.setValue('reason', 'adjusted', { shouldDirty: true })
                  }}
                >
                  <SelectTrigger
                    id={p.id}
                    ref={field.ref}
                    aria-invalid={p['aria-invalid']}
                    aria-describedby={p['aria-describedby']}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {ADJUST_DIRECTIONS.map((d) => (
                      <SelectItem key={d.value} value={d.value}>
                        {d.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </Field>
          )}
        />
        <Field
          label={adding ? 'Units to add' : 'Units to remove'}
          required
          hint={`A change to the count, not the new count. ${quantityHint(unit_of_measure)}`}
          error={errors.quantity_change?.message}
        >
          {(p) => <Input inputMode="decimal" autoComplete="off" {...p} {...form.register('quantity_change')} />}
        </Field>
      </div>
      {asksForExpiry({ batch_tracked, direction }) && (
        <Field
          label="Expiry date"
          hint="Optional. Recorded only if this starts a new batch — a batch already held keeps its date"
          error={errors.expiry_date?.message}
        >
          {(p) => <Input type="date" {...p} {...form.register('expiry_date')} />}
        </Field>
      )}
      <Controller
        control={form.control}
        name="reason"
        render={({ field }) => (
          <Field
            label="Reason"
            required
            hint={
              adding
                ? 'Expired stock can only be removed, so it is not offered when adding'
                : 'Recorded in the ledger with the adjustment'
            }
            error={errors.reason?.message}
          >
            {(p) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger
                  id={p.id}
                  ref={field.ref}
                  aria-invalid={p['aria-invalid']}
                  aria-describedby={p['aria-describedby']}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ADJUST_REASONS.map((reason) => (
                    <SelectItem key={reason} value={reason} disabled={adding && reason === 'expired'}>
                      {ADJUST_REASON[reason].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>
        )}
      />
      <Field
        label="Note"
        required
        hint="Why the count is changing. Recorded with the adjustment"
        error={errors.note?.message}
      >
        {(p) => <Textarea rows={2} placeholder="e.g. Counted 3 short on the shelf" {...p} {...form.register('note')} />}
      </Field>
    </>
  )
}

/**
 * Correct a count, or write off expired stock (`POST /inventory/adjust`, which
 * needs `inventory.adjust`). The quantity shown is the one this screen last
 * read; if it has moved, the server refuses a removal the row can no longer
 * cover (400) and says what it holds, and the stock is read again.
 *
 * The answer reports the change recorded and the item's totals across the
 * hospital — not what the corrected row now holds — so the toast says exactly
 * that and the refreshed list shows the row.
 */
export function AdjustDialog({ trigger, row }: { trigger: ReactNode; row?: StockRow }) {
  const adjust = useAdjustStock()
  const rereadWhenUnsure = useRereadWhenUnsure()
  return (
    <FormDialog<AdjustValues>
      trigger={trigger}
      title="Adjust stock"
      description="Corrects what one location holds of an item by adding or removing units, or writes off expired stock. Adding to a place that holds none is how an opening balance is entered. Each adjustment is recorded with its reason and note."
      resolver={zodResolver(adjustSchema)}
      defaults={() => adjustDefaults(row)}
      submitLabel="Save adjustment"
      pendingLabel="Saving…"
      contentClassName={LONG_FORM}
      fallbackError={UNCONFIRMED.adjust}
      onSubmit={async (values) => {
        const change = await rereadWhenUnsure(() => adjust.mutateAsync(toAdjustBody(values)))
        return adjustResult(change, { [values.location_id]: values.location_name })
      }}
    >
      {(form) => <AdjustFields form={form} row={row} />}
    </FormDialog>
  )
}
