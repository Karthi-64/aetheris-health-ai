import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, RotateCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { formatDate } from '@/lib/format'
import { ApiError } from '@/api/types'
import { GENDER_LABELS, usePatient } from '@/api/patients'
import { usePermissions } from '@/hooks/usePermissions'
import { PatientAppointments } from '@/components/appointments/PatientAppointments'
import { PatientInvoices } from '@/components/billing/PatientInvoices'
import { PatientLabOrders } from '@/components/laboratory/PatientLabOrders'
import { PatientPrescriptions } from '@/components/pharmacy/PatientPrescriptions'
import { EditPatientDialog } from './EditPatientDialog'

function formatAddress(address: Record<string, unknown> | null): string | null {
  if (!address) return null
  const parts = ['line1', 'line2', 'city', 'state', 'postal_code', 'country']
    .map((k) => address[k])
    .filter((v): v is string => typeof v === 'string' && v.length > 0)
  return parts.length ? parts.join(', ') : null
}

function names(items: Array<Record<string, unknown>>): string {
  const list = items.map((i) => (typeof i.name === 'string' ? i.name : null)).filter(Boolean)
  return list.length ? list.join(', ') : ''
}

/** Allergy names, each with its severity when one was recorded. */
function allergyNames(items: Array<Record<string, unknown>>): string {
  return names(
    items.map((i) =>
      typeof i.name === 'string' && typeof i.severity === 'string'
        ? { name: `${i.name} (${i.severity})` }
        : i,
    ),
  )
}

const text = (value: unknown): string | null => (typeof value === 'string' ? value : null)

/**
 * True when the API answered a read and turned it down: the record is gone
 * (404), or this user may no longer see it (403). A request that failed for
 * any other reason — the network, a 5xx, a rate limit — says nothing about
 * the record.
 */
function isRefusal(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.status === undefined) return false
  return error.status >= 400 && error.status < 500 && error.status !== 429
}

export default function PatientDetailPage() {
  const { patientId } = useParams<{ patientId: string }>()
  const { data: patient, error, isError, refetch } = usePatient(patientId ?? '')
  const { can } = usePermissions()

  const backLink = (
    <Link
      to="/patients"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to patients
    </Link>
  )

  // A refresh that merely failed must not take the record off the screen: the
  // edit form is mounted under it, and would go with whatever had been typed.
  if (isError && (!patient || isRefusal(error))) {
    return (
      <div className="w-full space-y-4">
        {backLink}
        <Alert variant="error" title="Couldn't load this patient">
          <div className="flex flex-col items-start gap-3">
            <p>The record could not be reached. It may have been removed, or the server is down.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      </div>
    )
  }

  if (!patient) {
    return (
      <div className="w-full space-y-6" role="status" aria-busy="true" aria-label="Loading patient">
        {backLink}
        <Skeleton className="h-20 w-full max-w-md rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
      </div>
    )
  }

  const allergies = allergyNames(patient.allergies)
  const conditions = names(patient.chronic_conditions)
  const medications = names(patient.current_medications)
  const hasHistory = allergies || conditions || medications
  const contact = patient.emergency_contact
  // The API refuses an update to an inactive record (404), and nothing can reactivate one.
  const canEdit = can('patient.update') && patient.status === 'active'

  return (
    <div className="w-full space-y-6">
      {backLink}

      {isError && (
        <Alert variant="warning" title="Couldn't refresh this record">
          <div className="flex flex-col items-start gap-3">
            <p>This is the copy loaded earlier. It may be out of date.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      )}

      <header className="glassmorphism shadow-glass-panel flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6">
        <div className="flex items-center gap-4">
          <span className="neo-extruded bg-primary-container flex size-14 items-center justify-center rounded-2xl text-lg font-bold text-white">
            {patient.first_name[0]}
            {patient.last_name[0]}
          </span>
          <div>
            <h1 className="font-display text-headline-md text-primary font-bold">
              {patient.full_name}
            </h1>
            <p className="font-mono text-outline text-sm">{patient.mrn}</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant={patient.status === 'active' ? 'success' : 'neutral'} className="capitalize">
            {patient.status}
          </Badge>
          {canEdit && <EditPatientDialog patient={patient} />}
        </div>
      </header>

      <InfoCard title="Demographics">
        <Detail label="Date of birth" value={formatDate(patient.date_of_birth)} />
        <Detail label="Age" value={`${patient.age} years`} />
        <Detail label="Gender" value={GENDER_LABELS[patient.gender]} />
        <Detail label="Blood group" value={patient.blood_group} />
        <Detail label="Marital status" value={patient.marital_status} />
        <Detail label="Occupation" value={patient.occupation} />
      </InfoCard>

      <InfoCard title="Contact">
        <Detail label="Phone" value={patient.phone} />
        <Detail label="Email" value={patient.email} />
        <Detail label="Address" value={formatAddress(patient.address)} />
      </InfoCard>

      <InfoCard title="Emergency contact">
        <Detail label="Name" value={text(contact?.name)} />
        <Detail label="Relationship" value={text(contact?.relation)} />
        <Detail label="Phone" value={text(contact?.phone)} />
      </InfoCard>

      {hasHistory && (
        <InfoCard title="Medical history">
          <Detail label="Allergies" value={allergies || null} />
          <Detail label="Chronic conditions" value={conditions || null} />
          <Detail label="Current medications" value={medications || null} />
        </InfoCard>
      )}

      <InfoCard title="Notes">
        <div className="col-span-full">
          <Detail
            label="Administrative notes"
            value={patient.notes && <p className="whitespace-pre-wrap">{patient.notes}</p>}
          />
        </div>
      </InfoCard>

      <PatientAppointments patient={patient} />

      <PatientLabOrders patientId={patient.id} />

      <PatientPrescriptions patientId={patient.id} />

      <PatientInvoices patientId={patient.id} />
    </div>
  )
}
