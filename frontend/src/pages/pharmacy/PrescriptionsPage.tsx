import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ClipboardList, X } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { usePendingPrescriptions, usePrescriptions, type PrescriptionStatus } from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { PRESCRIPTION_STATUS, PRESCRIPTION_STATUSES } from '@/components/pharmacy/pharmacyPresentation'
import { PharmacyTabs } from './PharmacyTabs'
import { prescriptionColumns } from './prescriptionColumns'
import { PrescriptionLoadError } from './PrescriptionsPageLoadError'

const PAGE_SIZE = 25
const ALL = 'all'

/** The dispensing queue, or every prescription. */
type View = 'pending' | 'all'

/**
 * Prescriptions (docs/18-API_CONTRACTS.md §9.4), in two views that are two
 * endpoints.
 *
 * "To dispense" is `GET /prescriptions/pending`: what is waiting, longest
 * waiting first. That endpoint takes paging and nothing else — a filter sent
 * to it is silently ignored — so none is offered there. "All prescriptions" is
 * `GET /prescriptions`, newest first, which filters by status, patient and
 * visit; it has no text search and no sort. `?patient_id=` and
 * `?appointment_id=` narrow that list, which is how a patient's record links
 * here, so either one shows it whatever view was chosen.
 *
 * A prescription is written from a visit (the appointment queue or a
 * patient's record), because it must belong to one.
 */
export default function PrescriptionsPage() {
  const { can } = usePermissions()
  const [searchParams, setSearchParams] = useSearchParams()
  const patientId = searchParams.get('patient_id') ?? undefined
  const appointmentId = searchParams.get('appointment_id') ?? undefined
  const scoped = !!patientId || !!appointmentId
  // Whoever dispenses starts on the queue; a prescriber starts on the list.
  const [chosen, setChosen] = useState<View>(() => (can('pharmacy.dispense.execute') ? 'pending' : 'all'))
  const view: View = scoped ? 'all' : chosen
  // The status filter and the page belong to the scope they were chosen in.
  // The scope is the URL's, so it changes under a mounted page (browser Back
  // after "Show all prescriptions"); a page or filter carried across would ask
  // for page 3 of one patient's two prescriptions and report none.
  const scopeKey = `${patientId ?? ''}|${appointmentId ?? ''}`
  const [chosenWithin, setChosenWithin] = useState({ scopeKey, status: ALL, page: 1 })
  const { status, page } = chosenWithin.scopeKey === scopeKey ? chosenWithin : { status: ALL, page: 1 }
  const setPage = (next: number) => setChosenWithin({ scopeKey, status, page: next })
  const setStatus = (next: string) => setChosenWithin({ scopeKey, status: next, page: 1 })

  const queue = usePendingPrescriptions({ page, page_size: PAGE_SIZE }, { enabled: view === 'pending' })
  const list = usePrescriptions(
    {
      status: status === ALL ? undefined : (status as PrescriptionStatus),
      patient_id: patientId,
      appointment_id: appointmentId,
      page,
      page_size: PAGE_SIZE,
    },
    { enabled: view === 'all' },
  )
  const { data, isPending, isError, error, refetch, isPlaceholderData } = view === 'pending' ? queue : list

  const prescriptions = data?.items ?? []
  const meta = data?.pagination
  const filtered = view === 'all' && status !== ALL
  // While a new scope loads, the rows on hand are the previous request's
  // (`keepPreviousData`) and may be another patient's. They are neither shown
  // under this scope's banner nor used to name it.
  const inScope = (p: (typeof prescriptions)[number]) =>
    (!patientId || p.patient_id === patientId) && (!appointmentId || p.appointment_id === appointmentId)
  const otherScope = scoped && isPlaceholderData && !prescriptions.every(inScope)
  const scopeName = scoped ? prescriptions.find(inScope)?.patient_name : undefined

  return (
    <div className="w-full">
      <PharmacyTabs />
      <PageHeader
        title="Prescriptions"
        subtitle="What doctors have prescribed, and what is waiting to be dispensed."
        actions={
          <>
            <Select
              value={view}
              disabled={scoped}
              onValueChange={(v) => {
                setChosen(v as View)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="View" className="w-48 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="pending">To dispense</SelectItem>
                <SelectItem value="all">All prescriptions</SelectItem>
              </SelectContent>
            </Select>
            {view === 'all' && (
              <Select
                value={status}
                onValueChange={setStatus}
              >
                <SelectTrigger aria-label="Filter by status" className="w-48 rounded-full py-2.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>All statuses</SelectItem>
                  {PRESCRIPTION_STATUSES.map((s) => (
                    <SelectItem key={s} value={s}>
                      {PRESCRIPTION_STATUS[s].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </>
        }
      />

      {scoped && (
        <div className="neo-pressed bg-surface mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl px-4 py-2.5">
          <p className="font-body text-body-sm text-on-surface">
            Showing prescriptions for{' '}
            {patientId && can('patient.read') ? (
              <Link to={`/patients/${patientId}`} className="text-secondary font-semibold hover:underline">
                {scopeName ?? 'this patient'}
              </Link>
            ) : (
              <span className="font-semibold">
                {scopeName ?? (appointmentId ? 'this visit' : 'this patient')}
              </span>
            )}
            {appointmentId && (patientId || scopeName) ? ' — one visit' : ''}
          </p>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              // What the button says: every prescription, not back to the queue.
              setSearchParams({})
              setChosen('all')
            }}
          >
            <X className="size-4" /> Show all prescriptions
          </Button>
        </div>
      )}

      {isError ? (
        <PrescriptionLoadError
          title="Couldn't load prescriptions"
          error={error}
          fallback="The prescriptions could not be reached. Check your connection and try again."
          onRetry={() => refetch()}
        />
      ) : (
        <DataTable
          columns={prescriptionColumns}
          data={otherScope ? [] : prescriptions}
          isLoading={isPending || otherScope}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta && !otherScope ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            view === 'pending' ? (
              <EmptyState
                icon={ClipboardList}
                title="Nothing is waiting to be dispensed"
                description="A prescription appears here as soon as it is written, longest waiting first."
              />
            ) : filtered || scoped ? (
              <EmptyState
                icon={ClipboardList}
                title="No matching prescriptions"
                description={
                  filtered
                    ? 'No prescriptions match this filter.'
                    : 'Nothing has been prescribed here yet.'
                }
              />
            ) : (
              <EmptyState
                icon={ClipboardList}
                title="No prescriptions yet"
                description="Prescriptions are written from a visit — in the appointment queue or on a patient's record — and appear here."
              />
            )
          }
        />
      )}
    </div>
  )
}
