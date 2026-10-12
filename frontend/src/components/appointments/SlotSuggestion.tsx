import { type ReactNode, useId, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Loader2, Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  isNoFreeSlots,
  isUsableRecommendation,
  useSlotRecommendation,
  type SlotRecommendation,
} from '@/api/appointments'
import { doctorKeys, useDoctorSlots, type DaySlots, type DoctorSlot } from '@/api/doctors'
import {
  AI_SLOT_RECOMMENDATION_FLAG,
  hospitalKeys,
  isFeatureAvailable,
  useFeatureFlags,
} from '@/api/hospitals'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatTimeIn } from '@/lib/format'

const SUGGEST = 'Suggest a slot with AI'
const PENDING = 'Getting a suggestion…'
const UNAVAILABLE = 'The suggestion is unavailable right now. Pick a slot below, or ask again.'
const NO_LONGER_FREE = 'The suggested time is no longer free. Pick a slot below, or ask again.'

interface Message {
  text: string
  /** Asking again cannot help, so the button stops sending requests. */
  terminal: boolean
}

const retryable = (text: string): Message => ({ text, terminal: false })
const terminal = (text: string): Message => ({ text, terminal: true })

/**
 * What to say about a request that was refused or never answered. The server's
 * code is read before the status: one 503 covers several different failures.
 */
function failureMessage(error: unknown): Message {
  if (!(error instanceof ApiError)) return retryable(UNAVAILABLE)
  const { code, status } = error
  if (code === 'AI_NOT_CONFIGURED') {
    return terminal('AI suggestions are not set up on this server. Pick a slot below.')
  }
  if (code === 'FEATURE_DISABLED') {
    return terminal('AI slot suggestions are not enabled for this hospital. Pick a slot below.')
  }
  if (code === 'PERMISSION_DENIED' || status === 403) {
    return terminal('Your role cannot ask for AI slot suggestions. Pick a slot below.')
  }
  if (code === 'AI_PROVIDER_TIMEOUT') {
    return retryable('The AI service took too long to answer. Pick a slot below, or ask again.')
  }
  if (code === 'AI_RESPONSE_INVALID') {
    return retryable(
      "The AI's answer could not be used, so there is no suggestion. Pick a slot below, or ask again.",
    )
  }
  // Also an invalid key, a rate limit at the provider or a retired model — cases
  // where the provider did answer — so this never says it could not be contacted.
  const unavailable = retryable('The AI service is unavailable right now. Pick a slot below, or ask again.')
  if (code === 'AI_PROVIDER_UNAVAILABLE') return unavailable
  if (status === undefined) {
    return retryable('No answer came back from the server. Pick a slot below, or ask again.')
  }
  if (status === 429) return retryable('Too many suggestion requests. Wait a minute, then ask again.')
  if (status === 400 || status === 422) {
    return retryable(
      'A suggestion could not be requested for this patient, doctor and date. Pick a slot below.',
    )
  }
  if (status >= 500) return unavailable
  return retryable(UNAVAILABLE)
}

/**
 * The server only suggests a free slot that has not started yet. That is
 * stricter than the picker, which keeps a slot until it has ended.
 */
function isSuggestable(slot: DoctorSlot, now: number): boolean {
  return slot.status === 'available' && Date.parse(slot.start) > now
}

/** The slot in the day's feed that is the recommended instant, if it can still be suggested. */
function findSuggested(
  day: DaySlots | undefined,
  recommendation: SlotRecommendation,
  now: number,
): DoctorSlot | undefined {
  const start = Date.parse(recommendation.slot_start)
  const end = Date.parse(recommendation.slot_end)
  return day?.slots.find(
    (slot) => isSuggestable(slot, now) && Date.parse(slot.start) === start && Date.parse(slot.end) === end,
  )
}

/**
 * An optional AI suggestion of one of the doctor's free slots on the chosen
 * day. It is advice only: it never books, and using it does no more than
 * select that slot in the picker — the form's own Book button is still the
 * only thing that creates an appointment.
 *
 * Offered only to a user who may ask (`appointment.recommend_slot` and
 * `doctor.availability.read`), in a hospital where the feature is available,
 * once the day's slots have loaded and at least one can be suggested. Once it
 * has been asked it stays on screen until patient, doctor or date changes, so
 * its answer is always shown — even when no slot is left to suggest.
 *
 * A refusal that asking again cannot change (not set up, not enabled, not
 * permitted) is reported through `onRefused`; the owner passes it back as
 * `refused` so that a later mount does not offer a request sure to be refused.
 *
 * The reason is text written by a model. It is untrusted and rendered as plain
 * text, never as markup or a link.
 */
