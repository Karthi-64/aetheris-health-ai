import { type ReactNode, useState } from 'react'
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { ChevronDown, ChevronsUpDown, ChevronUp, Search } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

/**
 * Page state for a table whose rows the server already filtered and paged.
 * Supplying it hands both concerns to the caller: the table renders the
 * controls and reports intent, but does no filtering or slicing of its own.
 */
interface ServerPagination {
  page: number
  totalPages: number
  onPageChange: (page: number) => void
}

interface DataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[]
  data: TData[]
  /** Enable the global search box in the toolbar. */
  searchable?: boolean
  searchPlaceholder?: string
  /**
   * Controlled search term. Pass this (with `onSearchChange`) when the server
   * does the searching — the rows arrive already filtered, so filtering them
   * again client-side would hide matches that are on another page.
   */
  searchValue?: string
  onSearchChange?: (value: string) => void
  /** Server-side page state. Omit for client-side pagination over all rows. */
  serverPagination?: ServerPagination
  /** Shown while data loads — renders skeleton rows instead of a spinner. */
  isLoading?: boolean
  /** Rendered when there are no rows (and not loading). */
  emptyState?: ReactNode
  /** Slot on the right of the toolbar, e.g. a "Register" button. */
  toolbarRight?: ReactNode
  pageSize?: number
  className?: string
}

/**
 * The single tabular surface for the app (frontend/CLAUDE.md: every table uses
 * TanStack Table). Client-side sort · global search · pagination, Clinical Glass
 * styling, with loading and empty states built in.
 */
export function DataTable<TData, TValue>({
  columns,
  data,
  searchable = false,
  searchPlaceholder = 'Search…',
  searchValue,
  onSearchChange,
  serverPagination,
  isLoading = false,
  emptyState,
  toolbarRight,
  pageSize = 10,
  className,
}: DataTableProps<TData, TValue>) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [globalFilter, setGlobalFilter] = useState('')

  const searchIsControlled = onSearchChange !== undefined
  const searchTerm = searchIsControlled ? (searchValue ?? '') : globalFilter

  // eslint-disable-next-line react-hooks/incompatible-library -- TanStack Table manages its own memoization; the compiler lint is a known false positive here.
  const table = useReactTable({
    data,
    columns,
    // A controlled search means the rows are already filtered upstream, so the
    // table's own filter stays empty rather than filtering the page again.
    state: { sorting, globalFilter: searchIsControlled ? '' : globalFilter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
    // Same reasoning for pages: the server sent exactly one page, so slicing it
    // again would show a fraction of it.
    ...(serverPagination ? { manualPagination: true, pageCount: serverPagination.totalPages } : {}),
  })

  const currentPage = serverPagination ? serverPagination.page : table.getState().pagination.pageIndex + 1
  const totalPages = serverPagination ? serverPagination.totalPages : table.getPageCount()
  const canPrevious = serverPagination ? currentPage > 1 : table.getCanPreviousPage()
  const canNext = serverPagination ? currentPage < totalPages : table.getCanNextPage()

  const goToPrevious = () =>
    serverPagination ? serverPagination.onPageChange(currentPage - 1) : table.previousPage()
  const goToNext = () =>
    serverPagination ? serverPagination.onPageChange(currentPage + 1) : table.nextPage()

  const rows = table.getRowModel().rows
  const showEmpty = !isLoading && rows.length === 0

  return (
    <div className={cn('space-y-4', className)}>
      {(searchable || toolbarRight) && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          {searchable ? (
            <div className="relative w-full max-w-xs">
              <Search className="text-outline pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
              <input
                value={searchTerm}
                onChange={(e) =>
                  searchIsControlled ? onSearchChange(e.target.value) : setGlobalFilter(e.target.value)
                }
                placeholder={searchPlaceholder}
                aria-label={searchPlaceholder}
                className="neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant w-full rounded-xl py-2.5 pr-4 pl-9 outline-none focus-visible:ring-2 focus-visible:ring-secondary"
              />
            </div>
          ) : (
            <span />
          )}
          {toolbarRight}
        </div>
      )}

      <div className="neo-extruded bg-surface overflow-hidden rounded-2xl">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-left">
            <thead>
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id} className="border-outline-variant/40 border-b">
                  {headerGroup.headers.map((header) => {
                    const canSort = header.column.getCanSort()
                    const sorted = header.column.getIsSorted()
                    return (
                      <th
                        key={header.id}
                        className="font-label text-label-caps text-on-surface-variant px-4 py-3 whitespace-nowrap"
                      >
                        {header.isPlaceholder ? null : canSort ? (
                          <button
                            type="button"
                            onClick={header.column.getToggleSortingHandler()}
                            className="hover:text-primary inline-flex items-center gap-1.5 transition-colors"
                          >
                            {flexRender(header.column.columnDef.header, header.getContext())}
                            {sorted === 'asc' ? (
                              <ChevronUp className="size-3.5" />
                            ) : sorted === 'desc' ? (
                              <ChevronDown className="size-3.5" />
                            ) : (
                              <ChevronsUpDown className="text-outline-variant size-3.5" />
                            )}
                          </button>
                        ) : (
                          flexRender(header.column.columnDef.header, header.getContext())
                        )}
                      </th>
                    )
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {isLoading
                ? Array.from({ length: Math.min(pageSize, 6) }).map((_, i) => (
                    <tr key={`sk-${i}`} className="border-outline-variant/20 border-b last:border-0">
                      {columns.map((_col, ci) => (
                        <td key={ci} className="px-4 py-3.5">
                          <Skeleton className="h-4 w-full max-w-[8rem]" />
                        </td>
                      ))}
                    </tr>
                  ))
                : rows.map((row) => (
                    <tr
                      key={row.id}
                      className="border-outline-variant/20 hover:bg-surface-container-low/60 border-b transition-colors last:border-0"
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td
                          key={cell.id}
                          className="font-body text-body-sm text-on-surface px-4 py-3.5"
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  ))}
            </tbody>
          </table>
        </div>

        {showEmpty && <div className="px-4">{emptyState}</div>}
      </div>

      {!showEmpty && totalPages > 1 && (
        <div className="flex items-center justify-between gap-4">
          <p className="font-body text-body-sm text-on-surface-variant">
            Page {currentPage} of {totalPages}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={goToPrevious}
              disabled={!canPrevious}
              className="neo-extruded bg-surface font-label text-label-caps text-on-surface-variant rounded-xl px-4 py-2 transition-transform active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Previous
            </button>
            <button
              type="button"
              onClick={goToNext}
              disabled={!canNext}
              className="neo-extruded bg-surface font-label text-label-caps text-on-surface-variant rounded-xl px-4 py-2 transition-transform active:scale-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
