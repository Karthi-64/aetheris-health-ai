import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ChevronRight, Pencil, Pill, Plus, RotateCw } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useMedicines, type Medicine } from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { formatMoney } from '@/lib/format'
import { medicineDetail } from '@/components/pharmacy/pharmacyPresentation'
import { CreateMedicineDialog, EditMedicineDialog } from './MedicineDialog'
import { PharmacyTabs } from './PharmacyTabs'

const PAGE_SIZE = 25
const ALL = 'all'
/** The API's limit on `q`; a longer one is a 422, not a search that finds nothing. */
const MAX_QUERY = 200

/**
 * The medicine catalog (docs/18-API_CONTRACTS.md §9.3), ordered by name.
 *
 * Search is the API's: the start of a name or generic name, or a whole SKU. It
 * does not trim what it is given, so the term is trimmed here. With no status
 * chosen the API returns active and inactive medicines alike.
 *
 * No stock figure is shown: a medicine carries none and there is no endpoint
 * for the stock of many, so a count per row would be a request per row. Stock
 * is one click away, for those who hold `pharmacy.batch.read` — a doctor reads
 * the catalog but not stock. Adding and editing need `pharmacy.medicine.create`
 * and `pharmacy.medicine.update`.
 */
export default function MedicinesPage() {
  const { can } = usePermissions()
  const canCreate = can('pharmacy.medicine.create')
  const canEdit = can('pharmacy.medicine.update')
  const canReadStock = can('pharmacy.batch.read')
  const [search, setSearch] = useState('')
  // The term and the page it is read at change together: a new term starts at
  // page 1 in the same request, never as a page 1 of the old term first.
  const [{ q, page }, setQuery] = useState({ q: '', page: 1 })
  const [active, setActive] = useState<string>(ALL)
  const setPage = (next: number) => setQuery((current) => ({ ...current, page: next }))

  useEffect(() => {
    const id = setTimeout(() => {
      const term = search.trim().slice(0, MAX_QUERY)
      // Typing that leaves the term as it was — spaces — is not a new search
      // and keeps the page.
      setQuery((current) => (current.q === term ? current : { q: term, page: 1 }))
    }, 300)
    return () => clearTimeout(id)
  }, [search])

  const { data, isPending, isError, isPlaceholderData, refetch } = useMedicines({
    q: q || undefined,
    is_active: active === ALL ? undefined : active === 'active',
    page,
    page_size: PAGE_SIZE,
  })

  const medicines = data?.items ?? []
  const meta = data?.pagination
  const filtered = !!q || active !== ALL

  // The catalog can shrink under the page being read — a medicine retired
  // while "Active only" is on, say — and the API then answers a page past the
  // last one with no rows (§9.3). That is not "nothing matches": go to the
  // last page there is. A page kept on screen from the previous request says
  // nothing about this one, so it is not judged.
  const lastPage = Math.max(1, meta?.totalPages ?? 1)
  const pastLastPage = !!meta && !isPlaceholderData && page > lastPage
  if (pastLastPage) setPage(lastPage)

  const columns: ColumnDef<Medicine>[] = useMemo(
    () => [
      {
        accessorKey: 'name',
        header: 'Medicine',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex max-w-72 min-w-40 flex-col [overflow-wrap:anywhere]">
            <span className="text-on-surface font-semibold">{row.original.name}</span>
            {row.original.generic_name && (
              <span className="text-outline text-xs">{row.original.generic_name}</span>
            )}
          </div>
        ),
      },
      {
        accessorKey: 'sku',
        header: 'SKU',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant block max-w-40 font-mono text-xs [overflow-wrap:anywhere]">
            {row.original.sku}
          </span>
        ),
      },
      {
        id: 'detail',
        header: 'Strength / form',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant">{medicineDetail(row.original) ?? '—'}</span>
        ),
      },
      {
        accessorKey: 'unit_price',
        header: 'Price per unit',
        enableSorting: false,
        cell: ({ row }) => <span className="tabular-nums">{formatMoney(row.original.unit_price)}</span>,
      },
      {
        accessorKey: 'requires_prescription',
        header: 'Prescription',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant whitespace-nowrap">
            {row.original.requires_prescription ? 'Needed' : 'Not needed'}
          </span>
        ),
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
        cell: ({ row }) => (
          <div className="flex items-center justify-end gap-3">
            {canEdit && (
              // The table keeps a cell by its row's position. Keyed by the
              // medicine, an open form is closed if a refetch puts another
              // medicine in this row, rather than saving its values onto it.
              <EditMedicineDialog
                key={row.original.id}
                medicine={row.original}
                trigger={
                  <Button variant="ghost" size="sm" aria-label={`Edit ${row.original.name}`}>
                    <Pencil className="size-4" /> Edit
                  </Button>
                }
              />
            )}
            {canReadStock && (
              <Link
                to={`/pharmacy/medicines/${row.original.id}`}
                aria-label={`Stock for ${row.original.name}`}
                className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 whitespace-nowrap transition-colors"
              >
                Stock <ChevronRight className="size-4" />
              </Link>
            )}
          </div>
        ),
      },
    ],
    [canEdit, canReadStock],
  )

  const addButton = (
    <Button className="rounded-full">
      <Plus className="size-4" /> Add medicine
    </Button>
  )

  return (
    <div className="w-full">
      <PharmacyTabs />
      <PageHeader
        title="Medicines"
        subtitle={
          canReadStock
            ? 'The medicine catalog and its selling prices. Stock is held per batch — open a medicine to see it.'
            : 'The medicine catalog and its selling prices. Only active medicines can be prescribed.'
        }
        actions={
          <>
            <Select
              value={active}
              onValueChange={(v) => {
                setActive(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by status" className="w-48 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Active and inactive</SelectItem>
                <SelectItem value="active">Active only</SelectItem>
                <SelectItem value="inactive">Inactive only</SelectItem>
              </SelectContent>
            </Select>
            {canCreate && <CreateMedicineDialog trigger={addButton} />}
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load the medicine catalog">
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
          data={medicines}
          // Rows kept from the previous request while the next one loads are
          // worth showing; an empty result kept that way is not — it would
          // say "nothing matches" about a request that has not answered.
          isLoading={isPending || (isPlaceholderData && medicines.length === 0)}
          searchable
          searchPlaceholder="Search by start of name or whole SKU…"
          searchValue={search}
          onSearchChange={setSearch}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            filtered ? (
              <EmptyState
                icon={Pill}
                title="No matching medicines"
                description={
                  q
                    ? 'Search matches the start of a name or generic name, or a whole SKU — not a word in the middle, and not part of a SKU.'
                    : 'No medicine in the catalog has this status.'
                }
              />
            ) : (
              <EmptyState
                icon={Pill}
                title="No medicines in the catalog"
                description={
                  canCreate
                    ? 'Add the medicines your pharmacy stocks so they can be prescribed, ordered and dispensed.'
                    : 'Medicines added to the catalog will appear here.'
                }
                action={canCreate ? <CreateMedicineDialog trigger={addButton} /> : undefined}
              />
            )
          }
        />
      )}
    </div>
  )
}
