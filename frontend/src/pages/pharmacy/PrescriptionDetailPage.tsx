import { Link, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  usePrescription,
  usePrescriptionDispenses,
  type Dispense,
  type Prescription,
  type PrescriptionItem,
} from '@/api/pharmacy'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime, formatMoney } from '@/lib/format'
import { PrescriptionStatusBadge } from '@/components/pharmacy/PharmacyBadges'
import { PRESCRIPTION_DISPENSABLE, canCancelPrescription } from '@/components/pharmacy/pharmacyPresentation'
import { CancelPrescriptionDialog } from './CancelPrescriptionDialog'
import { DispenseDialog, DispensedBatches } from './DispenseDialog'
import { outstandingLines } from './dispenseForm'
import { PrescriptionLoadError } from './PrescriptionsPageLoadError'

function Count({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="font-label text-label-caps text-on-surface-variant">{label}</dt>
      <dd className="font-body text-body-md text-on-surface font-semibold tabular-nums">{value}</dd>
    </div>
  )
}

/**
 * One prescribed line, with the counts as the server holds them.
 *
 * A free-text line shows what was prescribed and nothing else: it is never
 * dispensed here, and its `quantity_remaining` stays at the prescribed amount
 * for ever, so showing it as outstanding would be untrue.
 */
function LineCard({ item, waiting }: { item: PrescriptionItem; waiting: boolean }) {
  const stocked = item.medicine_id !== null
  // What can still be dispensed, and so the only place availability matters.
  const outstanding = waiting && stocked && item.quantity_remaining > 0
  const available = item.available_quantity

  return (
    <li className="neo-extruded bg-surface min-w-0 rounded-2xl p-5">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="font-display text-primary min-w-0 text-base font-bold [overflow-wrap:anywhere]">
          {item.medicine_name}
        </h3>
        {!stocked && <Badge variant="neutral">Not stocked here</Badge>}
      </div>
      <p className="font-body text-body-sm text-on-surface-variant mt-1 [overflow-wrap:anywhere]">
        {item.dosage} · {item.frequency}
        {item.duration_days !== null &&
          ` · ${item.duration_days} ${item.duration_days === 1 ? 'day' : 'days'}`}
      </p>
      {item.instructions && (
        <p className="font-body text-body-sm text-on-surface mt-1 [overflow-wrap:anywhere]">
          {item.instructions}
        </p>
      )}

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-2">
        <Count label="Prescribed" value={item.quantity} />
        {stocked && (
          <>
            <Count label="Dispensed" value={item.quantity_dispensed} />
            <Count label="Remaining" value={item.quantity_remaining} />
          </>
        )}
        {outstanding && available !== null && <Count label="Available today" value={available} />}
      </dl>

      {!stocked && (
        <p className="font-body text-outline mt-2 text-xs">
          Written by name. The pharmacy here cannot dispense it.
        </p>
      )}
      {outstanding && available !== null && available < item.quantity_remaining && (
        <p className="font-body text-body-sm text-error mt-2 font-semibold">
          {available === 0 ? 'Out of stock' : `Only ${available} available`}
        </p>
      )}
    </li>
  )
}

/**
 * One dispense as it was recorded. `warnings` is deliberately not shown: the
 * API works them out against today's date on every read, so on a past
 * dispense they are not what was true when it was handed over. They are shown
 * once, from the answer to the dispense itself.
 */
function DispenseCard({ prescription, dispense }: { prescription: Prescription; dispense: Dispense }) {
  const { canAny } = usePermissions()
  return (
    <li className="neo-extruded bg-surface min-w-0 space-y-3 rounded-2xl p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="font-display text-primary text-base font-bold tabular-nums">
          {formatDateTime(dispense.dispensed_at)}
        </h3>
        <p className="font-body text-body-sm text-on-surface">
          Total <span className="font-semibold tabular-nums">{formatMoney(dispense.total_amount)}</span>
          <span className="text-outline"> · </span>
          {dispense.invoice_id ? (
            canAny('invoice.read') ? (
              <Link to={`/billing/${dispense.invoice_id}`} className="text-secondary hover:underline">
                View invoice
              </Link>
            ) : (
              'Charged to an invoice'
            )
          ) : (
            'No invoice linked'
          )}
        </p>
      </div>
      {dispense.notes && (
        <p className="font-body text-body-sm text-on-surface-variant [overflow-wrap:anywhere]">
          Note: {dispense.notes}
        </p>
      )}
      <DispensedBatches prescription={prescription} dispense={dispense} />
    </li>
  )
}

function DispenseHistory({ prescription }: { prescription: Prescription }) {
  const { data: dispenses, isPending, isError, error, refetch } = usePrescriptionDispenses(prescription.id)

  return (
    <section className="space-y-3" aria-label="Dispense history">
      <h2 className="font-display text-title-lg text-primary font-bold">Dispense history</h2>
      {isError ? (
        <PrescriptionLoadError
          title="Couldn't load the dispense history"
          error={error}
          fallback="What has been dispensed could not be reached. Check your connection and try again."
          onRetry={() => refetch()}
        />
      ) : isPending ? (
        <div className="space-y-2" aria-busy="true" aria-label="Loading dispense history">
          <Skeleton className="h-24 w-full rounded-2xl" />
        </div>
      ) : dispenses.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">
          Nothing has been dispensed from this prescription.
        </p>
      ) : (
        <ol className="space-y-4">
          {dispenses.map((dispense) => (
            <DispenseCard key={dispense.id} prescription={prescription} dispense={dispense} />
          ))}
        </ol>
      )}
    </section>
  )
}

