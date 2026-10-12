import type { ReactNode } from 'react'
import { Controller, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Checkbox } from '@/components/ui/checkbox'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { FormDialog } from '@/components/forms/FormDialog'
import { useCreateMedicine, useUpdateMedicine, type Medicine } from '@/api/pharmacy'
import {
  EMPTY_MEDICINE,
  createMedicineSchema,
  editMedicineSchema,
  toCreateBody,
  toMedicineValues,
  toUpdateBody,
  type MedicineValues,
} from './medicineForm'

function MedicineFields({ form, editing }: { form: UseFormReturn<MedicineValues>; editing: boolean }) {
  const { errors } = form.formState

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field
        label="SKU"
        required
        hint={editing ? "Can't be changed" : "e.g. PARA-500 — saved in capitals, and can't be changed later"}
        error={errors.sku?.message}
      >
        {(p) => (
          <Input className="uppercase" autoComplete="off" disabled={editing} {...p} {...form.register('sku')} />
        )}
      </Field>
      <Field label="Name" required error={errors.name?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('name')} />}
      </Field>
      <Field label="Generic name" hint="Optional" error={errors.generic_name?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('generic_name')} />}
      </Field>
      <Field label="Strength" hint="Optional, e.g. 500 mg" error={errors.strength?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('strength')} />}
      </Field>
      <Field label="Form" hint="Optional, e.g. tablet" error={errors.form?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('form')} />}
      </Field>
      <Field label="ATC code" hint="Optional, e.g. N02BE01" error={errors.atc_code?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('atc_code')} />}
      </Field>
      <Field
        label="Selling price per unit"
        required
        hint={editing ? 'A new price applies to future dispenses only' : 'What one unit is charged at when dispensed'}
        error={errors.unit_price?.message}
      >
        {(p) => <Input inputMode="decimal" autoComplete="off" {...p} {...form.register('unit_price')} />}
      </Field>

      <div className="space-y-3 sm:col-span-2">
        <Controller
          control={form.control}
          name="requires_prescription"
          render={({ field }) => (
            <label className="font-body text-body-sm text-on-surface flex items-start gap-2">
              <Checkbox
                className="mt-0.5"
                checked={field.value}
                onCheckedChange={(v) => field.onChange(v === true)}
              />
              <span>
                Needs a prescription
                {/* docs/18-API_CONTRACTS.md §12: there is no counter sale, so nothing consults the flag. */}
                <span className="text-outline block text-xs">
                  A note on the catalog entry. Every medicine is dispensed against a prescription
                  here, so nothing in the system checks it.
                </span>
              </span>
            </label>
          )}
        />
        {editing && (
          <Controller
            control={form.control}
            name="is_active"
            render={({ field }) => (
              <label className="font-body text-body-sm text-on-surface flex items-start gap-2">
                <Checkbox
                  className="mt-0.5"
                  checked={field.value}
                  onCheckedChange={(v) => field.onChange(v === true)}
                />
                <span>
                  Active — can be prescribed, ordered and dispensed
                  <span className="text-outline block text-xs">
                    An inactive medicine stays in the catalog. No stock can be received for it; the
                    batches already held can still be corrected or recalled.
                  </span>
                </span>
              </label>
            )}
          />
        )}
      </div>
    </div>
  )
}

/** Add a medicine to the catalog (`POST /medicines`). It starts active, with no stock. */
export function CreateMedicineDialog({ trigger }: { trigger: ReactNode }) {
  const create = useCreateMedicine()
  return (
    <FormDialog<MedicineValues>
      trigger={trigger}
      title="Add a medicine"
      description="Adds a medicine to the catalog so it can be prescribed, ordered and dispensed. The SKU can't be changed afterwards. Stock is received separately, batch by batch."
      resolver={zodResolver(createMedicineSchema)}
      defaults={() => EMPTY_MEDICINE}
      submitLabel="Add medicine"
      pendingLabel="Adding…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't add the medicine. Please try again."
      onSubmit={async (values) => {
        const medicine = await create.mutateAsync(toCreateBody(values))
        // The SKU as the server saved it, which is not always what was typed.
        return `Added ${medicine.name} (${medicine.sku})`
      }}
    >
      {(form) => <MedicineFields form={form} editing={false} />}
    </FormDialog>
  )
}

/**
 * Edit a catalog medicine (`PATCH /medicines/{id}`). Only what changed is
 * sent, and never the SKU. A dispense already made keeps the price it was
 * charged at.
 */
export function EditMedicineDialog({ medicine, trigger }: { medicine: Medicine; trigger: ReactNode }) {
  const update = useUpdateMedicine(medicine.id)
  return (
    <FormDialog<MedicineValues>
      trigger={trigger}
      // The name can be 200 unbroken characters and a dialog title cannot
      // break one, so the title is generic and the form's own Name field names it.
      title="Edit medicine"
      description="Changes apply from now on. Medicines already dispensed keep the price they were charged at."
      resolver={zodResolver(editMedicineSchema)}
      defaults={() => toMedicineValues(medicine)}
      submitLabel="Save changes"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't save the medicine. Please try again."
      onSubmit={async (values, opened) => {
        const body = toUpdateBody(opened, values)
        if (Object.keys(body).length === 0) return 'Nothing to save — the medicine is unchanged'
        const saved = await update.mutateAsync(body)
        return `Saved ${saved.name}`
      }}
    >
      {(form) => <MedicineFields form={form} editing />}
    </FormDialog>
  )
}
