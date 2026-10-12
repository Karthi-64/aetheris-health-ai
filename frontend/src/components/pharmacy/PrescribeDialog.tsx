import type { ReactNode } from 'react'
import { Controller, useFieldArray, useWatch, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Textarea } from '@/components/ui/textarea'
import { FormDialog } from '@/components/forms/FormDialog'
import { useCreatePrescription } from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime } from '@/lib/format'
import { MedicinePicker } from './MedicinePicker'
import type { PrescribableVisit } from './pharmacyPresentation'
import {
  MAX_LINES,
  emptyLine,
  prescribeSchema,
  toPrescriptionBody,
  type LineSource,
  type PrescribeValues,
} from './prescribeForm'

/**
 * The prescribed lines. Each is either a catalog medicine or a name typed for
 * something the pharmacy does not stock — the API takes exactly one of the two
 * per line (docs/18-API_CONTRACTS.md §9.4).
 *
 * No stock is shown or asked for: a doctor cannot read it (§9.2), and the API
 * does not check it when a prescription is written.
 */
function MedicineLines({ form, canPick }: { form: UseFormReturn<PrescribeValues>; canPick: boolean }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: 'items' })
  const lines = useWatch({ control: form.control, name: 'items' })
  const errors = form.formState.errors.items
  const listError = errors?.root?.message ?? errors?.message

  return (
    <fieldset className="min-w-0 space-y-3">
      <legend className="font-display text-primary text-base font-bold">Medicines</legend>
      {listError && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}

      {fields.map((field, index) => {
        const e = errors?.[index]
        const n = index + 1
        const source: LineSource = lines[index]?.source ?? field.source
        // A catalog medicine may appear once on a prescription (§9.4).
        const taken = lines
          .filter((other, at) => at !== index && other.source === 'catalog' && other.medicine_id)
          .map((other) => other.medicine_id)

        return (
          <div
            key={field.id}
            role="group"
            aria-label={`Medicine ${n}`}
            className="neo-pressed bg-surface min-w-0 space-y-3 rounded-xl p-3"
          >
            <div className="flex items-center justify-between gap-3">
              <p className="font-label text-label-caps text-on-surface-variant">Medicine {n}</p>
              {fields.length > 1 && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="text-error hover:text-error"
                  aria-label={`Remove medicine ${n}`}
                  onClick={() => remove(index)}
                >
                  <Trash2 className="size-4" /> Remove
                </Button>
              )}
            </div>

            {canPick && (
              <Controller
                control={form.control}
                name={`items.${index}.source`}
                render={({ field: f }) => (
                  <RadioGroup
                    value={f.value}
                    aria-label={`Where medicine ${n} comes from`}
                    className="flex flex-wrap gap-x-5 gap-y-2"
                    onValueChange={(next) => {
                      f.onChange(next as LineSource)
                      // The picker forgets its medicine once hidden, so the id
                      // goes with it and stops blocking it on the other lines.
                      if (next === 'free_text') form.setValue(`items.${index}.medicine_id`, '')
                      form.clearErrors([`items.${index}.medicine_id`, `items.${index}.medicine_name`])
                    }}
                  >
                    <label className="font-body text-body-sm text-on-surface flex items-center gap-2">
                      <RadioGroupItem value="catalog" /> From the catalog
                    </label>
                    <label className="font-body text-body-sm text-on-surface flex items-center gap-2">
                      <RadioGroupItem value="free_text" /> Not in the catalog
                    </label>
                  </RadioGroup>
                )}
              />
            )}

            {source === 'catalog' ? (
              <Controller
                control={form.control}
                name={`items.${index}.medicine_id`}
                render={({ field: f }) => (
                  <Field label="Medicine" required error={e?.medicine_id?.message}>
                    {(p) => (
                      <MedicinePicker
                        id={p.id}
                        invalid={p['aria-invalid']}
                        value={f.value}
                        excludeIds={taken}
                        onChange={(medicine) => f.onChange(medicine?.id ?? '')}
                      />
                    )}
                  </Field>
                )}
              />
            ) : (
              <>
                <Field label="Medicine name" required error={e?.medicine_name?.message}>
                  {(p) => (
                    <Input
                      autoComplete="off"
                      placeholder="e.g. Vitamin D drops"
                      {...p}
                      {...form.register(`items.${index}.medicine_name`)}
                    />
                  )}
                </Field>
                <p className="font-body text-outline text-xs">
                  Recorded on the prescription, but it cannot be dispensed here.
                </p>
              </>
            )}

            {e?.message && (
              <p className="font-body text-error text-xs" role="alert">
                {e.message}
              </p>
            )}

            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Dosage" required error={e?.dosage?.message}>
                {(p) => (
                  <Input
                    autoComplete="off"
                    placeholder="e.g. 1 tablet"
                    {...p}
                    {...form.register(`items.${index}.dosage`)}
                  />
                )}
              </Field>
              <Field label="Frequency" required error={e?.frequency?.message}>
                {(p) => (
                  <Input
                    autoComplete="off"
                    placeholder="e.g. three times daily"
                    {...p}
                    {...form.register(`items.${index}.frequency`)}
                  />
                )}
              </Field>
              <Field label="Duration" hint="Days, optional" error={e?.duration_days?.message}>
                {(p) => (
                  <Input
                    inputMode="numeric"
                    autoComplete="off"
                    {...p}
                    {...form.register(`items.${index}.duration_days`)}
                  />
                )}
              </Field>
              <Field label="Quantity" required hint="Whole units in total" error={e?.quantity?.message}>
                {(p) => (
                  <Input
                    inputMode="numeric"
                    autoComplete="off"
                    {...p}
                    {...form.register(`items.${index}.quantity`)}
                  />
                )}
              </Field>
            </div>
            <Field label="Instructions" hint="Optional" error={e?.instructions?.message}>
              {(p) => (
                <Textarea
                  rows={2}
                  placeholder="e.g. After food"
                  {...p}
                  {...form.register(`items.${index}.instructions`)}
                />
              )}
            </Field>
          </div>
        )
      })}

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={fields.length >= MAX_LINES}
          onClick={() => append(emptyLine(canPick ? 'catalog' : 'free_text'))}
        >
          <Plus className="size-4" /> Add medicine
        </Button>
        {fields.length >= MAX_LINES && (
          <p className="font-body text-outline text-xs">
            A prescription can hold at most {MAX_LINES} medicines.
          </p>
        )}
      </div>
    </fieldset>
  )
}

