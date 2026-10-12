import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { Skeleton } from '@/components/ui/skeleton'
import { useLabOrder, type LabOrder, type LabOrderItem } from '@/api/lab'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime, formatMoney } from '@/lib/format'
import {
  LabOrderStatusBadge,
  LabPriorityBadge,
  LabResultFlagBadge,
} from '@/components/laboratory/LabBadges'
import { LAB_FLAG, referenceRangeLabel, resultLabel } from '@/components/laboratory/labPresentation'
import { AmendResultDialog, LabOrderActions } from './LabOrderActions'

/** "3 h 20 min" from the server's order-to-release minutes. */
function turnaroundLabel(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours} h ${rest} min` : `${hours} h`
}

/** One test on the order: its sample, its result as the server judged it, and any corrections. */
function ResultCard({ order, item }: { order: LabOrder; item: LabOrderItem }) {
  const { can } = usePermissions()
  const range = referenceRangeLabel(item)
  const amended = item.amendments.length > 0
  const canAmend = order.status === 'released' && item.result_value !== null && can('lab.order.amend')

  return (
    <li className="neo-extruded bg-surface rounded-2xl p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="font-display text-primary text-base font-bold">{item.test_name}</h3>
          <p className="font-body text-outline text-xs">
            <span className="font-mono">{item.test_code}</span>
            {' · '}
            {formatMoney(item.price)}
            {item.sample_id && (
              <>
                {' · sample '}
                <span className="font-mono">{item.sample_id}</span>
              </>
            )}
          </p>
        </div>
        {canAmend && <AmendResultDialog order={order} item={item} />}
      </div>

      {item.result_value === null ? (
        <p className="font-body text-body-sm text-on-surface-variant mt-3">
          {item.sample_collected_at ? 'Sample collected — no result yet.' : 'Sample not collected yet.'}
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <p className="font-display text-primary text-2xl font-bold tabular-nums [overflow-wrap:anywhere]">
              {resultLabel(item.result_value, item.result_unit)}
            </p>
            <LabResultFlagBadge flag={item.result_flag} />
            {amended && <Badge variant="primary">Amended</Badge>}
          </div>
          <p className="font-body text-outline text-xs">
            {range && (
              <>
                Reference range used: <span className="tabular-nums">{resultLabel(range, item.result_unit)}</span>
                {' · '}
              </>
            )}
            {item.result_entered_at && <>entered {formatDateTime(item.result_entered_at)}</>}
          </p>
          {item.notes && (
            <p className="font-body text-body-sm text-on-surface-variant [overflow-wrap:anywhere]">
              Note: {item.notes}
            </p>
          )}
        </div>
      )}

      {amended && (
        <div className="border-outline-variant/30 mt-4 border-t pt-3">
          <p className="font-label text-label-caps text-on-surface-variant mb-2">Amendment history</p>
          <ol className="space-y-2">
            {item.amendments.map((a) => (
              <li key={a.id} className="font-body text-body-sm text-on-surface">
                <span className="tabular-nums">
                  {a.previous_value ?? '—'}
                  {a.previous_flag ? ` (${LAB_FLAG[a.previous_flag].label})` : ''}
                </span>
                {' → '}
                <span className="font-semibold tabular-nums">
                  {a.new_value}
                  {a.new_flag ? ` (${LAB_FLAG[a.new_flag].label})` : ''}
                </span>
                <span className="text-outline text-xs"> · {formatDateTime(a.amended_at)}</span>
                <span className="text-on-surface-variant block text-xs [overflow-wrap:anywhere]">
                  Reason: {a.reason}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </li>
  )
}

/**
 * One lab order (`GET /lab-orders/{id}`): who it is for, where it stands, and
 * each test's result exactly as the server holds it. Flags, reference ranges
 * and the turnaround are the server's — nothing clinical is worked out here.
 */
export default function LabOrderDetailPage() {
  const { orderId = '' } = useParams<{ orderId: string }>()
  const { can, canAny } = usePermissions()
  const { data: order, isError, error, refetch } = useLabOrder(orderId)

  const backLink = (
    <Link
      to="/laboratory"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to laboratory
    </Link>
  )

  if (isError) {
    const notFound = error instanceof ApiError && error.status === 404
    return (
      <div className="w-full space-y-4">
        {backLink}
        <Alert variant="error" title={notFound ? 'Lab order not found' : "Couldn't load this lab order"}>
          <div className="flex flex-col items-start gap-3">
            <p>
              {notFound
                ? "This lab order doesn't exist, or you don't have access to it."
                : 'The lab order could not be reached. Check your connection and try again.'}
            </p>
            {!notFound && (
              <Button variant="outline" size="sm" onClick={() => refetch()}>
                <RotateCw className="size-4" /> Retry
              </Button>
            )}
          </div>
        </Alert>
      </div>
    )
  }

  if (!order) {
    return (
      <div className="w-full space-y-6" aria-busy="true" aria-label="Loading lab order">
        {backLink}
        <Skeleton className="h-24 w-full rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
        <Skeleton className="h-48 w-full rounded-2xl" />
      </div>
    )
  }

  const total = order.items.reduce((sum, item) => sum + Number(item.price), 0)

  return (
    <div className="w-full space-y-6">
      {backLink}

      <header className="glassmorphism shadow-glass-panel flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="font-display text-headline-md text-primary font-bold">Lab order</h1>
            <LabOrderStatusBadge status={order.status} />
            <LabPriorityBadge priority={order.priority} />
          </div>
          <p className="font-body text-body-md text-on-surface-variant mt-1">
            {can('patient.read') ? (
              <Link to={`/patients/${order.patient_id}`} className="text-secondary hover:underline">
                {order.patient_name}
              </Link>
            ) : (
              order.patient_name
            )}{' '}
            <span className="text-outline font-mono text-sm">· {order.patient_mrn}</span>
          </p>
        </div>
        <LabOrderActions order={order} />
      </header>

      {order.has_critical && (
        <Alert variant="error" title="Critical result">
          At least one result on this order is in the critical range for this patient.
          {order.status !== 'released' && order.status !== 'cancelled'
            ? ' The ordering doctor was notified when it was entered; the results have not been released yet.'
            : ''}
        </Alert>
      )}
      {order.status === 'cancelled' && (
        <Alert variant="error" title="This order was cancelled">
          {order.cancel_reason}
          {order.invoice_id ? ' Its charges were not removed from the invoice.' : ''}
        </Alert>
      )}
      {order.status === 'results_entered' && !can('lab.order.release') && (
        <Alert variant="info" title="Awaiting release">
          Every result has been entered. Someone with permission to release results sends them to
          the ordering doctor.
        </Alert>
      )}

      <InfoCard title="Details">
        <Detail label="Patient" value={order.patient_name} />
        <Detail label="MRN" value={order.patient_mrn} />
        <Detail label="Ordered by" value={order.doctor_name} />
        <Detail label="Ordered" value={formatDateTime(order.ordered_at)} />
        <Detail label="Sample collected" value={order.collected_at && formatDateTime(order.collected_at)} />
        <Detail
          label="Results entered"
          value={order.results_entered_at && formatDateTime(order.results_entered_at)}
        />
        <Detail label="Released" value={order.released_at && formatDateTime(order.released_at)} />
        <Detail
          label="Turnaround"
          value={order.turnaround_minutes === null ? null : turnaroundLabel(order.turnaround_minutes)}
        />
        <Detail
          label="Charged to"
          value={
            order.invoice_id ? (
              canAny('invoice.read') ? (
                <Link to={`/billing/${order.invoice_id}`} className="text-secondary hover:underline">
                  View invoice · {formatMoney(total)}
                </Link>
              ) : (
                `Invoice · ${formatMoney(total)}`
              )
            ) : null
          }
        />
        <Detail
          label="Visit"
          value={
            order.appointment_id ? (
              <Link
                to={`/laboratory?appointment_id=${order.appointment_id}`}
                className="text-secondary hover:underline"
              >
                All orders from this visit
              </Link>
            ) : null
          }
        />
        <Detail label="Notes for the lab" value={order.notes} />
      </InfoCard>

      <section className="space-y-3" aria-label="Tests and results">
        <h2 className="font-display text-title-lg text-primary font-bold">Tests and results</h2>
        <ul className="grid gap-4 lg:grid-cols-2">
          {order.items.map((item) => (
            <ResultCard key={item.id} order={order} item={item} />
          ))}
        </ul>
      </section>
    </div>
  )
}