export function SlotSuggestion({
  patientId,
  doctorId,
  date,
  selectedStart,
  onUse,
  refused = false,
  onRefused,
}: {
  patientId: string
  doctorId: string
  date: string
  /** The slot currently picked in the form ('' for none). */
  selectedStart: string
  /** Selects the slot in the existing picker. */
  onUse: (slot: DoctorSlot) => void
  /** The server has already refused, for good, while this dialog was open. */
  refused?: boolean
  /** Called when the server refuses in a way asking again cannot change. */
  onRefused?: () => void
}): ReactNode {
  const { can } = usePermissions()
  // The endpoint needs both codes; without them nothing here sends a request.
  const mayAsk = can('appointment.recommend_slot') && can('doctor.availability.read')
  const flags = useFeatureFlags(mayAsk)
  // A passive observer of the query the slot picker already runs: same key, no second request.
  const slots = useDoctorSlots(doctorId, date, { enabled: false })
  const recommend = useSlotRecommendation()
  const queryClient = useQueryClient()
  // `isPending` only shows after a re-render; this stops a second click before that.
  const inFlight = useRef(false)
  const helpId = useId()
  // Read on mount and on each request: a slot should not flip to "started" mid-render.
  const [now, setNow] = useState(() => Date.now())

  const day = slots.data
  const available = mayAsk && isFeatureAvailable(flags.data, AI_SLOT_RECOMMENDATION_FLAG)
  const canSuggest = day !== undefined && day.slots.some((slot) => isSuggestable(slot, now))
  // The rules for offering the control apply until it has been asked. After
  // that it stays, so the answer is shown and the pressed button keeps focus.
  if (recommend.isIdle && (refused || !available || !patientId || !canSuggest)) return null

  const pending = recommend.isPending
  const result: unknown = recommend.data
  let message: Message | undefined
  let match: DoctorSlot | undefined
  let reason: string | null = null
  if (recommend.isError) {
    message = failureMessage(recommend.error)
  } else if (recommend.isSuccess) {
    if (isUsableRecommendation(result)) {
      match = findSuggested(day, result.recommendation, now)
      reason = result.recommendation.reason
      if (!match) message = retryable(NO_LONGER_FREE)
    } else if (isNoFreeSlots(result)) {
      message = retryable('There are no upcoming free slots on this day to suggest.')
    } else {
      message = retryable(UNAVAILABLE)
    }
  }
  // No further request: refused for good, no longer available, or nothing left to suggest.
  const stopped = message?.terminal === true || !available || !patientId || !canSuggest
  const time = match && day ? formatTimeIn(match.start, day.timezone) : ''
  // Offered only against a settled feed, so a slot being re-read cannot be chosen.
  const usable = match && !slots.isFetching && !slots.isError ? match : undefined
  const slotsKey = doctorKeys.slotsFor(doctorId, date)

  async function suggest() {
    if (inFlight.current || stopped) return
    inFlight.current = true
    setNow(Date.now())
    recommend.reset()
    try {
      const answer: unknown = await recommend.mutateAsync({
        patient_id: patientId,
        doctor_id: doctorId,
        date,
      })
      // The server sees no slot the picker could offer, or suggested one the
      // picker does not show as free: the slots on screen are out of date.
      // Refreshed here, once per click — never from render.
      const current = queryClient.getQueryData<DaySlots>(slotsKey)
      const stale =
        isNoFreeSlots(answer) ||
        (isUsableRecommendation(answer) && !findSuggested(current, answer.recommendation, Date.now()))
      if (stale) void queryClient.invalidateQueries({ queryKey: slotsKey })
    } catch (error) {
      // The refusal is shown from the mutation's own state. One that asking
      // again cannot change means the capability read is out of date: it is
      // re-read, and the owner is told so no later mount asks again.
      if (failureMessage(error).terminal) {
        void queryClient.invalidateQueries({ queryKey: hospitalKeys.featureFlags() })
        onRefused?.()
      }
    } finally {
      inFlight.current = false
    }
  }

  return (
    <div
      role="group"
      aria-label="AI slot suggestion"
      className="border-outline-variant/40 flex flex-col items-start gap-2 rounded-xl border border-dashed px-3 py-3"
    >
      <Button
        type="button"
        variant="outline"
        size="sm"
        aria-describedby={helpId}
        aria-busy={pending}
        aria-disabled={pending || stopped}
        onClick={() => void suggest()}
        className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
      >
        {pending ? <Loader2 className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
        {pending ? PENDING : SUGGEST}
      </Button>
      <p id={helpId} className="font-body text-on-surface-variant text-xs">
        Optional. It only suggests a time — nothing is booked until you press Book.
      </p>
      {/* Never `hidden` while empty: a live region must already be exposed to
          assistive technology when its first text arrives, or that text is not
          announced. `sr-only` only takes the empty box out of the layout. */}
      <div
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="font-body text-body-sm text-on-surface space-y-1 self-stretch empty:sr-only"
      >
        {pending ? (
          <p>{PENDING}</p>
        ) : match ? (
          <>
            <p className="text-on-surface-variant text-xs">AI suggested slot — review before booking.</p>
            <p className="font-semibold tabular-nums">AI suggested: {time}</p>
            {reason !== null && reason.trim() !== '' && (
              <p className="text-on-surface-variant break-words">Reason from the AI: {reason}</p>
            )}
            {selectedStart === match.start && <p>{time} is selected below. Press Book to confirm.</p>}
          </>
        ) : message ? (
          <p>{message.text}</p>
        ) : null}
      </div>
      {!pending && usable && (
        <Button type="button" size="sm" onClick={() => onUse(usable)}>
          Use this slot
        </Button>
      )}
    </div>
  )
}
