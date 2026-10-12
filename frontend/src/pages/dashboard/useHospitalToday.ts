import {
  useAdminDashboard,
  useBillingDashboard,
  useDoctorDashboard,
  useReceptionDashboard,
} from '@/api/reports'
import { usePermissions } from '@/hooks/usePermissions'
import { todayISODate } from '@/lib/format'
import { dashboardSectionsFor, firstDashboardSection } from './dashboardSections'

export interface HospitalToday {
  /** `YYYY-MM-DD`; undefined until it is known. */
  today: string | undefined
  /** False only while the dashboard that carries the date is still loading. */
  isResolved: boolean
}

/**
 * The calendar day the home page's queue should ask for.
 *
 * The server reads a day filter in the hospital's timezone, and the dashboard
 * tiles count the hospital's day too, so the queue under them must use the
 * same day: `meta.today` of the first dashboard section the user is shown.
 * That section's own hook has the same query key, so this adds no request.
 *
 * The browser's date is used only when there is nothing better: for a user
 * with no dashboard section (no request is made for them), and when that
 * dashboard could not be read — a failed tile must not blank the queue.
 */
export function useHospitalToday(): HospitalToday {
  const { can } = usePermissions()
  const first = firstDashboardSection(dashboardSectionsFor(can))

  const admin = useAdminDashboard({ enabled: first === 'admin' })
  const billing = useBillingDashboard({ enabled: first === 'billing' })
  const reception = useReceptionDashboard({ enabled: first === 'reception' })
  const doctor = useDoctorDashboard({ enabled: first === 'doctor' })

  if (!first) return { today: todayISODate(), isResolved: true }
  const source = { admin, billing, reception, doctor }[first]
  if (source.data) return { today: source.data.meta.today, isResolved: true }
  if (source.isError) return { today: todayISODate(), isResolved: true }
  return { today: undefined, isResolved: false }
}
