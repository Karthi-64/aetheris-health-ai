import { useId } from 'react'
import { CalendarDays } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '@atheris/ui'
import type { MyAppointment } from '@/api/myAppointments'
import { appointmentCancelPath, appointmentPath } from '@/pages/appointments/paths'
import { StatusBadge } from '@/pages/appointments/StatusBadge'
import { appointmentStrings as S } from '@/pages/appointments/strings'
import { appointmentDay, appointmentTime } from '@/pages/appointments/when'

interface AppointmentCardProps {
  appointment: MyAppointment
  /**
   * May this card offer the way to cancelling? Even then it is offered only
   * when the server said the appointment can be cancelled.
   */
  offerCancel: boolean
}

/**
 * One appointment in the list: who with, where, when on the hospital's clock,
 * and its state. The reference is used for the links and never shown. Nothing
 * is cancelled from here — "Cancel" leads to the appointment's own page, which
 * asks first.
 */
export function AppointmentCard({ appointment, offerCancel }: AppointmentCardProps) {
  const id = useId()
  const { doctor, hospital } = appointment
  const named = `${id}-name ${id}-day`

  return (
    <li className="bg-card space-y-4 rounded-2xl border p-4">
      <div className="flex items-start gap-3">
        <span className="bg-secondary-fixed/50 text-secondary flex size-11 shrink-0 items-center justify-center rounded-xl">
          <CalendarDays className="size-5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1 space-y-1">
          <h3 id={`${id}-name`} className="text-body-lg text-on-surface font-semibold break-words">
            {doctor.name}
          </h3>
          {doctor.specialization && <p className="text-body-sm text-on-surface break-words">{doctor.specialization}</p>}
          <p className="text-body-sm text-on-surface-variant break-words">{hospital.name}</p>
          <p id={`${id}-day`} className="text-body-sm text-on-surface pt-1 font-medium">
            {appointmentDay(appointment)}
          </p>
          <p className="text-body-sm text-on-surface break-words tabular-nums">
            {S.timeWithZone(appointmentTime(appointment), appointment.timezone)}
          </p>
          <p className="pt-1">
            <StatusBadge status={appointment.status} />
          </p>
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <Button asChild variant="outline" size="touch" className="w-full">
          {/* Named with the doctor and the day, so twenty of these are not twenty identical links. */}
          <Link id={`${id}-view`} to={appointmentPath(appointment.ref)} aria-labelledby={`${id}-view ${named}`}>
            {S.viewAppointment}
          </Link>
        </Button>
        {offerCancel && appointment.can_cancel === true && (
          <Link
            id={`${id}-cancel`}
            to={appointmentCancelPath(appointment.ref)}
            aria-labelledby={`${id}-cancel ${named}`}
            className="text-error text-body-sm inline-flex min-h-11 items-center justify-center rounded-xl font-semibold hover:underline"
          >
            {S.cancelLink}
          </Link>
        )}
      </div>
    </li>
  )
}
