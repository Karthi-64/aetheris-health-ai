import { type ReactNode, useMemo, useState } from 'react'
import { Controller, type UseFormReturn } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { RotateCw, Search } from 'lucide-react'
import { Field } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { FormDialog } from '@/components/forms/FormDialog'
import { FormRefusal } from '@/components/forms/formRefusal'
import { useCreateLabOrder, useLabTests, type LabTest } from '@/api/lab'
import { ApiError } from '@/api/types'
import { formatMoney, formatTime } from '@/lib/format'
import { LAB_PRIORITIES, LAB_PRIORITY, type OrderableVisit } from './labPresentation'

/** The API takes 1–50 tests per order and a note of up to 2,000 characters (§8.4). */
const MAX_TESTS = 50

const schema = z.object({
  test_ids: z
    .array(z.string())
    .min(1, 'Choose at least one test')
    .max(MAX_TESTS, `An order can hold at most ${MAX_TESTS} tests`),
  priority: z.enum(['routine', 'urgent', 'stat']),
  notes: z.string().max(2000, 'Keep the note under 2,000 characters'),
})

type FormValues = z.infer<typeof schema>

function TestChecklist({ form }: { form: UseFormReturn<FormValues> }) {
  const [filter, setFilter] = useState('')
  // One request for the whole active catalog: the list is filtered as the user
  // types, and a ticked test must stay ticked when it scrolls out of a search.
  const { data, isPending, isError, refetch } = useLabTests({ is_active: true, page_size: 100 })
  const tests = useMemo(() => data?.items ?? [], [data])
  const error = form.formState.errors.test_ids?.message

  const needle = filter.trim().toLowerCase()
  const shown = needle
    ? tests.filter((t) =>
        [t.name, t.code, t.category ?? ''].some((v) => v.toLowerCase().includes(needle)),
      )
    : tests

  return (
    <Controller
      control={form.control}
      name="test_ids"
      render={({ field }) => {
        const chosen = new Set(field.value)
        const toggle = (test: LabTest, on: boolean) =>
          field.onChange(on ? [...field.value, test.id] : field.value.filter((id) => id !== test.id))
        const total = tests
          .filter((t) => chosen.has(t.id))
          .reduce((sum, t) => sum + Number(t.price), 0)

        return (
          <fieldset className="space-y-2">
            <legend className="font-label text-on-surface-variant">
              Tests<span className="text-error">*</span>
            </legend>

            {isPending ? (
              <div role="status" aria-label="Loading tests" className="space-y-2">
                <Skeleton className="h-10 w-full rounded-xl" />
                <Skeleton className="h-10 w-full rounded-xl" />
                <Skeleton className="h-10 w-full rounded-xl" />
              </div>
            ) : isError ? (
              <div className="flex flex-wrap items-center gap-3">
                <p className="font-body text-body-sm text-on-surface-variant">
                  The test catalog couldn't be loaded.
                </p>
                <Button type="button" variant="outline" size="sm" onClick={() => refetch()}>
                  <RotateCw className="size-4" /> Retry
                </Button>
              </div>
            ) : tests.length === 0 ? (
              <p className="font-body text-body-sm text-on-surface-variant">
                There are no active tests in the catalog.
              </p>
            ) : (
              <>
                <div className="relative">
                  <Search className="text-outline pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
                  <input
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                    placeholder="Filter by name, code or category…"
                    aria-label="Filter tests"
                    className="neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant focus-visible:ring-secondary w-full rounded-xl py-2.5 pr-4 pl-9 outline-none focus-visible:ring-2"
                  />
                </div>
                <ul className="neo-pressed bg-surface max-h-56 space-y-1 overflow-y-auto rounded-xl p-2">
                  {shown.length === 0 && (
                    <li className="font-body text-outline px-2 py-1.5 text-xs">No tests match “{filter}”.</li>
                  )}
                  {shown.map((test) => (
                    <li key={test.id}>
                      <label className="hover:bg-secondary/10 flex cursor-pointer items-center gap-3 rounded-lg px-2 py-1.5">
                        <Checkbox
                          checked={chosen.has(test.id)}
                          onCheckedChange={(v) => toggle(test, v === true)}
                        />
                        <span className="min-w-0 flex-1">
                          <span className="font-body text-body-sm text-on-surface block">{test.name}</span>
                          <span className="font-body text-outline block text-xs">
                            <span className="font-mono">{test.code}</span>
                            {test.category ? ` · ${test.category}` : ''}
                          </span>
                        </span>
                        <span className="font-body text-body-sm text-on-surface-variant tabular-nums">
                          {formatMoney(test.price)}
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
                <p className="font-body text-outline text-xs" aria-live="polite">
                  {chosen.size === 0
                    ? 'No tests chosen.'
                    : `${chosen.size} ${chosen.size === 1 ? 'test' : 'tests'} chosen · ${formatMoney(total)} at catalog prices`}
                </p>
              </>
            )}

            {error && (
              <p className="font-body text-error text-xs" role="alert">
                {error}
              </p>
            )}
          </fieldset>
        )
      }}
    />
  )
}

/**
 * Order lab tests for a visit (`POST /lab-orders`).
 *
 * An order hangs off an appointment: the patient and the ordering doctor are
 * read from it by the server, so they are shown here and never sent. Each test
 * is charged to the visit's draft invoice by the server in the same step —
 * the dialog says so, and bills nothing itself.
 */
export function OrderLabTestsDialog({ visit, trigger }: { visit: OrderableVisit; trigger: ReactNode }) {
  const create = useCreateLabOrder()

  return (
    <FormDialog<FormValues>
      trigger={trigger}
      title="Order lab tests"
      description={
        <>
          {visit.patient_name}, seen by {visit.doctor_name} at {formatTime(visit.scheduled_start)}.
          Each test is added to this visit's draft invoice at its catalog price.
        </>
      }
      resolver={zodResolver(schema)}
      defaults={() => ({ test_ids: [], priority: 'routine', notes: '' })}
      submitLabel="Place order"
      pendingLabel="Placing order…"
      contentClassName="max-h-[90vh] overflow-y-auto"
      fallbackError="Couldn't place the lab order. Please try again."
      onSubmit={async (values) => {
        try {
          const order = await create.mutateAsync({
            appointment_id: visit.id,
            test_ids: values.test_ids,
            priority: values.priority,
            ...(values.notes.trim() ? { notes: values.notes.trim() } : {}),
          })
          const count = order.items.length
          return `Lab order placed — ${count} ${count === 1 ? 'test' : 'tests'} for ${order.patient_name}`
        } catch (err) {
          // A refused test (inactive, removed) is named by position in the
          // request, which means nothing on a checklist; the API's own message
          // names the test, so that is what is shown.
          if (err instanceof ApiError && err.status === 422) throw new FormRefusal(err.message)
          throw err
        }
      }}
    >
      {(form) => (
        <>
          <TestChecklist form={form} />

          <Controller
            control={form.control}
            name="priority"
            render={({ field }) => (
              <Field label="Priority" required error={form.formState.errors.priority?.message}>
                {(p) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id={p.id} ref={field.ref} aria-invalid={p['aria-invalid']}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {LAB_PRIORITIES.map((priority) => (
                        <SelectItem key={priority} value={priority}>
                          {LAB_PRIORITY[priority].label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              </Field>
            )}
          />

          <Field label="Notes for the lab" hint="Optional" error={form.formState.errors.notes?.message}>
            {(p) => (
              <Textarea rows={2} placeholder="e.g. Fasting sample" {...p} {...form.register('notes')} />
            )}
          </Field>
        </>
      )}
    </FormDialog>
  )
}
