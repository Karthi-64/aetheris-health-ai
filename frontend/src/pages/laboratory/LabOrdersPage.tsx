import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { BookOpenText, FlaskConical, RotateCw, X } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useLabOrders, type LabOrderPriority, type LabOrderStatus } from '@/api/lab'
import { usePermissions } from '@/hooks/usePermissions'
import {
  LAB_ORDER_STATUS,
  LAB_ORDER_STATUSES,
  LAB_PRIORITIES,
  LAB_PRIORITY,
} from '@/components/laboratory/labPresentation'
import { labOrderColumns } from './columns'

const PAGE_SIZE = 25
const ALL = 'all'

/**
 * The lab worklist (docs/18-API_CONTRACTS.md §8.4), newest first.
 *
 * The API filters by status, priority, patient, doctor and visit; it has no
 * text search and no sort, so neither is offered. `?patient_id=` and
 * `?appointment_id=` narrow the list — that is how a patient's record links
 * here.
 *
 * Orders are placed from a visit (the appointment queue or a patient's
 * record), because an order must belong to one.
 */
export default function LabOrdersPage() {
  const { can } = usePermissions()
  const [searchParams, setSearchParams] = useSearchParams()
  const patientId = searchParams.get('patient_id') ?? undefined
  const appointmentId = searchParams.get('appointment_id') ?? undefined
  const [status, setStatus] = useState<string>(ALL)
  const [priority, setPriority] = useState<string>(ALL)
  const [page, setPage] = useState(1)

  const { data, isPending, isError, refetch } = useLabOrders({
    status: status === ALL ? undefined : (status as LabOrderStatus),
    priority: priority === ALL ? undefined : (priority as LabOrderPriority),
    patient_id: patientId,
    appointment_id: appointmentId,
    page,
    page_size: PAGE_SIZE,
  })

  const orders = data?.items ?? []
  const meta = data?.pagination
  const filtered = status !== ALL || priority !== ALL
  const scoped = !!patientId || !!appointmentId
  // Every row in a scoped list is the same patient's.
  const scopeName = orders[0]?.patient_name

  return (
    <div className="w-full">
      <PageHeader
        title="Laboratory"
        subtitle="Lab orders from sample collection to released results."
        actions={
          <>
            <Select
              value={status}
              onValueChange={(v) => {
                setStatus(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by status" className="w-48 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All statuses</SelectItem>
                {LAB_ORDER_STATUSES.map((s) => (
                  <SelectItem key={s} value={s}>
                    {LAB_ORDER_STATUS[s].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={priority}
              onValueChange={(v) => {
                setPriority(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by priority" className="w-40 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All priorities</SelectItem>
                {LAB_PRIORITIES.map((p) => (
                  <SelectItem key={p} value={p}>
                    {LAB_PRIORITY[p].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {can('lab.test.read') && (
              <Button asChild variant="outline" className="rounded-full">
                <Link to="/laboratory/catalog">
                  <BookOpenText className="size-4" /> Test catalog
                </Link>
              </Button>
            )}
          </>
        }
      />

      {scoped && (
        <div className="neo-pressed bg-surface mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl px-4 py-2.5">
          <p className="font-body text-body-sm text-on-surface">
            Showing lab orders for{' '}
            {patientId && can('patient.read') ? (
              <Link to={`/patients/${patientId}`} className="text-secondary font-semibold hover:underline">
                {scopeName ?? 'this patient'}
              </Link>
            ) : (
              <span className="font-semibold">
                {scopeName ?? (appointmentId ? 'this visit' : 'this patient')}
              </span>
            )}
            {appointmentId && patientId ? ' — one visit' : appointmentId && scopeName ? ' — one visit' : ''}
          </p>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setSearchParams({})
              setPage(1)
            }}
          >
            <X className="size-4" /> Show all orders
          </Button>
        </div>
      )}

      {isError ? (
        <Alert variant="error" title="Couldn't load lab orders">
          <div className="flex flex-col items-start gap-3">
            <p>The lab worklist could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={labOrderColumns}
          data={orders}
          isLoading={isPending}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            filtered || scoped ? (
              <EmptyState
                icon={FlaskConical}
                title="No matching lab orders"
                description={
                  filtered
                    ? 'No lab orders match these filters.'
                    : 'No tests have been ordered here yet.'
                }
              />
            ) : (
              <EmptyState
                icon={FlaskConical}
                title="No lab orders yet"
                description="Tests are ordered from a visit — in the appointment queue or on a patient's record — and appear here."
              />
            )
          }
        />
      )}
    </div>
  )
}
