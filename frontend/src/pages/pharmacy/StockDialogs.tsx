import type { ReactNode } from 'react'
import { Controller, useWatch, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { FormDialog } from '@/components/forms/FormDialog'
import {
  useAdjustStock,
  useReceiveBatch,
  useSetBatchRecalled,
  type Medicine,
  type MedicineBatch,
} from '@/api/pharmacy'
import { units } from '@/components/pharmacy/pharmacyPresentation'
import {
  ADJUST_DIRECTIONS,
  ADJUST_REASONS,
  EMPTY_ADJUSTMENT,
  EMPTY_RECEIPT,
  adjustSchema,
  expiryDateLabel,
  receiveSchema,
  toAdjustBody,
  toReceiveBody,
  type AdjustValues,
  type ReceiveValues,
} from './stockForm'

/*
 * The writes on a medicine's stock (docs/18-API_CONTRACTS.md §9.3). None takes
 * an Idempotency-Key and two of them repeat if sent twice — a second receipt
 * tops the batch up again, a second adjustment moves the count again — so each
 * is a FormDialog, which ignores a submit while one is in flight.
 *
 * A batch's own `medicine_id` builds its URLs: the API answers "Batch not
 * found" to a batch addressed under any other medicine.
 */

const confirmSchema = z.object({})
type ConfirmValues = z.infer<typeof confirmSchema>

/**
 * A medicine's name or a batch number inside a dialog's description. Either
 * can be one long unbroken string (200 and 50 characters), which would push
 * past a narrow dialog unless it may break anywhere. Dialog titles cannot do
 * this — FormDialog takes a plain string — so they name neither.
 */
function Named({ children }: { children: string }) {
  return <span className="text-on-surface font-semibold [overflow-wrap:anywhere]">{children}</span>
}

function ReceiveFields({ form }: { form: UseFormReturn<ReceiveValues> }) {
  const { errors } = form.formState
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field
        label="Batch number"
        required
        hint="As printed on the pack — saved in capitals"
        error={errors.batch_number?.message}
      >
        {(p) => <Input className="uppercase" autoComplete="off" {...p} {...form.register('batch_number')} />}
      </Field>
      <Field
        label="Expiry date"
        required
        hint="The last day the batch may be dispensed"
        error={errors.expiry_date?.message}
      >
        {(p) => <Input type="date" {...p} {...form.register('expiry_date')} />}
      </Field>
      <Field label="Quantity" required hint="Whole units received" error={errors.quantity?.message}>
        {(p) => <Input inputMode="numeric" autoComplete="off" {...p} {...form.register('quantity')} />}
      </Field>
      <Field
        label="Cost per unit"
        required
        hint="What one unit was bought for"
        error={errors.cost_per_unit?.message}
      >
        {(p) => <Input inputMode="decimal" autoComplete="off" {...p} {...form.register('cost_per_unit')} />}
      </Field>
    </div>
  )
}

/**
 * Take stock in directly, without a purchase order
 * (`POST /medicines/{id}/batches`). The server decides whether the batch number
 * is new or a top-up and whether the expiry date is acceptable; its answer is
 * what the toast reports.
 */
export function ReceiveBatchDialog({ medicine, trigger }: { medicine: Medicine; trigger: ReactNode }) {
  const receive = useReceiveBatch(medicine.id)
  return (
    <FormDialog<ReceiveValues>
      trigger={trigger}
      title="Receive stock"
      description={
        <>
          Records stock of <Named>{medicine.name}</Named> taken in without a purchase order. A
          batch number this medicine already has is topped up rather than added again: the expiry date must then match the one on
          record, and the batch keeps its original cost.
        </>
      }
      resolver={zodResolver(receiveSchema)}
      defaults={() => EMPTY_RECEIPT}
      submitLabel="Receive stock"
      pendingLabel="Receiving…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't receive the stock. Please try again."
      onSubmit={async (values) => {
        const body = toReceiveBody(values)
        const batch = await receive.mutateAsync(body)
        return `Received ${units(body.quantity)} into batch ${batch.batch_number} — it now holds ${units(batch.quantity_on_hand)}`
      }}
    >
      {(form) => <ReceiveFields form={form} />}
    </FormDialog>
  )
}

