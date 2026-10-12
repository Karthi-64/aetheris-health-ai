import type { ReactNode } from 'react'
import { Controller, useFieldArray, useWatch, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { FormDialog } from '@/components/forms/FormDialog'
import { useCreateLabTest, useUpdateLabTest, type LabTest } from '@/api/lab'
import {
  EMPTY_RANGE,
  EMPTY_TEST,
  labTestSchema,
  toCreateBody,
  toLabTestValues,
  toUpdateBody,
  type LabTestValues,
} from './labTestForm'

const SEXES = [
  { value: 'any', label: 'Any' },
  { value: 'male', label: 'Male' },
  { value: 'female', label: 'Female' },
] as const

function RangeFields({ form }: { form: UseFormReturn<LabTestValues> }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: 'reference_ranges' })
  const errors = form.formState.errors.reference_ranges
  const listError = errors?.root?.message ?? errors?.message

  return (
    <fieldset className="border-outline-variant/30 min-w-0 space-y-3 border-t pt-4">
      <legend className="font-display text-primary pr-3 text-base font-bold">Reference ranges</legend>
      <p className="font-body text-outline text-xs">
        One band per sex and age group. A result is judged by the server against the band that
        fits the patient; a value at or beyond a critical bound is flagged critical. These are
        this hospital's own values — enter them from your laboratory's validated ranges.
      </p>
      {listError && (
        <p className="font-body text-error text-xs" role="alert">
          {listError}
        </p>
      )}

      {fields.map((field, index) => {
        const e = errors?.[index]
        return (
          <div key={field.id} className="neo-pressed bg-surface space-y-3 rounded-xl p-3">
            <div className="flex items-center justify-between gap-3">
              <p className="font-label text-label-caps text-on-surface-variant">Band {index + 1}</p>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="text-error hover:text-error"
                aria-label={`Remove band ${index + 1}`}
                onClick={() => remove(index)}
              >
                <Trash2 className="size-4" /> Remove
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Controller
                control={form.control}
                name={`reference_ranges.${index}.sex`}
                render={({ field: f }) => (
                  <Field label="Sex" error={e?.sex?.message}>
                    {(p) => (
                      <Select value={f.value} onValueChange={f.onChange}>
                        <SelectTrigger id={p.id} ref={f.ref} aria-invalid={p['aria-invalid']}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {SEXES.map((s) => (
                            <SelectItem key={s.value} value={s.value}>
                              {s.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  </Field>
                )}
              />
              <Field label="Youngest age" hint="Years, optional" error={e?.age_min?.message}>
                {(p) => <Input inputMode="numeric" {...p} {...form.register(`reference_ranges.${index}.age_min`)} />}
              </Field>
              <Field label="Oldest age" hint="Years, optional" error={e?.age_max?.message}>
                {(p) => <Input inputMode="numeric" {...p} {...form.register(`reference_ranges.${index}.age_max`)} />}
              </Field>
              <Field label="Low" error={e?.low?.message}>
                {(p) => <Input inputMode="decimal" {...p} {...form.register(`reference_ranges.${index}.low`)} />}
              </Field>
              <Field label="High" error={e?.high?.message}>
                {(p) => <Input inputMode="decimal" {...p} {...form.register(`reference_ranges.${index}.high`)} />}
              </Field>
              <span className="hidden sm:block" />
              <Field label="Critical low" hint="Optional" error={e?.critical_low?.message}>
                {(p) => (
                  <Input inputMode="decimal" {...p} {...form.register(`reference_ranges.${index}.critical_low`)} />
                )}
              </Field>
              <Field label="Critical high" hint="Optional" error={e?.critical_high?.message}>
                {(p) => (
                  <Input inputMode="decimal" {...p} {...form.register(`reference_ranges.${index}.critical_high`)} />
                )}
              </Field>
            </div>
          </div>
        )
      })}

      <Button type="button" variant="outline" size="sm" onClick={() => append(EMPTY_RANGE)}>
        <Plus className="size-4" /> Add a band
      </Button>
    </fieldset>
  )
}

function TestFields({ form, editing }: { form: UseFormReturn<LabTestValues>; editing: boolean }) {
  const { errors } = form.formState
  const resultType = useWatch({ control: form.control, name: 'result_type' })

  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Code"
          required
          hint={editing ? "Can't be changed" : 'e.g. HB — saved in capitals'}
          error={errors.code?.message}
        >
          {(p) => <Input className="uppercase" disabled={editing} {...p} {...form.register('code')} />}
        </Field>
        <Field label="Name" required error={errors.name?.message}>
          {(p) => <Input {...p} {...form.register('name')} />}
        </Field>
        <Field label="Category" hint="Optional, e.g. Haematology" error={errors.category?.message}>
          {(p) => <Input {...p} {...form.register('category')} />}
        </Field>
        <Controller
          control={form.control}
          name="result_type"
          render={({ field }) => (
            <Field
              label="Result type"
              required
              hint={editing ? "Can't be changed" : 'Numeric results are checked against ranges'}
              error={errors.result_type?.message}
            >
              {(p) => (
                <Select value={field.value} onValueChange={field.onChange} disabled={editing}>
                  <SelectTrigger id={p.id} ref={field.ref} aria-invalid={p['aria-invalid']}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="numeric">Numeric</SelectItem>
                    <SelectItem value="text">Text</SelectItem>
                  </SelectContent>
                </Select>
              )}
            </Field>
          )}
        />
        <Field label="Unit" hint="Optional, e.g. g/dL" error={errors.unit?.message}>
          {(p) => <Input {...p} {...form.register('unit')} />}
        </Field>
        <Field label="Price" required error={errors.price?.message}>
          {(p) => <Input inputMode="decimal" {...p} {...form.register('price')} />}
        </Field>
        <Field label="Turnaround" hint="Expected hours, optional" error={errors.turnaround_hours?.message}>
          {(p) => <Input inputMode="numeric" {...p} {...form.register('turnaround_hours')} />}
        </Field>
        {editing && (
          <Controller
            control={form.control}
            name="is_active"
            render={({ field }) => (
              <label className="font-body text-body-sm text-on-surface flex items-center gap-2 self-end pb-2.5">
                <Checkbox checked={field.value} onCheckedChange={(v) => field.onChange(v === true)} />
                Active — can be ordered
              </label>
            )}
          />
        )}
      </div>

      {resultType === 'numeric' ? (
        <RangeFields form={form} />
      ) : (
        <p className="font-body text-body-sm text-on-surface-variant border-outline-variant/30 border-t pt-4">
          A text result is recorded as written and is not checked against a range.
        </p>
      )}
    </>
  )
}

/** Add a test to the catalog (`POST /tests-catalog`). */
export function CreateLabTestDialog({ trigger }: { trigger: ReactNode }) {
  const create = useCreateLabTest()
  return (
    <FormDialog<LabTestValues>
      trigger={trigger}
      title="Add a test"
      description="Add a test doctors can order. The code and result type can't be changed afterwards."
      resolver={zodResolver(labTestSchema)}
      defaults={() => EMPTY_TEST}
      submitLabel="Add test"
      pendingLabel="Adding…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't add the test. Please try again."
      onSubmit={async (values) => {
        const test = await create.mutateAsync(toCreateBody(values))
        return `Added ${test.name} (${test.code})`
      }}
    >
      {(form) => <TestFields form={form} editing={false} />}
    </FormDialog>
  )
}

/**
 * Edit a catalog test (`PATCH /tests-catalog/{id}`). Only what changed is
 * sent; an order already placed keeps the price and range it was placed with.
 */
export function EditLabTestDialog({ test, trigger }: { test: LabTest; trigger: ReactNode }) {
  const update = useUpdateLabTest(test.id)
  return (
    <FormDialog<LabTestValues>
      trigger={trigger}
      title={`Edit ${test.name}`}
      description="Changes apply to orders placed from now on. Orders already placed are not altered."
      resolver={zodResolver(labTestSchema)}
      defaults={() => toLabTestValues(test)}
      submitLabel="Save changes"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't save the test. Please try again."
      onSubmit={async (values, opened) => {
        const body = toUpdateBody(opened, values)
        if (Object.keys(body).length === 0) return 'Nothing to save — the test is unchanged'
        const saved = await update.mutateAsync(body)
        return `Saved ${saved.name}`
      }}
    >
      {(form) => <TestFields form={form} editing />}
    </FormDialog>
  )
}
