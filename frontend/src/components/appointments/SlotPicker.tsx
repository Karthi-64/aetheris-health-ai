import { type ReactNode, useState } from 'react'
import { RotateCw } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { useDoctorSlots, type DoctorSlot } from '@/api/doctors'
import { formatTimeIn } from '@/lib/format'
import { cn } from '@/lib/utils'

const SLOT_NOTE: Record<DoctorSlot['status'], string | undefined> = {
  available: undefined,
  booked: 'Booked',
  on_leave: 'On leave',
}

/** Why a slot cannot be picked, or nothing if it can. */
function slotNote(slot: DoctorSlot, now: number, currentAppointmentId?: string): string | undefined {
  if (currentAppointmentId && slot.appointment_id === currentAppointmentId) return 'Current time'
  return SLOT_NOTE[slot.status] ?? (new Date(slot.end).getTime() <= now ? 'Past' : undefined)
}

/**
 * The doctor's slots for the chosen day (docs/18-API_CONTRACTS.md §4.6).
 * Only an available slot that has not already ended can be picked; the rest
 * are shown, disabled, so it is clear why a time is missing. Times are in the
 * hospital's timezone, which the API names.
 *
 * When an existing appointment is being moved, pass its id: the slot it holds
 * today is then labelled as the current time rather than as someone else's.
 */
export function SlotPicker({
  doctorId,
  date,
  value,
  onChange,
  error,
  currentAppointmentId,
}: {
  doctorId: string
  date: string
  /** The picked slot's `start`, or '' for none. */
  value: string
  onChange: (slot: DoctorSlot) => void
  error?: string
  currentAppointmentId?: string
}) {
  const { data, isPending, isError, refetch } = useDoctorSlots(doctorId, date)
  // Read once per mount: a slot should not flip to "past" mid-render.
  const [now] = useState(() => Date.now())

  let body: ReactNode
  if (isPending) {
    body = (
      <div role="status" aria-label="Loading time slots" className="flex flex-wrap gap-2">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-9 w-20 rounded-full" />
        ))}
      </div>
    )
  } else if (isError) {
    body = (
      <div className="flex flex-wrap items-center gap-3">
        <p className="font-body text-body-sm text-on-surface-variant">
          The doctor's time slots couldn't be loaded.
        </p>
        <Button type="button" variant="outline" size="sm" onClick={() => refetch()}>
          <RotateCw className="size-4" /> Retry
        </Button>
      </div>
    )
  } else if (data.slots.length === 0) {
    body = (
      <p className="font-body text-body-sm text-on-surface-variant">
        This doctor has no time slots on that day. Pick another date or doctor.
      </p>
    )
  } else {
    body = (
      <div className="flex flex-wrap gap-2">
        {data.slots.map((slot) => {
          const note = slotNote(slot, now, currentAppointmentId)
          const selected = value === slot.start
          const label = formatTimeIn(slot.start, data.timezone)
          return (
            <button
              key={slot.start}
              type="button"
              disabled={!!note}
              aria-pressed={selected}
              aria-label={note ? `${label}, ${note.toLowerCase()}` : label}
              title={note}
              onClick={() => onChange(slot)}
              className={cn(
                'font-body text-body-sm focus-visible:ring-secondary rounded-full px-3 py-1.5 tabular-nums transition-colors outline-none focus-visible:ring-2',
                selected
                  ? 'bg-secondary-container text-on-secondary-container font-semibold'
                  : 'neo-pressed bg-surface text-on-surface hover:bg-secondary/10',
                note && 'cursor-not-allowed line-through opacity-50 hover:bg-surface',
              )}
            >
              {label}
            </button>
          )
        })}
      </div>
    )
  }

  return (
    <div role="group" aria-label="Time slots" className="space-y-2">
      <p className="font-label text-on-surface-variant flex items-center gap-1">
        Time slot<span className="text-error">*</span>
        {data && (
          <span className="font-body text-outline text-xs font-normal">· {data.timezone} time</span>
        )}
      </p>
      {body}
      {error && (
        <p className="font-body text-error text-xs" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
