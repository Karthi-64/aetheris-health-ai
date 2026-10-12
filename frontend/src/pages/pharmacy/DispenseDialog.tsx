import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useForm, useWatch } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { toast } from 'sonner'
import { Loader2, Pill } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { DataTable } from '@/components/ui/data-table'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  pharmacyKeys,
  shortagesOf,
  useDispensePrescription,
  type Dispense,
  type DispenseInput,
  type DispenseItem,
  type DispenseShortage,
  type Prescription,
} from '@/api/pharmacy'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { fieldErrorsOf } from '@/lib/apiErrors'
import { formatDate, formatDateTime, formatMoney } from '@/lib/format'
import {
  asksForNothing,
  canBeDispensed,
  dispenseDefaults,
  dispenseSchema,
  isPartial,
  outstandingLines,
  placeDispenseErrors,
  toDispenseBody,
  type DispenseValues,
} from './dispenseForm'

/** Why the last attempt did not dispense, kept above the form so it can be acted on. */
type Refusal =
  | { kind: 'shortage'; shortages: DispenseShortage[] }
  /** The short lines were set to what the server said it could supply. */
  | { kind: 'adjusted' }
  /** The same, and it left nothing to ask for: none of it is in stock. */
  | { kind: 'unavailable' }
  | { kind: 'message'; text: string }
  /** A 5xx or the network: the outcome is not known. */
  | { kind: 'unconfirmed' }

const UNCONFIRMED =
  "The dispense couldn't be confirmed. Check this prescription's dispense history before trying again, so nothing is dispensed twice."

/** A batch drawn on, named as the prescription names its line. */
interface BatchRow extends DispenseItem {
  line_name: string
}

/**
 * An expiry date is a plain day (`YYYY-MM-DD`). Read as an instant it would be
 * midnight UTC, which is the day before anywhere west of Greenwich — so it is
 * read as that day on the viewer's own calendar.
 */
const expiryLabel = (date: string) => formatDate(`${date}T00:00:00`)

/** Sorting is off: the rows are the server's allocation, shown in the prescription's order. */
const batchColumns: ColumnDef<BatchRow>[] = [
  {
    id: 'medicine',
    header: 'Medicine',
    enableSorting: false,
    cell: ({ row }) => <span className="text-on-surface font-semibold">{row.original.line_name}</span>,
  },
  {
    accessorKey: 'batch_number',
    header: 'Batch',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="font-mono text-xs [overflow-wrap:anywhere]">{row.original.batch_number}</span>
    ),
  },
  {
    accessorKey: 'expiry_date',
    header: 'Expires',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="whitespace-nowrap tabular-nums">{expiryLabel(row.original.expiry_date)}</span>
    ),
  },
  {
    accessorKey: 'quantity',
    header: 'Quantity',
    enableSorting: false,
    cell: ({ row }) => <span className="tabular-nums">{row.original.quantity}</span>,
  },
  {
    accessorKey: 'unit_price',
    header: 'Unit price',
    enableSorting: false,
    cell: ({ row }) => <span className="tabular-nums">{formatMoney(row.original.unit_price)}</span>,
  },
  {
    accessorKey: 'total',
    header: 'Line total',
    enableSorting: false,
    cell: ({ row }) => <span className="font-semibold tabular-nums">{formatMoney(row.original.total)}</span>,
  },
]

/**
 * The batches of one dispense, grouped by prescription line. A dispense has
 * one item per batch drawn on, in no guaranteed order, and names the medicine
 * as the catalog does now ("Paracetamol") — so each is tied to its line by
 * `prescription_item_id` and carries the name written on the prescription
 * ("Paracetamol 500 mg"). Within a line the server's order is kept.
 */
function batchRows(prescription: Pick<Prescription, 'items'>, dispense: Dispense): BatchRow[] {
  const lines = new Map(prescription.items.map((item, at) => [item.id, { at, name: item.medicine_name }]))
  return dispense.items
    .map((item, at) => ({ item, at, line: lines.get(item.prescription_item_id) }))
    .sort(
      (a, b) =>
        (a.line?.at ?? Number.MAX_SAFE_INTEGER) - (b.line?.at ?? Number.MAX_SAFE_INTEGER) || a.at - b.at,
    )
    .map(({ item, line }) => ({ ...item, line_name: line?.name ?? item.medicine_name }))
}

