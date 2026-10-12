import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ArrowLeft, BookOpenText, Pencil, Plus, RotateCw } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useLabTests, type LabTest } from '@/api/lab'
import { usePermissions } from '@/hooks/usePermissions'
import { formatMoney } from '@/lib/format'
import { CreateLabTestDialog, EditLabTestDialog } from './LabTestDialog'

const PAGE_SIZE = 25
const ALL = 'all'

/** "2 bands" for a numeric test, or what a text test does instead. */
function rangeSummary(test: LabTest): string {
  if (test.result_type === 'text') return 'Text result'
  const n = test.reference_ranges.length
  return `${n} ${n === 1 ? 'range' : 'ranges'}`
}

/**
 * The lab test catalog (docs/18-API_CONTRACTS.md §8.3), ordered by name.
 *
 * Search is the API's: the start of a name, or a whole code. Category is an
 * exact match, so it is offered as a list of the categories in use. Adding and
 * editing are shown only to holders of `lab.test.create` / `lab.test.update`.
 */
export default function LabCatalogPage() {
  const { can } = usePermissions()
  const canEdit = can('lab.test.update')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [category, setCategory] = useState<string>(ALL)
  const [active, setActive] = useState<string>(ALL)
  const [page, setPage] = useState(1)

  useEffect(() => {
    const id = setTimeout(() => setQ(search.trim()), 300)
    return () => clearTimeout(id)
  }, [search])

  const { data, isPending, isError, refetch } = useLabTests({
    q: q || undefined,
    category: category === ALL ? undefined : category,
    is_active: active === ALL ? undefined : active === 'active',
    page,
    page_size: PAGE_SIZE,
  })
  // The categories in use, for the filter. The catalog is small; one page of
  // 100 covers it, and a hospital with more still gets the first hundred's.
  const everything = useLabTests({ page_size: 100 })
  const categories = useMemo(
    () =>
      [...new Set((everything.data?.items ?? []).map((t) => t.category).filter((c): c is string => !!c))].sort(),
    [everything.data],
  )

  const tests = data?.items ?? []
  const meta = data?.pagination
  const filtered = !!q || category !== ALL || active !== ALL

  const columns: ColumnDef<LabTest>[] = useMemo(
    () => [
      {
        accessorKey: 'name',
        header: 'Test',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex min-w-40 flex-col">
            <span className="text-on-surface font-semibold">{row.original.name}</span>
            <span className="text-outline font-mono text-xs">{row.original.code}</span>
          </div>
        ),
      },
      {
        accessorKey: 'category',
        header: 'Category',
        enableSorting: false,
        cell: ({ row }) => <span className="text-on-surface-variant">{row.original.category ?? '—'}</span>,
      },
      {
        accessorKey: 'unit',
        header: 'Unit',
        enableSorting: false,
        cell: ({ row }) => <span className="text-on-surface-variant">{row.original.unit ?? '—'}</span>,
      },
      {
        id: 'ranges',
        header: 'Reference',
        enableSorting: false,
        cell: ({ row }) => <span className="text-on-surface-variant">{rangeSummary(row.original)}</span>,
      },
      {
        accessorKey: 'price',
        header: 'Price',
        enableSorting: false,
        cell: ({ row }) => <span className="tabular-nums">{formatMoney(row.original.price)}</span>,
      },
      {
        accessorKey: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_active ? <Badge variant="success">Active</Badge> : <Badge variant="neutral">Inactive</Badge>,
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        cell: ({ row }) =>
          canEdit ? (
            <div className="flex justify-end">
              <EditLabTestDialog
                test={row.original}
                trigger={
                  <Button variant="ghost" size="sm" aria-label={`Edit ${row.original.name}`}>
                    <Pencil className="size-4" /> Edit
                  </Button>
                }
              />
            </div>
          ) : null,
      },
    ],
    [canEdit],
  )

  const addButton = (
    <Button className="rounded-full">
      <Plus className="size-4" /> Add test
    </Button>
  )

  return (
    <div className="w-full">
      <Link
        to="/laboratory"
        className="text-outline hover:text-secondary font-body text-body-sm mb-4 inline-flex items-center gap-1.5 transition-colors"
      >
        <ArrowLeft className="size-4" /> Back to laboratory
      </Link>
      <PageHeader
        title="Test catalog"
        subtitle="The tests that can be ordered, with their prices and reference ranges."
        actions={
          <>
            <Select
              value={category}
              onValueChange={(v) => {
                setCategory(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by category" className="w-48 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All categories</SelectItem>
                {categories.map((c) => (
                  <SelectItem key={c} value={c}>
                    {c}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={active}
              onValueChange={(v) => {
                setActive(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by status" className="w-40 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Active and inactive</SelectItem>
                <SelectItem value="active">Active only</SelectItem>
                <SelectItem value="inactive">Inactive only</SelectItem>
              </SelectContent>
            </Select>
            {can('lab.test.create') && <CreateLabTestDialog trigger={addButton} />}
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load the test catalog">
          <div className="flex flex-col items-start gap-3">
            <p>The catalog could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={columns}
          data={tests}
          isLoading={isPending}
          searchable
          searchPlaceholder="Search by name or exact code…"
          searchValue={search}
          onSearchChange={(value) => {
            setSearch(value)
            setPage(1)
          }}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            filtered ? (
              <EmptyState
                icon={BookOpenText}
                title="No matching tests"
                description="Search matches the start of a test's name, or its whole code."
              />
            ) : (
              <EmptyState
                icon={BookOpenText}
                title="No tests in the catalog"
                description={
                  can('lab.test.create')
                    ? 'Add the tests your laboratory runs so doctors can order them.'
                    : 'Tests added to the catalog will appear here.'
                }
                action={can('lab.test.create') ? <CreateLabTestDialog trigger={addButton} /> : undefined}
              />
            )
          }
        />
      )}
    </div>
  )
}