/**
 * One prescription (`GET /prescriptions/{id}`): who it is for, each line as
 * the server counts it, and every dispense made from it. Status, quantities,
 * availability, totals and prices are all the server's.
 */
export default function PrescriptionDetailPage() {
  const { prescriptionId = '' } = useParams<{ prescriptionId: string }>()
  const { can } = usePermissions()
  const { data: prescription, isError, error, refetch } = usePrescription(prescriptionId)

  const backLink = (
    <Link
      to="/pharmacy/prescriptions"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to prescriptions
    </Link>
  )

  // Only a failure with nothing to show replaces the page. A failed REFETCH
  // keeps `data` (and sets `isError`): replacing the page then would unmount an
  // open dispense dialog — and with it the only view of the server's batches
  // and expiry warnings, or of "not confirmed, check the history" (§9.5).
  if (isError && !prescription) {
    const notFound = error instanceof ApiError && error.status === 404
    return (
      <div className="w-full space-y-4">
        {backLink}
        {notFound ? (
          <Alert variant="error" title="Prescription not found">
            This prescription doesn't exist, or you don't have access to it.
          </Alert>
        ) : (
          <PrescriptionLoadError
            title="Couldn't load this prescription"
            error={error}
            fallback="The prescription could not be reached. Check your connection and try again."
            onRetry={() => refetch()}
          />
        )}
      </div>
    )
  }

  if (!prescription) {
    return (
      <div className="w-full space-y-6" aria-busy="true" aria-label="Loading prescription">
        {backLink}
        <Skeleton className="h-24 w-full rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
        <Skeleton className="h-48 w-full rounded-2xl" />
      </div>
    )
  }

  const waiting = PRESCRIPTION_DISPENSABLE.has(prescription.status)
  // The dialog itself drops out once nothing is left to dispense, and stays
  // while it is showing the result of a dispense that finished the prescription.
  const canDispense = can('pharmacy.dispense.execute')
  // Cancelling has no permission of its own: the endpoint is guarded by
  // `pharmacy.prescription.create` (§9.1), which a pharmacist does not hold.
  const canCancel = canCancelPrescription(prescription) && can('pharmacy.prescription.create')

  return (
    <div className="w-full space-y-6">
      {backLink}

      {isError && (
        <PrescriptionLoadError
          variant="warning"
          title="This prescription may be out of date"
          error={error}
          fallback="The latest version could not be loaded, so what is shown here may have changed. Refresh it before acting on it."
          onRetry={() => refetch()}
        />
      )}

      <header className="glassmorphism shadow-glass-panel flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="font-display text-headline-md text-primary font-bold">Prescription</h1>
            <PrescriptionStatusBadge status={prescription.status} />
          </div>
          <p className="font-body text-body-md text-on-surface-variant mt-1">
            {can('patient.read') ? (
              <Link to={`/patients/${prescription.patient_id}`} className="text-secondary hover:underline">
                {prescription.patient_name}
              </Link>
            ) : (
              prescription.patient_name
            )}{' '}
            <span className="text-outline font-mono text-sm">· {prescription.patient_mrn}</span>
          </p>
        </div>
        {(canDispense || canCancel) && (
          <div className="flex flex-wrap items-center justify-end gap-2">
            {canDispense && <DispenseDialog prescription={prescription} />}
            {canCancel && <CancelPrescriptionDialog prescription={prescription} />}
          </div>
        )}
      </header>

      {prescription.status === 'cancelled' && (
        <Alert variant="error" title="This prescription was cancelled">
          {prescription.cancel_reason}
          {prescription.cancelled_at && (
            <span className="text-outline"> · {formatDateTime(prescription.cancelled_at)}</span>
          )}
        </Alert>
      )}
      {prescription.status === 'active' && outstandingLines(prescription).length === 0 && (
        <Alert variant="info" title="Nothing to dispense here">
          Nothing on this prescription is stocked here, so there is nothing to dispense. It stays
          open, and in the dispensing queue, until someone who can cancel prescriptions cancels it.
        </Alert>
      )}
      {prescription.status === 'partially_dispensed' && (
        <Alert variant="warning" title="Partly dispensed">
          What is still outstanding stays in the dispensing queue until it is dispensed. A
          prescription cannot be cancelled or closed once something has been dispensed from it.
        </Alert>
      )}

      <InfoCard title="Details">
        <Detail label="Patient" value={prescription.patient_name} />
        <Detail label="MRN" value={prescription.patient_mrn} />
        <Detail label="Prescribed by" value={prescription.doctor_name} />
        <Detail label="Prescribed" value={formatDateTime(prescription.prescribed_at)} />
        <Detail
          label="Visit"
          value={
            <Link
              to={`/pharmacy/prescriptions?appointment_id=${prescription.appointment_id}`}
              className="text-secondary hover:underline"
            >
              All prescriptions from this visit
            </Link>
          }
        />
        <Detail label="Notes" value={prescription.notes} />
      </InfoCard>

      <section className="space-y-3" aria-label="Medicines">
        <h2 className="font-display text-title-lg text-primary font-bold">Medicines</h2>
        <ul className="grid gap-4 lg:grid-cols-2">
          {prescription.items.map((item) => (
            <LineCard key={item.id} item={item} waiting={waiting} />
          ))}
        </ul>
      </section>

      <DispenseHistory prescription={prescription} />
    </div>
  )
}