/**
 * The batches the server drew on for a dispense — its choice, its prices and
 * its line totals, none of them worked out here (docs/18-API_CONTRACTS.md §9.5).
 */
export function DispensedBatches({
  prescription,
  dispense,
}: {
  prescription: Pick<Prescription, 'items'>
  dispense: Dispense
}) {
  const rows = useMemo(() => batchRows(prescription, dispense), [prescription, dispense])
  return (
    <DataTable
      columns={batchColumns}
      data={rows}
      // One page: a dispense is read whole.
      pageSize={Math.max(rows.length, 1)}
      emptyState={
        <p className="font-body text-body-sm text-on-surface-variant py-6 text-center">
          No batch details were returned for this dispense.
        </p>
      }
    />
  )
}

function RefusalAlert({ refusal, onUseAvailable }: { refusal: Refusal; onUseAvailable?: () => void }) {
  if (refusal.kind === 'adjusted') {
    return (
      <Alert variant="info" title="Quantities set to what is available">
        Nothing has been dispensed yet. Give the reason the rest is not being dispensed, then
        dispense.
      </Alert>
    )
  }
  if (refusal.kind === 'unavailable') {
    return (
      <Alert variant="warning" title="Nothing can be dispensed now">
        None of what was asked for is in stock, and nothing has been dispensed.
      </Alert>
    )
  }
  if (refusal.kind === 'unconfirmed') {
    return (
      <Alert variant="error" title="Dispense not confirmed">
        {UNCONFIRMED}
      </Alert>
    )
  }
  if (refusal.kind === 'message') {
    return (
      <Alert variant="error" title="Nothing was dispensed">
        {refusal.text}
      </Alert>
    )
  }
  return (
    <Alert variant="error" title="Nothing was dispensed">
      <div className="flex flex-col items-start gap-3">
        <p>
          There is not enough stock for everything asked for, so nothing was taken from stock and
          nothing was charged.
        </p>
        <ul className="list-disc space-y-1 pl-5">
          {refusal.shortages.map((s) => (
            <li key={s.prescription_item_id}>
              {s.medicine}: asked for {s.requested}, {s.available} available
            </li>
          ))}
        </ul>
        {onUseAvailable && (
          <Button type="button" variant="outline" size="sm" onClick={onUseAvailable}>
            Set these to what is available
          </Button>
        )}
      </div>
    </Alert>
  )
}

