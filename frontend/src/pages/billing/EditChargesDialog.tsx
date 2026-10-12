import { useRef, useState } from 'react'
import { FormProvider, useForm, type Path } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Loader2, Pencil } from 'lucide-react'
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
import { useServices, useUpdateInvoice, type Invoice } from '@/api/billing'
import { InvoiceLinesFields } from './InvoiceLinesFields'
import { fromInvoiceItem, lineSchema, toLineInput } from './invoiceLines'
import { billingErrorMessage, splitFieldErrors } from './billing'

const schema = z.object({
  items: z.array(lineSchema).max(200, 'An invoice can have at most 200 items'),
})

type FormValues = z.infer<typeof schema>

const isFormField = (field: string) =>
  /^items\.\d+\.(service_id|description|quantity|unit_price)$/.test(field)

/**
 * Edit a draft's charges. `PATCH /invoices/{id}` replaces the whole line set
 * (docs/18-API_CONTRACTS.md §6.4), so every line is sent back, changed or not.
 */
export function EditChargesDialog({ invoice }: { invoice: Invoice }) {
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const submitting = useRef(false)
  const update = useUpdateInvoice(invoice.id)
  // Same query the line editor uses, so this costs no second request.
  const { data: catalog } = useServices({ is_active: true, page_size: 100 })

  const form = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: { items: [] } })
  const { handleSubmit, reset, setError } = form

  function onOpenChange(next: boolean) {
    setOpen(next)
    setNotice(null)
    // Load the lines as the server holds them each time the dialog opens.
    if (next) reset({ items: invoice.items.map((i) => fromInvoiceItem(i, catalog?.items ?? [])) })
  }

  async function onSubmit(values: FormValues) {
    if (submitting.current) return
    setNotice(null)
    submitting.current = true
    try {
      await update.mutateAsync({ items: values.items.map(toLineInput) })
      toast.success('Charges updated')
      setOpen(false)
    } catch (err) {
      const { onFields, other } = splitFieldErrors(err, isFormField)
      for (const fe of onFields) setError(fe.field as Path<FormValues>, { message: fe.message })
      if (onFields.length === 0 || other.length > 0) {
        setNotice(
          other.length > 0
            ? other.join(' ')
            : billingErrorMessage(err, "Couldn't update the charges. Please try again."),
        )
      }
    } finally {
      submitting.current = false
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Pencil className="size-4" /> Edit charges
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Edit charges</DialogTitle>
          <DialogDescription>
            Totals are recalculated by the server when you save. Changing the charges withdraws
            any discount approval.
          </DialogDescription>
        </DialogHeader>

        <FormProvider {...form}>
          <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-4" noValidate>
            {notice && (
              <Alert variant="error" title="Couldn't update the charges">
                {notice}
              </Alert>
            )}
            <InvoiceLinesFields currency={invoice.currency} />
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={update.isPending} aria-busy={update.isPending}>
                {update.isPending && <Loader2 className="size-4 animate-spin" />}
                {update.isPending ? 'Saving…' : 'Save charges'}
              </Button>
            </DialogFooter>
          </form>
        </FormProvider>
      </DialogContent>
    </Dialog>
  )
}
