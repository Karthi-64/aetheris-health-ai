import { CircleCheck, LogIn, Play, type LucideIcon } from 'lucide-react'
import type { AppointmentStatus, AppointmentTransition } from '@/api/appointments'
import { ApiError } from '@/api/types'
import type { Permission } from '@/lib/rbac'

/** What the queue offers as the next step for an appointment. */
export interface LifecycleStep {
  action: AppointmentTransition
  label: string
  pendingLabel: string
  /** Toast shown once the server has accepted the move. */
  done: string
  /** Backend permission the endpoint requires (module spec §10). */
  permission: Permission
  icon: LucideIcon
  /** For error copy: "to check in", "can no longer be checked in". */
  infinitive: string
  participle: string
}

/**
 * The button offered for each status: booked → check in → start → complete.
 *
 * This is only which button to show. Whether a move is legal is decided by the
 * server's state machine, which answers 400 for anything else — including a
 * row that went stale because someone else acted first.
 */
export const NEXT_STEP: Partial<Record<AppointmentStatus, LifecycleStep>> = {
  booked: {
    action: 'check-in',
    label: 'Check in',
    pendingLabel: 'Checking in…',
    done: 'Patient checked in',
    permission: 'appointment.check_in',
    icon: LogIn,
    infinitive: 'check in',
    participle: 'checked in',
  },
  checked_in: {
    action: 'start',
    label: 'Start',
    pendingLabel: 'Starting…',
    done: 'Consultation started',
    permission: 'appointment.start',
    icon: Play,
    infinitive: 'start',
    participle: 'started',
  },
  in_progress: {
    action: 'complete',
    label: 'Complete',
    pendingLabel: 'Completing…',
    done: 'Consultation completed',
    permission: 'appointment.complete',
    icon: CircleCheck,
    infinitive: 'complete',
    participle: 'completed',
  },
}

/** Statuses the cancel endpoint accepts (module spec §5.4). */
export const CANCELLABLE: ReadonlySet<AppointmentStatus> = new Set(['booked', 'checked_in'])

/**
 * Statuses the no-show endpoint accepts: any that is not terminal and not
 * already a consultation (`ALLOWED_TRANSITIONS` in the appointment service).
 */
export const NO_SHOWABLE: ReadonlySet<AppointmentStatus> = new Set(['booked', 'checked_in'])

/** Statuses the reschedule endpoint accepts (module spec §5.3): not once the patient has checked in. */
export const RESCHEDULABLE: ReadonlySet<AppointmentStatus> = new Set(['booked'])

/**
 * Turn a failed lifecycle call into something reception can act on. Server
 * messages for 5xx and unknown failures are not shown — they are not written
 * for users.
 */
export function lifecycleErrorMessage(
  err: unknown,
  verb: { infinitive: string; participle: string },
): string {
  const status = err instanceof ApiError ? err.status : undefined
  switch (status) {
    case 400:
      return `This appointment can no longer be ${verb.participle} — its status has changed. The queue has been refreshed.`
    case 403:
      return `You don't have permission to ${verb.infinitive} appointments.`
    case 404:
      return 'This appointment no longer exists. The queue has been refreshed.'
    case 409:
      return 'Someone else just updated this appointment. The queue has been refreshed — check its status and try again.'
    case 422:
      return (err as ApiError).message
    default:
      return `Couldn't ${verb.infinitive} the appointment. Please try again.`
  }
}
