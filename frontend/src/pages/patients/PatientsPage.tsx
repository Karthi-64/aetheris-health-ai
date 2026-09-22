import { useEffect, useState } from 'react'
import { Download, UserPlus, Users } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { usePatients } from '@/api/patients'
import { patientColumns } from './columns'
import { RegisterPatientDialog } from './RegisterPatientDialog'

/** Rows per request. The backend caps `page_size` at 100. */
const PAGE_SIZE = 10

export default function PatientsPage() {
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [page, setPage] = useState(1)

  // Search runs on the server, so every keystroke would otherwise be a request.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(timer)
  }, [search])

  // A narrowed result set is rarely as deep as the page you were on, so a new
  // search starts from the first page.
  function handleSearchChange(value: string) {
    setSearch(value)
    setPage(1)
  }

  const { data, isLoading, isError } = usePatients({
    q: debouncedSearch || undefined,
    page,
    page_size: PAGE_SIZE,
  })

  const patients = data?.items ?? []
  const totalPages = data?.pagination.totalPages ?? 0

  const registerButton = (
    <Button className="rounded-full">
      <UserPlus className="size-4" /> Register Patient
    </Button>
  )

  return (
    <div className="w-full">
      <PageHeader
        title="Patients"
        subtitle="Patient registry, admissions and AI-assisted summaries."
        actions={
          <>
            <Button variant="outline" className="rounded-full">
              <Download className="size-4" /> Export
            </Button>
            <RegisterPatientDialog trigger={registerButton} />
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load patients">
          Something went wrong fetching the patient registry. Please retry.
        </Alert>
      ) : (
        <DataTable
          columns={patientColumns}
          data={patients}
          isLoading={isLoading}
          searchable
          // `q` matches a name *prefix*, or an exact MRN or phone number — the
          // placeholder says so rather than promising a substring search.
          searchPlaceholder="Search name, MRN or phone…"
          searchValue={search}
          onSearchChange={handleSearchChange}
          pageSize={PAGE_SIZE}
          serverPagination={{ page, totalPages, onPageChange: setPage }}
          emptyState={
            debouncedSearch ? (
              <EmptyState
                icon={Users}
                title="No matching patients"
                description="Names match from the start, and MRN or phone must match exactly."
              />
            ) : (
              <EmptyState
                icon={Users}
                title="No patients yet"
                description="Register your first patient to start building the registry."
                action={<RegisterPatientDialog trigger={registerButton} />}
              />
            )
          }
        />
      )}
    </div>
  )
}