function AdjustFields({ form }: { form: UseFormReturn<AdjustValues> }) {
  const { errors } = form.formState
  const direction = useWatch({ control: form.control, name: 'direction' })
  const adding = direction === 'add'

  return (
    <>
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
                    // The API refuses "expired" with an addition (§9.3), so the
                    // reason goes back to the one an addition can carry — in
                    // view, rather than swapped silently on submit.
                    if (value === 'add') form.setValue('reason', 'adjusted', { shouldDirty: true })
                  }}
                >
                  <SelectTrigger id={p.id} ref={field.ref} aria-invalid={p['aria-invalid']}>
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
          hint="A change to the count, not the new count"
          error={errors.quantity_change?.message}
        >
          {(p) => <Input inputMode="numeric" autoComplete="off" {...p} {...form.register('quantity_change')} />}
        </Field>
      </div>
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
                : 'Recorded with the adjustment. It does not change the batch’s expiry date or recall'
            }
            error={errors.reason?.message}
          >
            {(p) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger id={p.id} ref={field.ref} aria-invalid={p['aria-invalid']}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ADJUST_REASONS.map((r) => (
                    <SelectItem key={r.value} value={r.value} disabled={adding && r.value === 'expired'}>
                      {r.label}
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
        hint="Why the count is being corrected. Recorded with the adjustment"
        error={errors.note?.message}
      >
        {(p) => <Textarea rows={2} placeholder="e.g. Damaged strip" {...p} {...form.register('note')} />}
      </Field>
    </>
  )
}

/**
 * Correct one batch's count
 * (`POST /medicines/{id}/batches/{batch_id}/adjust`). The count shown is the
 * one this screen last read; if it has moved, the server refuses a removal the
 * batch can no longer cover and says what it holds, and the stock is read again.
 */
export function AdjustStockDialog({
  medicine,
  batch,
  trigger,
}: {
  medicine: Medicine
  batch: MedicineBatch
  trigger: ReactNode
}) {
  const adjust = useAdjustStock(batch.medicine_id)
  return (
    <FormDialog<AdjustValues>
      trigger={trigger}
      title="Adjust this batch"
      description={
        <>
          Corrects the count of this batch of <Named>{medicine.name}</Named> by adding or removing units. Each
          adjustment is recorded with its reason and note.
        </>
      }
      resolver={zodResolver(adjustSchema)}
      defaults={() => EMPTY_ADJUSTMENT}
      submitLabel="Save adjustment"
      pendingLabel="Saving…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't adjust the stock. Please try again."
      onSubmit={async (values) => {
        const saved = await adjust.mutateAsync({ batchId: batch.id, ...toAdjustBody(values) })
        return `Batch ${saved.batch_number} now holds ${units(saved.quantity_on_hand)}`
      }}
    >
      {(form) => (
        <>
          <dl className="neo-pressed bg-surface flex flex-wrap gap-x-8 gap-y-2 rounded-xl px-4 py-3">
            <div className="min-w-0">
              <dt className="font-label text-label-caps text-on-surface-variant">Batch</dt>
              <dd className="font-body text-body-sm text-on-surface font-mono font-semibold [overflow-wrap:anywhere]">
                {batch.batch_number}
              </dd>
            </div>
            <div>
              <dt className="font-label text-label-caps text-on-surface-variant">On hand now</dt>
              <dd className="font-body text-body-sm text-on-surface font-semibold tabular-nums">
                {units(batch.quantity_on_hand)}
              </dd>
            </div>
            <div>
              <dt className="font-label text-label-caps text-on-surface-variant">Expiry date</dt>
              <dd className="font-body text-body-sm text-on-surface font-semibold">
                {expiryDateLabel(batch.expiry_date)}
              </dd>
            </div>
          </dl>
          <AdjustFields form={form} />
        </>
      )}
    </FormDialog>
  )
}

/**
 * Recall a batch (`PATCH /medicines/{id}/batches/{batch_id}` with
 * `is_recalled: true`). Nothing is removed: the units stay on hand and stop
 * being dispensable.
 */
export function RecallBatchDialog({
  medicine,
  batch,
  trigger,
}: {
  medicine: Medicine
  batch: MedicineBatch
  trigger: ReactNode
}) {
  const setRecalled = useSetBatchRecalled(batch.medicine_id)
  return (
    <FormDialog<ConfirmValues>
      trigger={trigger}
      title="Recall this batch?"
      description={
        <>
          Batch <Named>{batch.batch_number}</Named> of <Named>{medicine.name}</Named> holds{' '}
          {units(batch.quantity_on_hand)}. Once it is recalled they can no longer be dispensed. The count is not changed — the units stay on hand as recalled
          stock — and the recall can be lifted later.
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Recall batch"
      pendingLabel="Recalling…"
      destructive
      dismissLabel="Not now"
      fallbackError="Couldn't recall the batch. Please try again."
      onSubmit={async () => {
        const saved = await setRecalled.mutateAsync({ batchId: batch.id, is_recalled: true })
        return `Batch ${saved.batch_number} recalled`
      }}
    >
      {() => null}
    </FormDialog>
  )
}

/**
 * Lift a recall (the same endpoint with `is_recalled: false`). Lifting it does
 * not by itself make the units dispensable: an expired or empty batch stays
 * undispensable (the server's `is_expired` and `is_dispensable` say so), and
 * so does every batch of an inactive medicine — which `is_dispensable` does
 * not account for.
 */
export function LiftRecallDialog({
  medicine,
  batch,
  trigger,
}: {
  medicine: Medicine
  batch: MedicineBatch
  trigger: ReactNode
}) {
  const setRecalled = useSetBatchRecalled(batch.medicine_id)
  const consequence = batch.is_expired
    ? 'It has expired, so its units still cannot be dispensed once the recall is lifted.'
    : !medicine.is_active
      ? 'The medicine is inactive, so its units still cannot be dispensed once the recall is lifted.'
      : batch.quantity_on_hand === 0
        ? 'It is empty, so there is nothing to dispense from it.'
        : 'Once the recall is lifted they can be dispensed again.'
  return (
    <FormDialog<ConfirmValues>
      trigger={trigger}
      title="Lift the recall on this batch?"
      description={
        <>
          Batch <Named>{batch.batch_number}</Named> of <Named>{medicine.name}</Named> holds{' '}
          {units(batch.quantity_on_hand)}. {consequence}
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Lift recall"
      pendingLabel="Lifting…"
      dismissLabel="Keep recalled"
      fallbackError="Couldn't lift the recall. Please try again."
      onSubmit={async () => {
        const saved = await setRecalled.mutateAsync({ batchId: batch.id, is_recalled: false })
        return saved.is_dispensable && medicine.is_active
          ? `Recall lifted — batch ${saved.batch_number} can be dispensed again`
          : `Recall lifted on batch ${saved.batch_number}`
      }}
    >
      {() => null}
    </FormDialog>
  )
}
