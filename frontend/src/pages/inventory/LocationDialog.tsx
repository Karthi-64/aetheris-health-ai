import { type ReactNode, useId } from 'react'
import { Controller, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Checkbox } from '@/components/ui/checkbox'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { FormDialog } from '@/components/forms/FormDialog'
import { useCreateLocation, useUpdateLocation, type InventoryLocation } from '@/api/inventory'
import { LOCATION_KIND, LOCATION_KINDS } from '@/components/inventory/inventoryPresentation'
import {
  EMPTY_LOCATION,
  createLocationSchema,
  editLocationSchema,
  toCreateBody,
  toLocationValues,
  toUpdateBody,
  type LocationValues,
} from './locationForm'

function LocationFields({ form, editing }: { form: UseFormReturn<LocationValues>; editing: boolean }) {
  const { errors } = form.formState
  const activeId = useId()

  return (
    <>
      <Field label="Name" required error={errors.name?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('name')} />}
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label="Code"
          required
          hint={editing ? "Can't be changed" : "e.g. WARD-A — saved in capitals, and can't be changed later"}
          error={errors.code?.message}
        >
          {(p) => (
            <Input className="uppercase" autoComplete="off" disabled={editing} {...p} {...form.register('code')} />
          )}
        </Field>
        <Field label="Kind" required error={errors.kind?.message}>
          {(p) => (
            <Controller
              control={form.control}
              name="kind"
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger ref={field.ref} onBlur={field.onBlur} {...p}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {LOCATION_KINDS.map((kind) => (
                      <SelectItem key={kind} value={kind}>
                        {LOCATION_KIND[kind].label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            />
          )}
        </Field>
      </div>
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
                  aria-invalid={!!errors.is_active}
                  aria-describedby={
                    errors.is_active ? `${activeId}-help ${activeId}-error` : `${activeId}-help`
                  }
                />
                Active — can receive stock
              </label>
            )}
          />
          {/* docs/18-API_CONTRACTS.md §10.3. The API switches a location off
              whatever it holds: there is no check on its stock, and so no
              refusal to show for one. */}
          <p id={`${activeId}-help`} className="font-body text-outline text-xs">
            Switching a location off does not move or remove what it holds. That stock can still be
            used, corrected or transferred out, but nothing can be transferred or received into the
            location.
          </p>
          {/* A 422 naming the field is put on it by the dialog, and has to show somewhere. */}
          {errors.is_active?.message && (
            <p id={`${activeId}-error`} className="font-body text-error text-xs" role="alert">
              {errors.is_active.message}
            </p>
          )}
        </div>
      )}
    </>
  )
}

/** Add a location (`POST /inventory/locations`). A code already in use is a 409, shown in the API's words. */
export function CreateLocationDialog({ trigger }: { trigger: ReactNode }) {
  const create = useCreateLocation()
  return (
    <FormDialog<LocationValues>
      trigger={trigger}
      title="Add a location"
      description="A ward, theatre, ICU or store that holds stock. Its code can't be changed afterwards. It can be switched off later, but not deleted."
      resolver={zodResolver(createLocationSchema)}
      defaults={() => EMPTY_LOCATION}
      submitLabel="Add location"
      pendingLabel="Adding…"
      contentClassName="max-h-[90vh] overflow-y-auto"
      fallbackError="Couldn't add the location. Please try again."
      onSubmit={async (values) => {
        const location = await create.mutateAsync(toCreateBody(values))
        // The code as the server saved it, which is not always what was typed.
        return `Added ${location.name} (${location.code})`
      }}
    >
      {(form) => <LocationFields form={form} editing={false} />}
    </FormDialog>
  )
}

/**
 * Edit a location, or switch it off (`PATCH /inventory/locations/{id}`). Only
 * what changed is sent, and never the code. There is no delete: `is_active` is
 * how a location is retired.
 */
export function EditLocationDialog({ location, trigger }: { location: InventoryLocation; trigger: ReactNode }) {
  const update = useUpdateLocation(location.id)
  return (
    <FormDialog<LocationValues>
      trigger={trigger}
      // The name goes in the description, where it can wrap: it may be 200
      // characters with no space in it, and a dialog title does not break.
      title="Edit location"
      description={
        <>
          Editing <span className="[overflow-wrap:anywhere]">{location.name}</span>. A new name shows on
          every stock row held here.
        </>
      }
      resolver={zodResolver(editLocationSchema)}
      defaults={() => toLocationValues(location)}
      submitLabel="Save changes"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] overflow-y-auto"
      fallbackError="Couldn't save the location. Please try again."
      onSubmit={async (values, opened) => {
        const body = toUpdateBody(opened, values)
        if (Object.keys(body).length === 0) return 'Nothing to save — the location is unchanged'
        const saved = await update.mutateAsync(body)
        return `Saved ${saved.name}`
      }}
    >
      {(form) => <LocationFields form={form} editing />}
    </FormDialog>
  )
}
