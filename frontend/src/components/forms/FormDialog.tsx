import { type ReactNode, useEffect, useRef, useState } from 'react'
import {
  FormProvider,
  useForm,
  type DefaultValues,
  type FieldValues,
  type Path,
  type Resolver,
  type UseFormReturn,
} from 'react-hook-form'
import { toast } from 'sonner'
import { Loader2 } from 'lucide-react'
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
import { apiErrorMessage, hasPath, splitFieldErrors } from '@/lib/apiErrors'
import { cn } from '@/lib/utils'
import { FormRefusal } from './formRefusal'

interface FormDialogProps<T extends FieldValues> {
  trigger: ReactNode
  title: string
  description: ReactNode
  resolver: Resolver<T>
  /** Read each time the dialog opens, so it always starts from the record's current values. */
  defaults: () => DefaultValues<T>
  submitLabel: string
  pendingLabel: string
  /** Style the confirm button as destructive (void, refund, deactivate). */
  destructive?: boolean
  /** Label for the button that closes without acting. */
  dismissLabel?: string
  /**
   * Keep the submit button disabled until the user changes something. For an
   * edit form whose API rejects an update that changes nothing.
   */
  requireChanges?: boolean
  /** Extra classes for the dialog panel, e.g. a wider, scrollable one for a long form. */
  contentClassName?: string
  /** Shown when the failure is not one the API explains (5xx, network). */
  fallbackError: string
  /**
   * Send the request. Resolves to the success toast.
   *
   * `opened` is what the form held when the dialog opened. An edit that sends
   * only what changed measures against it — not against the live record, which
   * can be refetched while the dialog is open: a field somebody else changed
   * meanwhile must not be sent back over theirs.
   */
  onSubmit: (values: T, opened: T) => Promise<string>
  children: (form: UseFormReturn<T>) => ReactNode
}

/**
 * A dialog holding one form that makes one API call: an edit, or an action
 * that needs input or confirmation.
 *
 * It owns what those share. The form resets to the record's current values on
 * open; a second submit is ignored while one is in flight; a 422 lands under
 * the field it names (nested and list paths included) and focus goes to the
 * first such field; any other refusal is shown in the dialog in the API's own
 * words, so the user can correct and retry. A click outside does not close a
 * form that has unsaved changes.
 */
export function FormDialog<T extends FieldValues>({
  trigger,
  title,
  description,
  resolver,
  defaults,
  submitLabel,
  pendingLabel,
  destructive,
  dismissLabel = 'Cancel',
  requireChanges,
  contentClassName,
  fallbackError,
  onSubmit,
  children,
}: FormDialogProps<T>) {
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const submitting = useRef(false)
  const noticeRef = useRef<HTMLDivElement>(null)
  // What the form was opened with; see `onSubmit`.
  const [opened, setOpened] = useState(defaults)
  const form = useForm<T>({ resolver, defaultValues: opened })
  const { isDirty } = form.formState
  const unchanged = !!requireChanges && !isDirty

  // On a long, scrolling form the notice renders above the first field while
  // the user is at the submit button below the last one.
  useEffect(() => {
    if (notice) noticeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [notice])

  function onOpenChange(next: boolean) {
    setOpen(next)
    setNotice(null)
    if (next) {
      const current = defaults()
      setOpened(current)
      form.reset(current)
    }
  }

  async function submit(values: T) {
    if (submitting.current) return
    submitting.current = true
    setPending(true)
    setNotice(null)
    try {
      toast.success(await onSubmit(values, opened as T))
      setOpen(false)
    } catch (err) {
      const { onFields, other } = splitFieldErrors(err, (field) => hasPath(form.getValues(), field))
      onFields.forEach((fe, index) =>
        form.setError(fe.field as Path<T>, { message: fe.message }, { shouldFocus: index === 0 }),
      )
      const message =
        err instanceof FormRefusal
          ? err.message
          : other.length > 0
            ? other.join(' ')
            : onFields.length === 0
              ? apiErrorMessage(err, fallbackError)
              : null
      setNotice(message)
      // One toast per refused submit. It is the one place the reason is always
      // in view: the form may be scrolled, and after a 400, 404 or 409 the
      // record is refetched and may no longer offer this action at all — in
      // which case the dialog goes with it.
      toast.error(message ?? "Couldn't save. Check the highlighted fields.")
    } finally {
      submitting.current = false
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent
        className={cn(contentClassName)}
        onInteractOutside={(event) => {
          // A stray click on the backdrop should not throw away typed changes.
          // Escape, the close button and the dismiss button still close.
          if (isDirty) event.preventDefault()
        }}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        <FormProvider {...form}>
          <form onSubmit={(e) => form.handleSubmit(submit)(e)} className="space-y-4" noValidate>
            {notice && (
              <div ref={noticeRef}>
                <Alert variant="error" title="That didn't go through">
                  {notice}
                </Alert>
              </div>
            )}
            {children(form)}
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                {dismissLabel}
              </Button>
              <Button
                type="submit"
                variant={destructive ? 'destructive' : 'default'}
                disabled={pending || unchanged}
                aria-busy={pending}
                title={unchanged ? 'Nothing has been changed yet' : undefined}
              >
                {pending && <Loader2 className="size-4 animate-spin" />}
                {pending ? pendingLabel : submitLabel}
              </Button>
            </DialogFooter>
          </form>
        </FormProvider>
      </DialogContent>
    </Dialog>
  )
}
