import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { useLabOrders } from '@/api/lab'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime } from '@/lib/format'
import { LabAbnormalMarker, LabOrderStatusBadge, LabPriorityBadge } from './LabBadges'

/** How many of the newest orders to show before linking to the worklist. */
const RECENT = 5

/**
 * A patient's most recent lab orders, linking into Laboratory. Renders
 * nothing for a user who cannot read lab orders.
 */
export function PatientLabOrders({ patientId }: { patientId: string }) {
  const { can } = usePermissions()
  const allowed = can('lab.order.read')
  const { data, isPending, isError } = useLabOrders(
    { patient_id: patientId, page_size: RECENT },
    { enabled: allowed },
  )
  if (!allowed) return null

  const orders = data?.items ?? []
  const total = data?.pagination.total ?? 0

  return (
    <section className="neo-extruded bg-surface rounded-2xl p-6" aria-label="Lab orders">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="font-display text-title-lg text-primary font-bold">Lab orders</h2>
        {total > 0 && (
          <Link
            to={`/laboratory?patient_id=${patientId}`}
            className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 transition-colors"
          >
            {total > RECENT ? `View all ${total} orders` : 'Open in Laboratory'}
            <ChevronRight className="size-4" />
          </Link>
        )}
      </div>

      {isError ? (
        <p className="font-body text-body-sm text-error">This patient's lab orders couldn't be loaded.</p>
      ) : isPending ? (
        <div className="space-y-2" aria-busy="true" aria-label="Loading lab orders">
          <Skeleton className="h-10 w-full rounded-xl" />
          <Skeleton className="h-10 w-full rounded-xl" />
        </div>
      ) : orders.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">No lab orders for this patient yet.</p>
      ) : (
        <ul className="divide-outline-variant/20 divide-y">
          {orders.map((order) => (
            <li key={order.id}>
              <Link
                to={`/laboratory/orders/${order.id}`}
                className="hover:bg-surface-container-low/60 flex flex-wrap items-center justify-between gap-3 rounded-lg px-2 py-2.5 transition-colors"
              >
                <span className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="font-body text-body-sm text-on-surface font-semibold">
                    {order.items.map((i) => i.test_code).join(', ')}
                  </span>
                  <span className="font-body text-outline text-xs tabular-nums">
                    {formatDateTime(order.ordered_at)}
                  </span>
                </span>
                <span className="flex flex-wrap items-center gap-2">
                  {order.priority !== 'routine' && <LabPriorityBadge priority={order.priority} />}
                  <LabAbnormalMarker order={order} />
                  <LabOrderStatusBadge status={order.status} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
