import { useEffect, useState } from 'react'
import { RotateCw, Search, Stethoscope } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useDoctors } from '@/api/doctors'
import { useDepartments } from '@/api/departments'
import { doctorColumns } from './columns'

const PAGE_SIZE = 25
const ALL = 'all'

export default function DoctorsPage() {
  const [searchInput, setSearchInput] = useState('')
  const [q, setQ] = useState('')
  const [department, setDepartment] = useState<string>(ALL)
  const [page, setPage] = useState(1)

  useEffect(() => {
    const t = setTimeout(() => {
      setQ(searchInput.trim())
      setPage(1)
    }, 300)
    return () => clearTimeout(t)
  }, [searchInput])

  const { data: departments } = useDepartments()

  const { data, isPending, isError, refetch } = useDoctors({
    q: q || undefined,
    department: department === ALL ? undefined : department,
    page,
    page_size: PAGE_SIZE,
  })

  const doctors = data?.items ?? []
  const meta = data?.pagination
  const filtered = q !== '' || department !== ALL

  return (
    <div className="w-full">
      <PageHeader
        title="Doctors"
        subtitle="Clinical directory — specialties, departments and availability."
        actions={
          <>
            <div className="relative">
              <Search className="text-outline pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
              <input
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Search name or licence…"
                aria-label="Search doctors"
                className="neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant focus-visible:ring-secondary w-full rounded-full py-2.5 pr-4 pl-9 outline-none focus-visible:ring-2 sm:w-56"
              />
            </div>
            <Select
              value={department}
              onValueChange={(v) => {
                setDepartment(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by department" className="w-44 rounded-full py-2.5">
                <SelectValue placeholder="Department" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All departments</SelectItem>
                {departments?.map((d) => (
                  <SelectItem key={d.id} value={d.id}>
                    {d.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load doctors">
          <div className="flex flex-col items-start gap-3">
            <p>The directory could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={doctorColumns}
          data={doctors}
          isLoading={isPending}
          emptyState={
            <EmptyState
              icon={filtered ? Search : Stethoscope}
              title={filtered ? 'No matches' : 'No doctors yet'}
              description={
                filtered
                  ? 'No doctors match the current search and filter.'
                  : 'Doctors added by an administrator will appear here.'
              }
            />
          }
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
        />
      )}
    </div>
  )
}