/**
 * Write a prescription for a visit (`POST /prescriptions`).
 *
 * A prescription hangs off an appointment: the patient and the prescriber are
 * read from it by the server, so they are shown here and never sent. Nothing
 * is charged and no stock moves until the pharmacy dispenses (§9.5, §9.6) —
 * the dialog says so.
 */
export function PrescribeDialog({ visit, trigger }: { visit: PrescribableVisit; trigger: ReactNode }) {
  const { can } = usePermissions()
  const create = useCreatePrescription()
  // The picker lists the catalog, which needs its own permission (§9.1). A
  // user without it writes each medicine by name and no catalog request is made.
  const canPick = can('pharmacy.medicine.read')

  return (
    <FormDialog<PrescribeValues>
      trigger={trigger}
      title="Write prescription"
      description={
        <>
          For {visit.patient_name}, from the visit with {visit.doctor_name} on{' '}
          {formatDateTime(visit.scheduled_start)}. {visit.doctor_name} is recorded as the
          prescriber. Nothing is charged until the pharmacy dispenses it.
        </>
      }
      resolver={zodResolver(prescribeSchema)}
      defaults={() => ({ notes: '', items: [emptyLine(canPick ? 'catalog' : 'free_text')] })}
      submitLabel="Write prescription"
      pendingLabel="Writing…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't write the prescription. Please try again."
      onSubmit={async (values) => {
        const prescription = await create.mutateAsync(toPrescriptionBody(visit.id, values))
        const count = prescription.items.length
        return `Prescription written — ${count} ${count === 1 ? 'medicine' : 'medicines'} for ${prescription.patient_name}`
      }}
    >
      {(form) => (
        <>
          <MedicineLines form={form} canPick={canPick} />
          {!canPick && (
            <p className="font-body text-outline text-xs">
              Your account cannot read the medicine catalog, so each medicine is written by name.
            </p>
          )}
          <Field label="Notes" hint="Optional" error={form.formState.errors.notes?.message}>
            {(p) => (
              <Textarea
                rows={2}
                placeholder="e.g. Review in five days"
                {...p}
                {...form.register('notes')}
              />
            )}
          </Field>
        </>
      )}
    </FormDialog>
  )
}
