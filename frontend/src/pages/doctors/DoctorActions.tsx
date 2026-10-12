import { useEffect, useRef } from 'react'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Loader2, UserCheck, UserX } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { FormDialog } from '@/components/forms/FormDialog'
import { useActivateDoctor, useDeactivateDoctor, type Doctor } from '@/api/doctors'
import { usePermissions } from '@/hooks/usePermissions'
import { apiErrorMessage } from '@/lib/apiErrors'
import { EditDoctorDialog } from './EditDoctorDialog'

const confirmSchema = z.object({})
type ConfirmValues = z.infer<typeof confirmSchema>

interface StatusActionProps {
  doctor: Doctor
  /** Called as the change of status is asked for, before its outcome is known. */
  onRequest: () => void
}

/**
 * Deactivate a doctor (`DELETE /doctors/{id}`, which keeps the record).
 * Confirmed first because it takes the doctor off the directory and out of
 * booking. The API refuses while future appointments exist and says what to
 * do about them; that message is shown as it is.
 */
function DeactivateDoctorDialog({ doctor, onRequest }: StatusActionProps) {
  const deactivate = useDeactivateDoctor(doctor.id)
  return (
    <FormDialog<ConfirmValues>
      trigger={
        <Button variant="outline" size="sm" className="text-error hover:text-error">
          <UserX className="size-4" /> Deactivate
        </Button>
      }
      title={`Deactivate ${doctor.full_name}?`}
      description={
        <>
          They will be hidden from the doctor directory and can no longer be booked for
          appointments. Nothing is deleted: the profile and its history are kept, and this can be
          reversed by reactivating the doctor.
        </>
      }
      resolver={zodResolver(confirmSchema)}
      defaults={() => ({})}
      submitLabel="Deactivate doctor"
      pendingLabel="Deactivating…"
      destructive
      dismissLabel="Keep active"
      fallbackError="Couldn't deactivate the doctor. Please try again."
      onSubmit={async () => {
        onRequest()
        await deactivate.mutateAsync()
        return 'Doctor deactivated'
      }}
    >
      {() => null}
    </FormDialog>
  )
}

/** Reactivate a doctor. Not confirmed: it restores what was there and is itself reversible. */
function ReactivateDoctorButton({ doctor, onRequest }: StatusActionProps) {
  const activate = useActivateDoctor(doctor.id)
  const busy = useRef(false)

  async function run() {
    if (busy.current) return
    busy.current = true
    onRequest()
    try {
      await activate.mutateAsync()
      toast.success('Doctor reactivated')
    } catch (err) {
      toast.error(apiErrorMessage(err, "Couldn't reactivate the doctor. Please try again."))
    } finally {
      busy.current = false
    }
  }

  return (
    <Button size="sm" disabled={activate.isPending} aria-busy={activate.isPending} onClick={() => run()}>
      {activate.isPending ? <Loader2 className="size-4 animate-spin" /> : <UserCheck className="size-4" />}
      {activate.isPending ? 'Reactivating…' : 'Reactivate'}
    </Button>
  )
}

/**
 * The actions a doctor's record offers, from its status and the user's
 * permissions. A deactivated doctor cannot be edited until reactivated, and
 * reactivating needs `doctor.update`, not `doctor.delete`. Hiding an action
 * is a convenience — the API enforces both.
 */
export function DoctorActions({ doctor }: { doctor: Doctor }) {
  const { can } = usePermissions()
  const active = doctor.status === 'active'
  const group = useRef<HTMLDivElement>(null)
  const statusRequested = useRef(false)

  // A change of status replaces every action here, the one holding the
  // keyboard focus included, and the focus would drop to the top of the page.
  // When the change was asked for from here, the group takes it instead. A
  // focus that is anywhere else by then has been moved on purpose and is left.
  useEffect(() => {
    if (!statusRequested.current) return
    statusRequested.current = false
    if (document.activeElement === document.body) group.current?.focus()
  }, [active])

  const onRequest = () => {
    statusRequested.current = true
  }

  const actions = [
    active && can('doctor.update') && <EditDoctorDialog key="edit" doctor={doctor} />,
    active && can('doctor.delete') && (
      <DeactivateDoctorDialog key="deactivate" doctor={doctor} onRequest={onRequest} />
    ),
    !active && can('doctor.update') && (
      <ReactivateDoctorButton key="reactivate" doctor={doctor} onRequest={onRequest} />
    ),
  ].filter(Boolean)

  if (actions.length === 0) return null
  return (
    <div
      ref={group}
      role="group"
      aria-label="Doctor actions"
      tabIndex={-1}
      className="focus-visible:ring-secondary flex flex-wrap items-center gap-2 rounded-md outline-none focus-visible:ring-2"
    >
      {actions}
    </div>
  )
}
