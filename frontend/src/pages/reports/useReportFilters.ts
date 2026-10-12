import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { AppointmentsReportParams, Granularity } from '@/api/reports'
import { isGranularity, isISODate, isUuid, periodError } from '@/lib/reportPeriod'

/**
 * The filters of a report, kept in the address bar so a filtered report can be
 * linked to and survives a reload: `?from=&to=&granularity=`, and `doctor_id`
 * and `department_id` on a report that takes them.
 *
 * A filter that is not in the address is not sent. The server then applies its
 * own default (the last 30 days, by day) and says what it used in
 * `data.filters`, which is what the inputs show.
 */

type FilterKey = 'from' | 'to' | 'granularity' | 'doctor_id' | 'department_id'

const VALID: Record<FilterKey, (value: string) => boolean> = {
  from: isISODate,
  to: isISODate,
  granularity: isGranularity,
  doctor_id: isUuid,
  department_id: isUuid,
}

const PERIOD_KEYS: FilterKey[] = ['from', 'to', 'granularity']

export type ReportFilterChanges = Partial<Record<FilterKey, string | undefined>>

export interface ReportFilters {
  from: string | undefined
  to: string | undefined
  granularity: Granularity | undefined
  doctorId: string | undefined
  departmentId: string | undefined
  /** The filters to send: only the ones the address holds. */
  params: AppointmentsReportParams
  /**
   * Why the server would refuse this period, in its words, or null. While it
   * is set the report must not be requested.
   */
  error: string | null
  /** True when the address holds any filter. */
  isFiltered: boolean
  /** Set the given filters; `undefined` or an empty value removes one. */
  update: (changes: ReportFilterChanges) => void
  /** Remove every filter, back to the server's defaults. */
  clear: () => void
}

export function useReportFilters(
  options: { doctor?: boolean; department?: boolean } = {},
): ReportFilters {
  const [searchParams, setSearchParams] = useSearchParams()
  const keys: FilterKey[] = [
    ...PERIOD_KEYS,
    ...(options.doctor ? (['doctor_id'] as const) : []),
    ...(options.department ? (['department_id'] as const) : []),
  ]

  // A value that is not a date, a granularity or an id is treated as absent at
  // once — it is never sent — and then taken out of the address.
  const read = (key: FilterKey): string | undefined => {
    if (!keys.includes(key)) return undefined
    const value = searchParams.get(key)
    return value !== null && VALID[key](value) ? value : undefined
  }
  const malformed = keys
    .filter((key) => {
      const value = searchParams.get(key)
      return value !== null && !VALID[key](value)
    })
    .join(',')

  useEffect(() => {
    if (!malformed) return
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of malformed.split(',')) next.delete(key)
        return next
      },
      { replace: true },
    )
  }, [malformed, setSearchParams])

  const from = read('from')
  const to = read('to')
  const granularity = read('granularity') as Granularity | undefined
  const doctorId = read('doctor_id')
  const departmentId = read('department_id')

  const update = (changes: ReportFilterChanges) =>
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const [key, value] of Object.entries(changes)) {
          if (value) next.set(key, value)
          else next.delete(key)
        }
        return next
      },
      { replace: true },
    )

  const clear = () =>
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current)
        for (const key of keys) next.delete(key)
        return next
      },
      { replace: true },
    )

  return {
    from,
    to,
    granularity,
    doctorId,
    departmentId,
    params: { from, to, granularity, doctor_id: doctorId, department_id: departmentId },
    error: periodError(from, to),
    isFiltered: [from, to, granularity, doctorId, departmentId].some((value) => value !== undefined),
    update,
    clear,
  }
}

/**
 * The hospital's "today" from the last report that answered (`meta.today`).
 * The period presets are counted from it, and it must outlive a request that
 * fails — a preset is how a user gets out of a period the server refused.
 * Never the browser's clock.
 */
export function useLastToday(today: string | undefined): string | undefined {
  const [last, setLast] = useState(today)
  if (today !== undefined && today !== last) setLast(today)
  return today ?? last
}
