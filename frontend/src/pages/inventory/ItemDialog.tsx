import { type ReactNode, useId } from 'react'
import { Controller, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { Checkbox } from '@/components/ui/checkbox'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { FormDialog } from '@/components/forms/FormDialog'
import { FormRefusal } from '@/components/forms/formRefusal'
import { useCreateItem, useUpdateItem, type InventoryItem } from '@/api/inventory'
import {
  EMPTY_ITEM,
  createItemSchema,
  editItemSchema,
  levelsRefusal,
  toCreateBody,
  toItemValues,
  toUpdateBody,
  type ItemValues,
} from './itemForm'

/**
 * A checkbox with what `Field` gives an input: a short label that is its
 * whole name, the explanation tied to it as its description, and a slot for
 * an error — a 422 naming the field is put on it by the dialog, and has to
 * show somewhere.
 */
function CheckField({
  label,
  help,
  error,
  children,
}: {
  label: string
  help: string
  error?: string
  children: (props: { 'aria-invalid': boolean; 'aria-describedby': string }) => ReactNode
}) {
  const id = useId()
  return (
    <div className="space-y-1">
      <label className="font-body text-body-sm text-on-surface flex items-center gap-2">
        {children({
          'aria-invalid': !!error,
          'aria-describedby': error ? `${id}-help ${id}-error` : `${id}-help`,
        })}
        {label}
      </label>
      <p id={`${id}-help`} className="font-body text-outline text-xs">
        {help}
      </p>
      {error && (
        <p id={`${id}-error`} className="font-body text-error text-xs" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

function ItemFields({ form, editing }: { form: UseFormReturn<ItemValues>; editing: boolean }) {
  const { errors } = form.formState

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field
        label="SKU"
        required
        hint={editing ? "Can't be changed" : "e.g. GLOVE-M — saved in capitals, and can't be changed later"}
        error={errors.sku?.message}
      >
        {(p) => (
          <Input className="uppercase" autoComplete="off" disabled={editing} {...p} {...form.register('sku')} />
        )}
      </Field>
      <Field label="Name" required error={errors.name?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('name')} />}
      </Field>
      <Field label="Category" hint="Optional, e.g. Disposables" error={errors.category?.message}>
        {(p) => <Input autoComplete="off" {...p} {...form.register('category')} />}
      </Field>
      <Field
        label="Unit of measure"
        required
        // The API changes the unit whatever stock the item holds (§10.3), and
        // every quantity already recorded is then read in the new one.
        hint={
          editing
            ? 'What one unit is, e.g. piece or box of 100. Changing it relabels the quantities already recorded; it does not convert them.'
            : 'What one unit is, e.g. piece or box of 100. Every quantity of this item is counted in it.'
        }
        error={errors.unit_of_measure?.message}
      >
        {(p) => <Input autoComplete="off" {...p} {...form.register('unit_of_measure')} />}
      </Field>
      {/* docs/18-API_CONTRACTS.md §10.4: low stock is the server's judgement,
          made hospital-wide from this figure — there is none per location. */}
      <Field
        label="Reorder point"
        hint="Optional. When the usable stock across the hospital is at or below this, the item is marked low. Left blank, it never is."
        error={errors.reorder_point?.message}
      >
        {(p) => <Input inputMode="numeric" autoComplete="off" {...p} {...form.register('reorder_point')} />}
      </Field>
      <Field
        label="Target stock"
        hint="Optional. The level a reorder should bring the item back to. Not less than the reorder point."
        error={errors.target_stock?.message}
      >
        {(p) => <Input inputMode="numeric" autoComplete="off" {...p} {...form.register('target_stock')} />}
      </Field>

      <div className="space-y-3 sm:col-span-2">
        <Controller
          control={form.control}
          name="is_batch_tracked"
          render={({ field }) => (
            <CheckField
              label="Tracked by batch"
              help={
                editing
                  ? "Can't be changed. Stock of a batch-tracked item is kept per batch number, each with its own expiry date."
                  : "Stock is kept per batch number, each with its own expiry date. Can't be changed later."
              }
              error={errors.is_batch_tracked?.message}
            >
              {(p) => (
                <Checkbox
                  ref={field.ref}
                  checked={field.value}
                  // Stock already held was recorded with or without batches on
                  // the strength of it, so the API never lets it change (§10.3).
                  disabled={editing}
                  onCheckedChange={(v) => field.onChange(v === true)}
                  {...p}
                />
              )}
            </CheckField>
          )}
        />
        {editing && (
          <Controller
            control={form.control}
            name="is_active"
            render={({ field }) => (
              // docs/18-API_CONTRACTS.md §10.4: the stock summary, and so the
              // low-stock list, is of active items only — whatever they hold.
              <CheckField
                label="Active — can be used, moved and ordered"
                help="An inactive item stays in the catalog and keeps its stock, which can still be corrected. It cannot be used, transferred or put on a new purchase order, and it is left out of the stock overview and the low-stock list even while it holds stock."
                error={errors.is_active?.message}
              >
                {(p) => (
                  <Checkbox
                    ref={field.ref}
                    checked={field.value}
                    onCheckedChange={(v) => field.onChange(v === true)}
                    {...p}
                  />
                )}
              </CheckField>
            )}
          />
        )}
      </div>
    </div>
  )
}

/**
 * Add an item to the catalog (`POST /inventory/items`). It starts active, with
 * no stock. A SKU already in use is a 409, shown in the API's words.
 */
export function CreateItemDialog({ trigger }: { trigger: ReactNode }) {
  const create = useCreateItem()
  return (
    <FormDialog<ItemValues>
      trigger={trigger}
      title="Add an item"
      description="Adds an item to the catalog so its stock can be recorded, used and ordered. The SKU and whether it is tracked by batch can't be changed afterwards. The item starts with no stock."
      resolver={zodResolver(createItemSchema)}
      defaults={() => EMPTY_ITEM}
      submitLabel="Add item"
      pendingLabel="Adding…"
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't add the item. Please try again."
      onSubmit={async (values) => {
        const item = await create.mutateAsync(toCreateBody(values))
        // The SKU as the server saved it, which is not always what was typed.
        return `Added ${item.name} (${item.sku})`
      }}
    >
      {(form) => <ItemFields form={form} editing={false} />}
    </FormDialog>
  )
}

/**
 * Edit a catalog item, or switch it off (`PATCH /inventory/items/{id}`). Only
 * what changed is sent, and never the SKU or the batch-tracking flag. There is
 * no delete: `is_active` is how an item is retired.
 */
export function EditItemDialog({ item, trigger }: { item: InventoryItem; trigger: ReactNode }) {
  const update = useUpdateItem(item.id)
  return (
    <FormDialog<ItemValues>
      trigger={trigger}
      // The name can be 200 unbroken characters and a dialog title cannot
      // break one, so the title is generic and the form's own Name field names it.
      title="Edit item"
      description="Changes apply from now on. A new name or unit shows on every stock row and order line for this item, past ones included."
      resolver={zodResolver(editItemSchema)}
      defaults={() => toItemValues(item)}
      submitLabel="Save changes"
      pendingLabel="Saving…"
      requireChanges
      contentClassName="max-h-[90vh] max-w-2xl overflow-y-auto"
      fallbackError="Couldn't save the item. Please try again."
      onSubmit={async (values, opened) => {
        const body = toUpdateBody(opened, values)
        if (Object.keys(body).length === 0) return 'Nothing to save — the item is unchanged'
        const refusal = levelsRefusal(body)
        if (refusal) throw new FormRefusal(refusal)
        const saved = await update.mutateAsync(body)
        return `Saved ${saved.name}`
      }}
    >
      {(form) => <ItemFields form={form} editing />}
    </FormDialog>
  )
}
