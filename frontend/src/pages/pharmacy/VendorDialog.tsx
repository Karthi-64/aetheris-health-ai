import type { ReactNode } from 'react'
import { Controller, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Checkbox } from '@/components/ui/checkbox'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { FormDialog } from '@/components/forms/FormDialog'
import { useCreateVendor, useUpdateVendor, type Vendor } from '@/api/pharmacy'
import {
  EMPTY_VENDOR,
  toCreateBody,
  toUpdateBody,
  toVendorValues,
  vendorSchema,
  type VendorValues,
} from './vendorForm'

function VendorFields({ form, editing }: { form: UseFormReturn<VendorValues>; editing: boolean }) {
  const { errors } = form.formState

  return (
    <>
      {/* Uniqueness is exact and case-sensitive, and a vendor that has been
          switched off still holds its name (docs/18-API_CONTRACTS.md §9.7). */}
      <Field
        label="Name"
        required
        hint="Must be unique. An inactive vendor's name cannot be reused."
        error={errors.name?.message}
      >
        {(p) => <Input autoComplete="off" {...p} {...form.register('name')} />}
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Contact" hint="Optional — a person, phone or email" error={errors.contact?.message}>
          {(p) => <Input autoComplete="off" {...p} {...form.register('contact')} />}
        </Field>
        <Field label="Tax ID" hint="Optional" error={errors.tax_id?.message}>
          {(p) => <Input autoComplete="off" {...p} {...form.register('tax_id')} />}
        </Field>
      </div>
      <Field label="Address" hint="Optional" error={errors.address?.message}>
        {(p) => <Textarea rows={3} {...p} {...form.register('address')} />}
      </Field>
      {editing && (
        <div className="space-y-1">
          <Controller
            control={form.control}
            name="is_active"
            render={({ field }) => (
              <label className="font-body text-body-sm text-on-surface flex items-center gap-2">
                <Checkbox
                  ref={field.ref}
                  checked={field.value}
                  onCheckedChange={(v) => field.onChange(v === true)}
                />
                Active — can be ordered from
              </label>
            )}
          />
          <p className="font-body text-outline text-xs">
            Switching a vendor off only stops new orders. Orders already drafted or sent to it can
            still be sent, received or cancelled.
          </p>
        </div>
      )}
    </>
  )
}

/** Add a vendor (`POST /vendors`). A name already in use is a 409, shown in the API's words. */
export function CreateVendorDialog({ trigger }: { trigger: ReactNode }) {
  const create = useCreateVendor()
  return (
    <FormDialog<VendorValues>
      trigger={trigger}
      title="Add a vendor"
      description="A vendor medicines are ordered from. It can be switched off later, but not deleted."
      resolver={zodResolver(vendorSchema)}
      defaults={() => EMPTY_VENDOR}
      submitLabel="Add vendor"
      pendingLabel="Adding…"
      contentClassName="max-h-[90vh] overflow-y-auto"
      fallbackError="Couldn't add the vendor. Please try again."
      onSubmit={async (values) => {
        const vendor = await create.mutateAsync(toCreateBody(values))
        return `Added ${vendor.name}`
      }}
    >
      {(form) => <VendorFields form={form} editing={false} />}
    </FormDialog>
  )
}

/**
 * Edit a vendor, or switch it off (`PATCH /vendors/{id}`). Only what changed
 * is sent. There is no delete: `is_active` is how a vendor is retired.
 */
export function EditVendorDialog({ vendor, trigger }: { vendor: Vendor; trigger: ReactNode }) {
  const update = useUpdateVendor(vendor.id)
  return (
    <FormDialog<VendorValues>
      trigger={trigger}
      // The name goes in the description, where it can wrap: it may be 200
      // characters with no space in it, and a dialog title does not break.
      title="Edit vendor"
      description={
        <>
          Editing <span className="[overflow-wrap:anywhere]">{vendor.name}</span>. A new name shows on
          every order placed with this vendor, past ones included.
        </>
      }
      resolver={zodResolver(vendorSchema)}
      defaults={() => toVendorValues(vendor)}
      submitLabel="Save changes"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] overflow-y-auto"
      fallbackError="Couldn't save the vendor. Please try again."
      onSubmit={async (values, opened) => {
        const body = toUpdateBody(opened, values)
        if (Object.keys(body).length === 0) return 'Nothing to save — the vendor is unchanged'
        const saved = await update.mutateAsync(body)
        return `Saved ${saved.name}`
      }}
    >
      {(form) => <VendorFields form={form} editing />}
    </FormDialog>
  )
}
