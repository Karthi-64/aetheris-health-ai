import { useFieldArray, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ClipboardPen, Pencil, Send, TestTube, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { FormDialog } from '@/components/forms/FormDialog'
import { FormRefusal } from '@/components/forms/formRefusal'
import {
  useAmendLabResult,
  useCancelLabOrder,
  useCollectSamples,
  useEnterResults,
  useReleaseLabOrder,
  type LabOrder,
  type LabOrderItem,
} from '@/api/lab'
import { usePermissions } from '@/hooks/usePermissions'
import { fieldErrorsOf } from '@/lib/apiErrors'
import { LabResultFlagBadge } from '@/components/laboratory/LabBadges'
import {
  LAB_CANCELLABLE,
  LAB_RESULTS_ENTERABLE,
  resultLabel,
} from '@/components/laboratory/labPresentation'

const confirmSchema = z.object({})
type ConfirmValues = z.infer<typeof confirmSchema>

/** A number as the API accepts one for a numeric test: digits, optional sign and decimals. */
const NUMERIC = /^[+-]?(\d+\.?\d*|\.\d+)$/

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

/**
 * Collect the samples for an order (`POST /lab-orders/{id}/collect`). Sent with
 * no body, so every outstanding sample is collected and the server generates
 * the sample ids.
 */
function CollectSamplesDialog({ order }: { order: LabOrder }) {
  const collect = useCollectSamples(order.id)
  const outstanding = order.items.filter((i) => i.sample_collected_at === null).length
  return (
    <FormDialog<ConfirmValues>
      trigger={
        <Button size="sm">
          <TestTube className="size-4" /> Collect samples
        </Button>
      }
      title="Collect samples?"
      description={
        <>
          Records that {plural(outstanding, 'sample has', 'samples have')} been taken from{' '}
          {order.patient_name}. The system assigns each sample its id.
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Mark collected"
      pendingLabel="Saving…"
      dismissLabel="Not yet"
      fallbackError="Couldn't record the collection. Please try again."
      onSubmit={async () => {
        await collect.mutateAsync()
        return `Samples collected for ${order.patient_name}`
      }}
    >
      {() => null}
    </FormDialog>
  )
}

const resultsSchema = z
  .object({
    results: z.array(
      z.object({
        item_id: z.string(),
        numeric: z.boolean(),
        value: z.string().max(2000, 'Keep the result under 2,000 characters'),
        notes: z.string().max(2000, 'Keep the note under 2,000 characters'),
      }),
    ),
  })
  .superRefine((values, ctx) => {
    values.results.forEach((row, index) => {
      const value = row.value.trim()
      if (value && row.numeric && !NUMERIC.test(value)) {
        ctx.addIssue({ code: 'custom', path: ['results', index, 'value'], message: 'Enter a number' })
      }
      if (!value && row.notes.trim()) {
        ctx.addIssue({
          code: 'custom',
          path: ['results', index, 'value'],
          message: 'Enter the result this note belongs to',
        })
      }
    })
    if (values.results.every((row) => !row.value.trim())) {
      ctx.addIssue({ code: 'custom', path: ['results'], message: 'Enter at least one result' })
    }
  })

type ResultsValues = z.infer<typeof resultsSchema>

function ResultRows({ form, order }: { form: UseFormReturn<ResultsValues>; order: LabOrder }) {
  const { fields } = useFieldArray({ control: form.control, name: 'results' })
  const { errors } = form.formState
  const formError = errors.results?.root?.message ?? errors.results?.message

  return (
    <div className="space-y-4">
      {fields.map((field, index) => {
        const item = order.items[index]
        return (
          <fieldset
            key={field.id}
            className="border-outline-variant/30 min-w-0 space-y-3 border-t pt-4 first:border-t-0 first:pt-0"
          >
            <legend className="font-display text-primary pr-3 text-base font-bold">
              {item.test_name}{' '}
              <span className="text-outline font-mono text-xs font-normal">{item.test_code}</span>
            </legend>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="Result"
                hint={item.result_type === 'numeric' ? 'A number' : 'Free text'}
                error={errors.results?.[index]?.value?.message}
              >
                {(p) => (
                  <Input
                    inputMode={item.result_type === 'numeric' ? 'decimal' : 'text'}
                    autoComplete="off"
                    {...p}
                    {...form.register(`results.${index}.value`)}
                  />
                )}
              </Field>
              <Field label="Note" hint="Optional" error={errors.results?.[index]?.notes?.message}>
                {(p) => <Input autoComplete="off" {...p} {...form.register(`results.${index}.notes`)} />}
              </Field>
            </div>
            {item.sample_id && (
              <p className="font-body text-outline text-xs">
                Sample <span className="font-mono">{item.sample_id}</span>
              </p>
            )}
          </fieldset>
        )
      })}
      {formError && (
        <p className="font-body text-error text-xs" role="alert">
          {formError}
        </p>
      )}
    </div>
  )
}

/**
 * Enter results (`POST /lab-orders/{id}/enter-results`). A row left empty is
 * not sent, so results can be entered as they come off the bench; until the
 * order is released a result can be entered again to correct it.
 *
 * No reference range is shown or applied here: the server picks the range for
 * the patient's sex and age and returns its own flag.
 */
function EnterResultsDialog({ order }: { order: LabOrder }) {
  const enter = useEnterResults(order.id)
  const entered = order.items.filter((i) => i.result_value !== null).length

  return (
    <FormDialog<ResultsValues>
      trigger={
        <Button size="sm" variant={order.status === 'results_entered' ? 'outline' : 'default'}>
          <ClipboardPen className="size-4" />
          {entered === 0 ? 'Enter results' : entered === order.items.length ? 'Edit results' : 'Continue results'}
        </Button>
      }
      title="Enter results"
      description={
        <>
          {order.patient_name} · {plural(order.items.length, 'test', 'tests')}. Leave a test blank to
          enter it later. The system checks each number against the reference range for this
          patient.
        </>
      }
      resolver={zodResolver(resultsSchema)}
      defaults={() => ({
        results: order.items.map((item) => ({
          item_id: item.id,
          numeric: item.result_type === 'numeric',
          value: item.result_value ?? '',
          notes: item.notes ?? '',
        })),
      })}
      submitLabel="Save results"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't save the results. Please try again."
      onSubmit={async (values) => {
        const sent = values.results
          .filter((row) => row.value.trim())
          .map((row) => ({
            item_id: row.item_id,
            value: row.value.trim(),
            ...(row.notes.trim() ? { notes: row.notes.trim() } : {}),
          }))
        try {
          const saved = await enter.mutateAsync(sent)
          const done = saved.items.filter((i) => i.result_value !== null).length
          const progress =
            saved.status === 'results_entered'
              ? 'All results entered — awaiting release'
              : `${done} of ${saved.items.length} results entered`
          return saved.has_critical
            ? `${progress}. A critical result was flagged and the ordering doctor has been notified.`
            : progress
        } catch (err) {
          // A rejected value is named by its position in what was sent.
          const refused = fieldErrorsOf(err)
            .map((fe) => {
              const [, at] = /^results\.(\d+)\./.exec(fe.field) ?? []
              const item = at === undefined ? undefined : order.items.find((i) => i.id === sent[Number(at)]?.item_id)
              return item ? `${item.test_name}: ${fe.message}` : null
            })
            .filter((line): line is string => line !== null)
          if (refused.length > 0) throw new FormRefusal(refused.join(' '))
          throw err
        }
      }}
    >
      {(form) => <ResultRows form={form} order={order} />}
    </FormDialog>
  )
}

/** Release a fully-resulted order (`POST /lab-orders/{id}/release`). */
function ReleaseOrderDialog({ order }: { order: LabOrder }) {
  const release = useReleaseLabOrder(order.id)
  return (
    <FormDialog<ConfirmValues>
      trigger={
        <Button size="sm">
          <Send className="size-4" /> Release results
        </Button>
      }
      title="Release these results?"
      description={
        <>
          {plural(order.items.length, 'result', 'results')} for {order.patient_name} will be
          released and {order.doctor_name} will be notified. After release a result can only be
          changed by an amendment, which keeps the original value on record.
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Release results"
      pendingLabel="Releasing…"
      dismissLabel="Not yet"
      fallbackError="Couldn't release the results. Please try again."
      onSubmit={async () => {
        await release.mutateAsync()
        return `Results released for ${order.patient_name}`
      }}
    >
      {() => null}
    </FormDialog>
  )
}

const cancelSchema = z.object({
  reason: z
    .string()
    .trim()
    .min(1, 'Give a reason for cancelling')
    .max(500, 'Keep the reason under 500 characters'),
})

type CancelValues = z.infer<typeof cancelSchema>

/** Cancel an order that has not been released (`POST /lab-orders/{id}/cancel`). */
function CancelOrderDialog({ order }: { order: LabOrder }) {
  const cancel = useCancelLabOrder(order.id)
  return (
    <FormDialog<CancelValues>
      trigger={
        <Button variant="outline" size="sm" className="text-error hover:text-error">
          <XCircle className="size-4" /> Cancel order
        </Button>
      }
      title="Cancel this lab order?"
      description={
        <>
          The whole order for {order.patient_name} is cancelled; a single test cannot be removed.
          {order.invoice_id
            ? ' The tests already charged to the invoice stay on it — Billing has to correct the invoice by hand.'
            : ''}
        </>
      }
      resolver={zodResolver(cancelSchema)}
      defaults={() => ({ reason: '' })}
      submitLabel="Cancel order"
      pendingLabel="Cancelling…"
      destructive
      dismissLabel="Keep order"
      fallbackError="Couldn't cancel the lab order. Please try again."
      onSubmit={async (values) => {
        await cancel.mutateAsync(values.reason)
        return `Lab order cancelled for ${order.patient_name}`
      }}
    >
      {(form) => (
        <Field label="Reason" required error={form.formState.errors.reason?.message}>
          {(p) => (
            <Textarea rows={3} placeholder="e.g. Ordered for the wrong visit" {...p} {...form.register('reason')} />
          )}
        </Field>
      )}
    </FormDialog>
  )
}

/**
 * The actions an order offers in its current status, each shown only to a
 * user holding the permission its endpoint requires
 * (docs/18-API_CONTRACTS.md §8.1, §8.5). A convenience: the server enforces
 * both the status and the permission.
 */
export function LabOrderActions({ order }: { order: LabOrder }) {
  const { can } = usePermissions()
  const canCollect = order.status === 'ordered' && can('lab.order.collect_sample')
  const canEnter = LAB_RESULTS_ENTERABLE.has(order.status) && can('lab.order.enter_results')
  const canRelease = order.status === 'results_entered' && can('lab.order.release')
  const canCancel = LAB_CANCELLABLE.has(order.status) && can('lab.order.cancel')
  if (!canCollect && !canEnter && !canRelease && !canCancel) return null

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {canCollect && <CollectSamplesDialog order={order} />}
      {canRelease && <ReleaseOrderDialog order={order} />}
      {canEnter && <EnterResultsDialog order={order} />}
      {canCancel && <CancelOrderDialog order={order} />}
    </div>
  )
}

/**
 * Correct a released result
 * (`POST /lab-orders/{order_id}/items/{item_id}/amend`). The original value is
 * kept by the server in the item's history; this never overwrites it silently.
 */
export function AmendResultDialog({ order, item }: { order: LabOrder; item: LabOrderItem }) {
  const amend = useAmendLabResult(order.id)
  const current = item.result_value ?? ''
  const numeric = item.result_type === 'numeric'

  const schema = z
    .object({
      new_value: z
        .string()
        .trim()
        .min(1, 'Enter the corrected result')
        .max(2000, 'Keep the result under 2,000 characters'),
      reason: z
        .string()
        .trim()
        .min(1, 'Say why the result is being corrected')
        .max(500, 'Keep the reason under 500 characters'),
    })
    .superRefine((values, ctx) => {
      if (numeric && values.new_value && !NUMERIC.test(values.new_value)) {
        ctx.addIssue({ code: 'custom', path: ['new_value'], message: 'Enter a number' })
      } else if (values.new_value === current.trim()) {
        ctx.addIssue({
          code: 'custom',
          path: ['new_value'],
          message: 'This is the same as the released result',
        })
      }
    })

  type AmendValues = z.infer<typeof schema>

  return (
    <FormDialog<AmendValues>
      trigger={
        <Button variant="outline" size="sm" aria-label={`Amend ${item.test_name} result`}>
          <Pencil className="size-4" /> Amend
        </Button>
      }
      title={`Amend ${item.test_name}`}
      description={
        <>
          This result has been released. The correction is recorded as an amendment: the original
          value, the new one, who made it and why are all kept, and {order.doctor_name} is
          notified.
        </>
      }
      resolver={zodResolver(schema)}
      defaults={() => ({ new_value: '', reason: '' })}
      submitLabel="Save amendment"
      pendingLabel="Saving…"
      fallbackError="Couldn't save the amendment. Please try again."
      onSubmit={async (values) => {
        await amend.mutateAsync({ itemId: item.id, new_value: values.new_value, reason: values.reason })
        return `${item.test_name} amended`
      }}
    >
      {(form) => (
        <>
          <dl className="neo-pressed bg-surface flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl px-4 py-3">
            <dt className="font-label text-label-caps text-on-surface-variant">Released result</dt>
            <dd className="font-body text-body-sm text-on-surface flex flex-wrap items-center gap-2 font-semibold tabular-nums">
              {resultLabel(current, item.result_unit)}
              <LabResultFlagBadge flag={item.result_flag} />
            </dd>
          </dl>
          <Field
            label="Corrected result"
            required
            hint={item.result_unit ? `In ${item.result_unit}` : undefined}
            error={form.formState.errors.new_value?.message}
          >
            {(p) => (
              <Input
                inputMode={numeric ? 'decimal' : 'text'}
                autoComplete="off"
                {...p}
                {...form.register('new_value')}
              />
            )}
          </Field>
          <Field label="Reason for the correction" required error={form.formState.errors.reason?.message}>
            {(p) => (
              <Textarea rows={2} placeholder="e.g. Transcription error" {...p} {...form.register('reason')} />
            )}
          </Field>
        </>
      )}
    </FormDialog>
  )
}
