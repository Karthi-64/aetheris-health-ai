import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Field } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { FormDialog } from '@/components/forms/FormDialog'
import { useCancelPrescription, type Prescription } from '@/api/pharmacy'

/** The API takes a reason of 1–500 characters and nothing else (docs/18-API_CONTRACTS.md §9.4). */
const schema = z.object({
  reason: z
    .string()
    .trim()
    .min(1, 'Give a reason for cancelling')
    .max(500, 'Keep the reason under 500 characters'),
})

type FormValues = z.infer<typeof schema>

/**
 * Cancel a prescription nothing has been dispensed from
 * (`POST /prescriptions/{id}/cancel`).
 *
 * The API only cancels from `active`, so no stock has moved and nothing has
 * been charged — there is nothing to undo, and the dialog says so. There is no
 * endpoint that reopens a cancelled prescription.
 */
export function CancelPrescriptionDialog({ prescription }: { prescription: Prescription }) {
  const cancel = useCancelPrescription(prescription.id)
  return (
    <FormDialog<FormValues>
      trigger={
        <Button variant="outline" size="sm" className="text-error hover:text-error">
          <XCircle className="size-4" /> Cancel prescription
        </Button>
      }
      title="Cancel this prescription?"
      description={
        <>
          Nothing has been dispensed from this prescription for {prescription.patient_name}, and
          nothing has been charged for it. A cancelled prescription cannot be reopened — a new one
          has to be written from the visit.
        </>
      }
      resolver={zodResolver(schema)}
      defaults={() => ({ reason: '' })}
      submitLabel="Cancel prescription"
      pendingLabel="Cancelling…"
      destructive
      dismissLabel="Keep prescription"
      fallbackError="Couldn't cancel the prescription. Please try again."
      onSubmit={async (values) => {
        await cancel.mutateAsync(values.reason)
        return `Prescription cancelled for ${prescription.patient_name}`
      }}
    >
      {(form) => (
        <Field label="Reason" required error={form.formState.errors.reason?.message}>
          {(p) => (
            <Textarea
              rows={3}
              placeholder="e.g. Written for the wrong visit"
              {...p}
              {...form.register('reason')}
            />
          )}
        </Field>
      )}
    </FormDialog>
  )
}
