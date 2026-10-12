import { useRef } from 'react'
import { Controller, type Control, type FieldPath, type FieldValues } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Ban, BadgeCheck, CreditCard, Loader2, Percent, Send, Undo2 } from 'lucide-react'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { FormDialog } from '@/components/forms/FormDialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  useApproveDiscount,
  useIssueInvoice,
  useRecordPayment,
  useRecordRefund,
  useUpdateInvoice,
  useVoidInvoice,
  type Invoice,
  type PaymentMethod,
} from '@/api/billing'
import { usePermissions } from '@/hooks/usePermissions'
import { formatMoney } from '@/lib/format'
import { EditChargesDialog } from './EditChargesDialog'
import {
  MONEY_PATTERN,
  PAYMENT_METHODS,
  PAYMENT_METHOD_LABEL,
  billingErrorMessage,
  isPositiveMoney,
} from './billing'

const AMOUNT_MESSAGE = 'Enter an amount above 0, with at most 2 decimals'
const methodSchema = z.enum(['cash', 'card', 'upi', 'bank_transfer', 'insurance'])

function MethodField<T extends FieldValues>({
  control,
  name,
  label,
  methods,
}: {
  control: Control<T>
  name: FieldPath<T>
  label: string
  methods: PaymentMethod[]
}) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field, fieldState }) => (
        <Field label={label} required error={fieldState.error?.message}>
          {(p) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger ref={field.ref} id={p.id} aria-invalid={p['aria-invalid']}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {methods.map((m) => (
                  <SelectItem key={m} value={m}>
                    {PAYMENT_METHOD_LABEL[m]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </Field>
      )}
    />
  )
}

// ── Payment ─────────────────────────────────────────────────────────────────

const paymentSchema = z.object({
  amount: z.string().trim().refine(isPositiveMoney, AMOUNT_MESSAGE),
  method: methodSchema,
  reference: z.string().trim().max(100, 'Keep the reference under 100 characters'),
  notes: z.string().trim().max(2000, 'Keep notes under 2000 characters'),
})
type PaymentValues = z.infer<typeof paymentSchema>

/**
 * Record a payment (docs/18-API_CONTRACTS.md §6.6). The amount starts at the
 * server's outstanding balance; whether an amount is too much is decided by
 * the server, whose refusal is shown in the dialog.
 */
function RecordPaymentDialog({ invoice, methods }: { invoice: Invoice; methods: PaymentMethod[] }) {
  const record = useRecordPayment(invoice.id)
  return (
    <FormDialog<PaymentValues>
      trigger={
        <Button size="sm">
          <CreditCard className="size-4" /> Record payment
        </Button>
      }
      title="Record payment"
      description={
        <>
          Outstanding balance: <strong>{formatMoney(invoice.balance_due, invoice.currency)}</strong>.
          {methods.length === 1 && ' Your role can record cash payments only.'}
        </>
      }
      resolver={zodResolver(paymentSchema)}
      defaults={() => ({ amount: invoice.balance_due, method: methods[0], reference: '', notes: '' })}
      submitLabel="Record payment"
      pendingLabel="Recording…"
      fallbackError="Couldn't record the payment. Check the payment history before trying again."
      onSubmit={async (v) => {
        const result = await record.mutateAsync({
          amount: v.amount,
          method: v.method,
          ...(v.reference ? { reference: v.reference } : {}),
          ...(v.notes ? { notes: v.notes } : {}),
        })
        return result.invoice.status === 'paid'
          ? 'Payment recorded — invoice paid in full'
          : 'Payment recorded'
      }}
    >
      {({ control, register, formState: { errors } }) => (
        <>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Amount" required error={errors.amount?.message}>
              {(p) => <Input inputMode="decimal" {...p} {...register('amount')} />}
            </Field>
            <MethodField control={control} name="method" label="Method" methods={methods} />
          </div>
          <Field label="Reference" hint="Optional — transaction or receipt number" error={errors.reference?.message}>
            {(p) => <Input {...p} {...register('reference')} />}
          </Field>
          <Field label="Notes" hint="Optional" error={errors.notes?.message}>
            {(p) => <Textarea rows={2} {...p} {...register('notes')} />}
          </Field>
        </>
      )}
    </FormDialog>
  )
}

// ── Refund ──────────────────────────────────────────────────────────────────

const refundSchema = z.object({
  amount: z.string().trim().refine(isPositiveMoney, AMOUNT_MESSAGE),
  method: methodSchema,
  reason: z
    .string()
    .trim()
    .min(1, 'Give a reason for the refund')
    .max(500, 'Keep the reason under 500 characters'),
  reference: z.string().trim().max(100, 'Keep the reference under 100 characters'),
})
type RefundValues = z.infer<typeof refundSchema>

/** Give money back (§6.8). Refunding everything paid closes the invoice as refunded. */
function RefundDialog({ invoice }: { invoice: Invoice }) {
  const refund = useRecordRefund(invoice.id)
  return (
    <FormDialog<RefundValues>
      trigger={
        <Button variant="outline" size="sm" className="text-error hover:text-error">
          <Undo2 className="size-4" /> Refund
        </Button>
      }
      title="Refund this invoice?"
      description={
        <>
          Paid: <strong>{formatMoney(invoice.amount_paid, invoice.currency)}</strong> · already
          refunded: <strong>{formatMoney(invoice.amount_refunded, invoice.currency)}</strong>.
          Refunding everything that was paid closes the invoice, and that can't be undone.
        </>
      }
      resolver={zodResolver(refundSchema)}
      defaults={() => ({ amount: '', method: 'cash', reason: '', reference: '' })}
      submitLabel="Issue refund"
      pendingLabel="Refunding…"
      destructive
      dismissLabel="Keep as is"
      fallbackError="Couldn't issue the refund. Check the refund history before trying again."
      onSubmit={async (v) => {
        const result = await refund.mutateAsync({
          amount: v.amount,
          method: v.method,
          reason: v.reason,
          ...(v.reference ? { reference: v.reference } : {}),
        })
        return result.invoice.status === 'refunded'
          ? 'Refund issued — invoice closed as refunded'
          : 'Refund issued'
      }}
    >
      {({ control, register, formState: { errors } }) => (
        <>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Amount" required error={errors.amount?.message}>
              {(p) => <Input inputMode="decimal" placeholder="0.00" {...p} {...register('amount')} />}
            </Field>
            <MethodField control={control} name="method" label="Refund by" methods={PAYMENT_METHODS} />
          </div>
          <Field label="Reason" required error={errors.reason?.message}>
            {(p) => <Textarea rows={2} {...p} {...register('reason')} />}
          </Field>
          <Field label="Reference" hint="Optional" error={errors.reference?.message}>
            {(p) => <Input {...p} {...register('reference')} />}
          </Field>
        </>
      )}
    </FormDialog>
  )
}

// ── Discount ────────────────────────────────────────────────────────────────

const discountSchema = z
  .object({
    discount_amount: z
      .string()
      .trim()
      .regex(MONEY_PATTERN, 'Enter an amount, with at most 2 decimals'),
    discount_reason: z.string().trim().max(500, 'Keep the reason under 500 characters'),
  })
  .superRefine((v, ctx) => {
    if (isPositiveMoney(v.discount_amount) && !v.discount_reason) {
      ctx.addIssue({ code: 'custom', path: ['discount_reason'], message: 'Give a reason for the discount' })
    }
  })
type DiscountValues = z.infer<typeof discountSchema>

/**
 * Set or remove a draft's discount (§6.7). The server compares it with the
 * hospital's approval threshold and says whether it now needs an admin — the
 * threshold is never evaluated here.
 */
function DiscountDialog({ invoice }: { invoice: Invoice }) {
  const update = useUpdateInvoice(invoice.id)
  return (
    <FormDialog<DiscountValues>
      trigger={
        <Button variant="outline" size="sm">
          <Percent className="size-4" /> Discount
        </Button>
      }
      title="Invoice discount"
      description={
        <>
          An amount off the whole invoice, not a percentage. Subtotal:{' '}
          <strong>{formatMoney(invoice.subtotal, invoice.currency)}</strong>. Enter 0 to remove
          the discount. A larger discount may need an administrator's approval.
        </>
      }
      resolver={zodResolver(discountSchema)}
      defaults={() => ({
        discount_amount: invoice.discount_amount,
        discount_reason: invoice.discount_reason ?? '',
      })}
      submitLabel="Save discount"
      pendingLabel="Saving…"
      fallbackError="Couldn't save the discount. Please try again."
      onSubmit={async (v) => {
        const hasDiscount = isPositiveMoney(v.discount_amount)
        const updated = await update.mutateAsync({
          discount_amount: v.discount_amount,
          ...(hasDiscount ? { discount_reason: v.discount_reason } : {}),
        })
        if (updated.discount_pending_approval) {
          return 'Discount saved — it needs approval before the invoice can be issued'
        }
        return hasDiscount ? 'Discount applied' : 'Discount removed'
      }}
    >
      {({ register, formState: { errors } }) => (
        <>
          <Field label="Discount amount" required error={errors.discount_amount?.message}>
            {(p) => <Input inputMode="decimal" {...p} {...register('discount_amount')} />}
          </Field>
          <Field label="Reason" error={errors.discount_reason?.message}>
            {(p) => (
              <Textarea rows={2} placeholder="e.g. Financial hardship" {...p} {...register('discount_reason')} />
            )}
          </Field>
        </>
      )}
    </FormDialog>
  )
}

// ── Issue / void / approve ──────────────────────────────────────────────────

const confirmSchema = z.object({})
type ConfirmValues = z.infer<typeof confirmSchema>

/** Issue a draft (§6.5). Confirmed first: an issued invoice can't be edited again. */
function IssueInvoiceDialog({ invoice, blocked }: { invoice: Invoice; blocked?: string }) {
  const issue = useIssueInvoice(invoice.id)
  return (
    <FormDialog<ConfirmValues>
      trigger={
        <Button size="sm" disabled={!!blocked} title={blocked}>
          <Send className="size-4" /> Issue invoice
        </Button>
      }
      title="Issue this invoice?"
      description={
        <>
          The total of <strong>{formatMoney(invoice.total, invoice.currency)}</strong> is frozen and
          an invoice number is assigned. An issued invoice can't be edited — a correction means
          voiding it and raising a new one.
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Issue invoice"
      pendingLabel="Issuing…"
      dismissLabel="Not yet"
      fallbackError="Couldn't issue the invoice. Please try again."
      onSubmit={async () => {
        const issued = await issue.mutateAsync()
        return issued.invoice_number ? `Invoice ${issued.invoice_number} issued` : 'Invoice issued'
      }}
    >
      {() => null}
    </FormDialog>
  )
}

const voidSchema = z.object({
  reason: z
    .string()
    .trim()
    .min(1, 'Give a reason for voiding')
    .max(500, 'Keep the reason under 500 characters'),
})
type VoidValues = z.infer<typeof voidSchema>

/** Void an issued invoice that has taken no payment (§6.9). */
function VoidInvoiceDialog({ invoice }: { invoice: Invoice }) {
  const voidInvoice = useVoidInvoice(invoice.id)
  return (
    <FormDialog<VoidValues>
      trigger={
        <Button variant="outline" size="sm" className="text-error hover:text-error">
          <Ban className="size-4" /> Void
        </Button>
      }
      title="Void this invoice?"
      description={
        <>
          {invoice.invoice_number} will be cancelled and can't be reinstated. It keeps its number,
          and a new invoice can be raised in its place.
        </>
      }
      resolver={zodResolver(voidSchema)}
      defaults={() => ({ reason: '' })}
      submitLabel="Void invoice"
      pendingLabel="Voiding…"
      destructive
      dismissLabel="Keep invoice"
      fallbackError="Couldn't void the invoice. Please try again."
      onSubmit={async (v) => {
        await voidInvoice.mutateAsync(v.reason)
        return 'Invoice voided'
      }}
    >
      {({ register, formState: { errors } }) => (
        <Field label="Reason" required error={errors.reason?.message}>
          {(p) => <Textarea rows={2} {...p} {...register('reason')} />}
        </Field>
      )}
    </FormDialog>
  )
}

/** Approve a pending discount (§6.7). A 409 means someone else already did. */
function ApproveDiscountButton({ invoice }: { invoice: Invoice }) {
  const approve = useApproveDiscount(invoice.id)
  const busy = useRef(false)

  async function run() {
    if (busy.current) return
    busy.current = true
    try {
      await approve.mutateAsync()
      toast.success('Discount approved')
    } catch (err) {
      toast.error(billingErrorMessage(err, "Couldn't approve the discount. Please try again."))
    } finally {
      busy.current = false
    }
  }

  return (
    <Button size="sm" disabled={approve.isPending} aria-busy={approve.isPending} onClick={() => run()}>
      {approve.isPending ? <Loader2 className="size-4 animate-spin" /> : <BadgeCheck className="size-4" />}
      {approve.isPending ? 'Approving…' : 'Approve discount'}
    </Button>
  )
}

/**
 * The actions an invoice offers, from its status (the table in §6.4) and the
 * user's permissions (§6.11). Hiding an action is a convenience — the API
 * enforces both, and a refusal is shown rather than assumed away.
 */
export function InvoiceActions({ invoice }: { invoice: Invoice }) {
  const { can } = usePermissions()
  const { status } = invoice

  // A receptionist holds the cash-only code; the server refuses any other method.
  const methods: PaymentMethod[] = can('invoice.payment.record')
    ? PAYMENT_METHODS
    : can('invoice.payment.record.cash')
      ? ['cash']
      : []

  const isDraft = status === 'draft'
  const takesPayment = (status === 'issued' || status === 'partially_paid') && methods.length > 0
  const refundable = (status === 'partially_paid' || status === 'paid') && can('invoice.refund')
  const voidable = status === 'issued' && can('invoice.void')

  const issueBlocked = invoice.discount_pending_approval
    ? 'The discount is awaiting approval'
    : invoice.items.length === 0
      ? 'Add at least one charge first'
      : undefined

  const actions = [
    isDraft && can('invoice.update') && <EditChargesDialog key="edit" invoice={invoice} />,
    isDraft && can('invoice.update') && <DiscountDialog key="discount" invoice={invoice} />,
    isDraft && invoice.discount_pending_approval && can('invoice.approve_discount') && (
      <ApproveDiscountButton key="approve" invoice={invoice} />
    ),
    isDraft && can('invoice.issue') && (
      <IssueInvoiceDialog key="issue" invoice={invoice} blocked={issueBlocked} />
    ),
    takesPayment && <RecordPaymentDialog key="pay" invoice={invoice} methods={methods} />,
    refundable && <RefundDialog key="refund" invoice={invoice} />,
    voidable && <VoidInvoiceDialog key="void" invoice={invoice} />,
  ].filter(Boolean)

  if (actions.length === 0) return null
  return (
    <div role="group" aria-label="Invoice actions" className="flex flex-wrap items-center gap-2">
      {actions}
    </div>
  )
}