function RefusalNotice({ refusal, onUseAvailable }: { refusal: Refusal | null; onUseAvailable?: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  // On a long prescription the notice renders above the first row while the
  // user is at the button below the last one.
  useEffect(() => {
    if (refusal) ref.current?.scrollIntoView({ block: 'nearest' })
  }, [refusal])
  if (!refusal) return null
  return (
    <div ref={ref}>
      <RefusalAlert refusal={refusal} onUseAvailable={onUseAvailable} />
    </div>
  )
}

/**
 * Step 1. One row per catalog line with units outstanding; free-text lines are
 * listed as not stocked and never sent.
 *
 * Nothing about stock is decided here. "Available today" is the server's
 * `available_quantity`, the batches and the price are chosen by the server,
 * and a shortage is whatever the server says it is when it answers 409.
 */
function DispenseForm({
  prescription,
  refusal,
  onRefusal,
  onBusyChange,
  onDispensed,
  onClose,
}: {
  prescription: Prescription
  refusal: Refusal | null
  onRefusal: (refusal: Refusal | null) => void
  onBusyChange: (busy: boolean) => void
  onDispensed: (dispense: Dispense) => void
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const dispense = useDispensePrescription(prescription.id)
  // No `Idempotency-Key` is honoured, and a repeated partial dispense is
  // dispensed and billed again (§9.5). `isPending` only disables the button
  // after a re-render; this also stops a second submit fired before that.
  const submitting = useRef(false)

  // The lines as the server has them now. The rows are fixed when the form
  // opens; a refetch after a refusal changes what is left or available on
  // them, never which lines there are to add.
  const lines = useMemo(() => outstandingLines(prescription), [prescription])
  const [opened] = useState(() => dispenseDefaults(lines))
  const resolver = useMemo(() => zodResolver(dispenseSchema(lines)), [lines])
  const {
    control,
    register,
    handleSubmit,
    getValues,
    setError,
    setFocus,
    setValue,
    formState: { errors },
  } = useForm<DispenseValues>({ resolver, defaultValues: opened })
  const rows = useWatch({ control, name: 'lines' })
  // What is typed now, for the rules that depend on it; the reason is not one of them.
  const typed: DispenseValues = { lines: rows, notes: '' }
  const partial = isPartial(lines, typed)
  const listError = errors.lines?.root?.message ?? errors.lines?.message
  const freeText = prescription.items.filter((item) => item.medicine_id === null)

  function applyAvailable(shortages: DispenseShortage[]) {
    for (const shortage of shortages) {
      const index = opened.lines.findIndex((row) => row.prescription_item_id === shortage.prescription_item_id)
      if (index >= 0) {
        setValue(`lines.${index}.quantity`, String(shortage.available), { shouldDirty: true, shouldValidate: true })
      }
    }
    // Out of stock altogether leaves every line at 0, and nothing to send.
    if (asksForNothing(lines, { lines: getValues('lines'), notes: '' })) {
      onRefusal({ kind: 'unavailable' })
      return
    }
    onRefusal({ kind: 'adjusted' })
    setFocus('notes')
  }

  function refuse(err: unknown, sent: DispenseInput, values: DispenseValues) {
    const status = err instanceof ApiError ? err.status : undefined
    const shortages = shortagesOf(err)
    if (shortages.length > 0) {
      // The hook refetches the prescription, so "Available today" catches up.
      onRefusal({ kind: 'shortage', shortages })
      toast.error('Nothing was dispensed — not enough stock.')
      return
    }
    if (err instanceof ApiError && status === 422) {
      const placed = placeDispenseErrors(fieldErrorsOf(err), sent, values)
      placed.rows.forEach(({ index, message }, n) =>
        setError(`lines.${index}.quantity`, { message }, { shouldFocus: n === 0 }),
      )
      if (placed.notes) setError('notes', { message: placed.notes }, { shouldFocus: placed.rows.length === 0 })
      // A refused quantity means what is left changed since this was loaded.
      // The hook refetches on a 400, 404 or 409 only, so it is asked for here.
      if (placed.rows.length > 0) {
        queryClient.invalidateQueries({ queryKey: pharmacyKeys.prescription(prescription.id) })
      }
      const onForm = placed.rows.length > 0 || placed.notes !== null
      const text = placed.other.length > 0 ? placed.other.join(' ') : onForm ? null : err.message
      onRefusal(text ? { kind: 'message', text } : null)
      toast.error(text ?? 'Nothing was dispensed. Check the highlighted fields.')
      return
    }
    if (err instanceof ApiError && [400, 403, 404, 409, 429].includes(status ?? 0)) {
      // A definite no, in the API's own words. For a 400 the status moved on,
      // a medicine was deactivated or nothing is left — the API says which,
      // and the hook has refetched the prescription.
      const deactivated = (err.details as { medicine_id?: unknown } | null | undefined)?.medicine_id
      const line = typeof deactivated === 'string' ? lines.find((l) => l.medicine_id === deactivated) : undefined
      // "Dispense the rest" is only advice when there is a rest: a request that
      // includes an inactive medicine's line is refused whole, and with no other
      // line outstanding there is nothing else to ask for.
      const text = !line
        ? err.message
        : lines.length > 1
          ? `${err.message} It is ${line.medicine_name} — set it to 0 to dispense the rest.`
          : `${err.message} It is ${line.medicine_name}, which cannot be dispensed while it is inactive.`
      onRefusal({ kind: 'message', text })
      toast.error(err.message)
      return
    }
    // A 5xx or a lost connection says nothing about whether the dispense went
    // through. The record is refetched so the screen shows what the server holds.
    queryClient.invalidateQueries({ queryKey: pharmacyKeys.prescription(prescription.id) })
    onRefusal({ kind: 'unconfirmed' })
    toast.error(UNCONFIRMED)
  }

  async function submit(values: DispenseValues) {
    if (submitting.current) return
    submitting.current = true
    onBusyChange(true)
    onRefusal(null)
    const body = toDispenseBody(lines, values)
    try {
      const done = await dispense.mutateAsync(body)
      toast.success(`Dispensed for ${prescription.patient_name} — total ${formatMoney(done.total_amount)}`)
      onDispensed(done)
    } catch (err) {
      refuse(err, body, values)
    } finally {
      submitting.current = false
      onBusyChange(false)
    }
  }

  return (
    <form onSubmit={(e) => handleSubmit(submit)(e)} className="space-y-4" noValidate>
      <RefusalNotice
        refusal={refusal}
        onUseAvailable={refusal?.kind === 'shortage' ? () => applyAvailable(refusal.shortages) : undefined}
      />

      <p className="font-body text-body-sm text-on-surface-variant">
        The system chooses the batches — earliest expiry first — and the price. A dispense is all
        or nothing: if any medicine asked for is short, nothing is dispensed.
      </p>

      <ul className="space-y-3" aria-label="Medicines to dispense">
        {opened.lines.map((row, index) => {
          const item = prescription.items.find((i) => i.id === row.prescription_item_id)
          if (!item) return null
          const outstanding = lines.some((l) => l.id === item.id)
          return (
            <li
              key={row.prescription_item_id}
              className="neo-pressed bg-surface grid min-w-0 gap-3 rounded-xl p-3 sm:grid-cols-[minmax(0,1fr)_12rem]"
            >
              <div className="min-w-0 space-y-1">
                <p className="font-body text-body-md text-on-surface font-semibold [overflow-wrap:anywhere]">
                  {item.medicine_name}
                </p>
                <p className="font-body text-on-surface-variant text-xs [overflow-wrap:anywhere]">
                  {item.dosage} · {item.frequency}
                </p>
                {outstanding ? (
                  <p className="font-body text-body-sm text-on-surface tabular-nums">
                    Remaining {item.quantity_remaining}
                    <span className="text-outline"> · </span>
                    {item.available_quantity === 0 ? (
                      <span className="text-error font-semibold">Out of stock</span>
                    ) : item.available_quantity === null ? (
                      'Availability not reported'
                    ) : (
                      <>Available today {item.available_quantity}</>
                    )}
                  </p>
                ) : (
                  <p className="font-body text-body-sm text-on-surface-variant">Nothing left to dispense.</p>
                )}
              </div>
              {outstanding && (
                <Field
                  label={`Quantity of ${item.medicine_name}`}
                  hint={`0 to ${item.quantity_remaining}`}
                  error={errors.lines?.[index]?.quantity?.message}
                >
                  {(p) => (
                    <Input
                      inputMode="numeric"
                      autoComplete="off"
                      {...p}
                      {...register(`lines.${index}.quantity`, { deps: ['notes'] })}
                    />
                  )}
                </Field>
              )}
            </li>
          )
        })}
      </ul>
      {listError && asksForNothing(lines, typed) && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}

      {freeText.length > 0 && (
        <div className="space-y-1">
          <p className="font-label text-label-caps text-on-surface-variant">Not stocked here</p>
          <ul className="font-body text-body-sm text-on-surface-variant list-disc space-y-1 pl-5">
            {freeText.map((item) => (
              <li key={item.id} className="[overflow-wrap:anywhere]">
                {item.medicine_name} — written by name, so it is not part of this dispense.
              </li>
            ))}
          </ul>
        </div>
      )}

      <Field
        label="Reason"
        required={partial}
        hint={
          partial
            ? 'Required: part of what was prescribed is not being dispensed now'
            : 'Optional — kept with the dispense'
        }
        error={errors.notes?.message}
      >
        {(p) => (
          <Textarea
            rows={2}
            placeholder="e.g. Rest tomorrow"
            aria-required={partial}
            {...p}
            {...register('notes')}
          />
        )}
      </Field>

      <DialogFooter>
        <Button type="button" variant="ghost" disabled={dispense.isPending} onClick={onClose}>
          Not now
        </Button>
        <Button type="submit" disabled={dispense.isPending} aria-busy={dispense.isPending}>
          {dispense.isPending && <Loader2 className="size-4 animate-spin" />}
          {dispense.isPending ? 'Dispensing…' : 'Dispense'}
        </Button>
      </DialogFooter>
    </form>
  )
}

/**
 * Step 2: what the server did, from the 201 body alone — its total, its
 * warnings word for word, and the batches it chose. Nothing here is worked out
 * in the browser.
 */
function DispenseResult({
  prescription,
  dispense,
  onDone,
}: {
  prescription: Prescription
  dispense: Dispense
  onDone: () => void
}) {
  const { canAny } = usePermissions()
  return (
    // `min-w-0`: the dialog panel is a grid, and without it a wide batch table
    // would stretch the panel instead of scrolling inside its own container.
    <div className="min-w-0 space-y-4">
      <DialogHeader>
        <DialogTitle>Dispensed</DialogTitle>
        <DialogDescription>
          {prescription.patient_name} · {formatDateTime(dispense.dispensed_at)}
        </DialogDescription>
      </DialogHeader>

      <dl className="neo-pressed bg-surface flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 rounded-xl px-4 py-3">
        <dt className="font-label text-label-caps text-on-surface-variant">Total</dt>
        <dd className="font-display text-primary text-2xl font-bold tabular-nums">
          {formatMoney(dispense.total_amount)}
        </dd>
      </dl>

      {dispense.warnings.map((warning) => (
        <Alert key={warning} variant="warning" title="Warning">
          {warning}
        </Alert>
      ))}

      <section className="space-y-2" aria-label="Batches dispensed">
        <h3 className="font-display text-primary text-base font-bold">Batches dispensed</h3>
        <DispensedBatches prescription={prescription} dispense={dispense} />
      </section>

      {dispense.notes && (
        <p className="font-body text-body-sm text-on-surface-variant [overflow-wrap:anywhere]">
          Note: {dispense.notes}
        </p>
      )}

      <p className="font-body text-body-sm text-on-surface">
        {dispense.invoice_id ? (
          <>
            Charged to the patient's draft invoice.{' '}
            {canAny('invoice.read') && (
              <Link to={`/billing/${dispense.invoice_id}`} className="text-secondary font-semibold hover:underline">
                View invoice
              </Link>
            )}
          </>
        ) : (
          'No invoice was linked to this dispense.'
        )}
      </p>

      <DialogFooter>
        <Button type="button" onClick={onDone}>
          Done
        </Button>
      </DialogFooter>
    </div>
  )
}

function DispenseSteps({
  prescription,
  onBusyChange,
  onClose,
}: {
  prescription: Prescription
  onBusyChange: (busy: boolean) => void
  onClose: () => void
}) {
  const [result, setResult] = useState<Dispense | null>(null)
  const [refusal, setRefusal] = useState<Refusal | null>(null)
  // The prescription is refetched before the dispense call settles. Until it
  // does, the form keeps the record it was submitted against, so it does not
  // redraw itself — or vanish, once nothing is left — under the user.
  const [submittedAgainst, setSubmittedAgainst] = useState<Prescription | null>(null)
  const shown = submittedAgainst ?? prescription

  if (result) return <DispenseResult prescription={shown} dispense={result} onDone={onClose} />

  const header = (
    <DialogHeader>
      <DialogTitle>Dispense prescription</DialogTitle>
      <DialogDescription>
        {shown.patient_name} · {shown.patient_mrn} · prescribed by {shown.doctor_name}
      </DialogDescription>
    </DialogHeader>
  )

  // Dispensed, cancelled or emptied by someone else while this was open.
  if (!canBeDispensed(shown)) {
    return (
      <div className="min-w-0 space-y-4">
        {header}
        <RefusalNotice refusal={refusal} />
        <p className="font-body text-body-sm text-on-surface-variant">
          Nothing is left to dispense on this prescription.
        </p>
        <DialogFooter>
          <Button type="button" onClick={onClose}>
            Back to the prescription
          </Button>
        </DialogFooter>
      </div>
    )
  }

  return (
    <div className="min-w-0 space-y-4">
      {header}
      <DispenseForm
        prescription={shown}
        refusal={refusal}
        onRefusal={setRefusal}
        onBusyChange={(busy) => {
          setSubmittedAgainst(busy ? prescription : null)
          onBusyChange(busy)
        }}
        onDispensed={setResult}
        onClose={onClose}
      />
    </div>
  )
}

/**
 * Dispense a prescription (`POST /prescriptions/{id}/dispense`) in two steps:
 * the quantities, then what the server did. The result is shown, not just
 * toasted — the batches and the expiry warnings are what the pharmacist
 * needs in hand.
 *
 * Renders nothing when the prescription has nothing to dispense, but stays
 * mounted while open: a full dispense turns the prescription `dispensed`, and
 * the result must outlive that.
 */
export function DispenseDialog({ prescription }: { prescription: Prescription }) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  if (!open && !canBeDispensed(prescription)) return null

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Closing mid-request would throw away the only view of the result.
        if (!busy) setOpen(next)
      }}
    >
      <DialogTrigger asChild>
        <Button size="sm">
          <Pill className="size-4" /> Dispense
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DispenseSteps prescription={prescription} onBusyChange={setBusy} onClose={() => setOpen(false)} />
      </DialogContent>
    </Dialog>
  )
}
