import { Controller, useFieldArray, useFormContext, useWatch } from 'react-hook-form'
import { Plus, X } from 'lucide-react'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useServices, type Service } from '@/api/billing'
import { formatMoney } from '@/lib/format'
import { CUSTOM_ITEM, EMPTY_LINE, type LinesFormValues } from './invoiceLines'

function LineRow({
  index,
  services,
  currency,
  onRemove,
}: {
  index: number
  services: Service[]
  currency?: string
  onRemove: () => void
}) {
  const {
    control,
    register,
    formState: { errors },
  } = useFormContext<LinesFormValues>()
  const line = useWatch({ control, name: `items.${index}` })
  const lineErrors = errors.items?.[index]
  const isCustom = line?.service_id === CUSTOM_ITEM
  const service = services.find((s) => s.id === line?.service_id)
  // A line already on the invoice may point at a service that has since been
  // retired; it still needs an option or the select would show nothing.
  const unlisted = line?.service_id && !isCustom && !service
  const n = index + 1

  return (
    <li aria-label={`Item ${n}`} className="neo-pressed bg-surface space-y-3 rounded-xl p-4">
      <div className="flex items-start gap-3">
        <div className="grid flex-1 grid-cols-[1fr_6rem] gap-3">
          <Controller
            control={control}
            name={`items.${index}.service_id`}
            render={({ field }) => (
              <Field label={`Item ${n}`} required error={lineErrors?.service_id?.message}>
                {(p) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id={p.id} aria-invalid={p['aria-invalid']}>
                      <SelectValue placeholder="Select a service" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={CUSTOM_ITEM}>Custom item</SelectItem>
                      {unlisted && (
                        <SelectItem value={line.service_id}>
                          {line.description || 'Current service'}
                        </SelectItem>
                      )}
                      {services.map((s) => (
                        <SelectItem key={s.id} value={s.id}>
                          {s.name} · {formatMoney(s.price, currency)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </Field>
            )}
          />
          <Field label="Quantity" required error={lineErrors?.quantity?.message}>
            {(p) => <Input inputMode="decimal" {...p} {...register(`items.${index}.quantity`)} />}
          </Field>
        </div>
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove item ${n}`}
          className="text-outline hover:text-error mt-8 transition-colors"
        >
          <X className="size-4" />
        </button>
      </div>

      {isCustom && (
        <div className="grid grid-cols-[1fr_8rem] gap-3">
          <Field label="Description" required error={lineErrors?.description?.message}>
            {(p) => (
              <Input placeholder="e.g. Crepe bandage" {...p} {...register(`items.${index}.description`)} />
            )}
          </Field>
          <Field label="Unit price" required error={lineErrors?.unit_price?.message}>
            {(p) => (
              <Input
                inputMode="decimal"
                placeholder="0.00"
                {...p}
                {...register(`items.${index}.unit_price`)}
              />
            )}
          </Field>
          <Controller
            control={control}
            name={`items.${index}.taxable`}
            render={({ field }) => (
              <label className="font-body text-body-sm text-on-surface col-span-2 flex items-center gap-2">
                <Checkbox checked={field.value} onCheckedChange={(v) => field.onChange(v === true)} />
                Taxable at the hospital's rate
              </label>
            )}
          />
        </div>
      )}
    </li>
  )
}

/**
 * The line-item editor shared by "New invoice" and "Edit charges". Must be
 * rendered inside a `FormProvider` whose values include `items`.
 *
 * No totals are shown here: the server computes them (docs/18-API_CONTRACTS.md
 * §6.2) and the invoice shows its figures once the lines are saved.
 */
export function InvoiceLinesFields({ currency }: { currency?: string }) {
  const { control } = useFormContext<LinesFormValues>()
  const { fields, append, remove } = useFieldArray({ control, name: 'items' })
  const { data, isError } = useServices({ is_active: true, page_size: 100 })
  const services = data?.items ?? []

  return (
    <div className="space-y-3">
      {isError && (
        <Alert variant="warning" title="Service catalog unavailable">
          Catalog services couldn't be loaded. Custom items can still be added.
        </Alert>
      )}
      {fields.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">
          No charges yet. An invoice needs at least one before it can be issued.
        </p>
      ) : (
        <ul className="space-y-3">
          {fields.map((f, index) => (
            <LineRow
              key={f.id}
              index={index}
              services={services}
              currency={currency}
              onRemove={() => remove(index)}
            />
          ))}
        </ul>
      )}
      <Button type="button" variant="outline" size="sm" onClick={() => append({ ...EMPTY_LINE })}>
        <Plus className="size-4" /> Add item
      </Button>
    </div>
  )
}
