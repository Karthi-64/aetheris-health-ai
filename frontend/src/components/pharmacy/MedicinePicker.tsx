import { useEffect, useState } from 'react'
import { Search, X } from 'lucide-react'
import { useMedicines, type Medicine } from '@/api/pharmacy'
import { cn } from '@/lib/utils'
import { medicineDetail } from './pharmacyPresentation'

/** How many matches to offer at a time. The search narrows them. */
const MATCHES = 8

/**
 * Searchable picker over the active medicine catalog (`GET /medicines`, which
 * needs `pharmacy.medicine.read`). Shows the chosen medicine as a chip.
 *
 * Search is the API's: the start of a name or generic name, or a whole SKU.
 * `value` is the chosen medicine's id; the whole record is handed to
 * `onChange` so a caller can read its name or price without a second request.
 */
export function MedicinePicker({
  value,
  onChange,
  invalid,
  id,
  excludeIds = [],
}: {
  value: string
  onChange: (medicine: Medicine | null) => void
  invalid?: boolean
  id?: string
  /** Medicines already chosen on another line. Listed, but cannot be picked twice. */
  excludeIds?: readonly string[]
}) {
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [chosen, setChosen] = useState<Medicine | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setQ(search.trim()), 250)
    return () => clearTimeout(t)
  }, [search])

  const picking = !(value && chosen)
  const { data, isError } = useMedicines(
    { q: q || undefined, is_active: true, page: 1, page_size: MATCHES },
    { enabled: picking },
  )
  const results = data?.items ?? []

  if (value && chosen) {
    const detail = medicineDetail(chosen)
    return (
      <div className="neo-pressed bg-surface flex items-center justify-between gap-3 rounded-xl px-4 py-2.5">
        <span className="font-body text-body-sm text-on-surface min-w-0">
          {chosen.name}
          {detail && <span className="text-on-surface-variant"> · {detail}</span>}{' '}
          <span className="text-outline font-mono text-xs">{chosen.sku}</span>
        </span>
        <button
          type="button"
          onClick={() => {
            setChosen(null)
            onChange(null)
          }}
          aria-label={`Change medicine (${chosen.name})`}
          className="text-outline hover:text-error focus-visible:ring-secondary shrink-0 rounded transition-colors outline-none focus-visible:ring-2"
        >
          <X className="size-4" />
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="text-outline pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
        <input
          id={id}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by name or exact SKU…"
          autoComplete="off"
          aria-invalid={invalid}
          className={cn(
            'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant focus-visible:ring-secondary w-full rounded-xl py-2.5 pr-4 pl-9 outline-none focus-visible:ring-2',
            invalid && 'ring-error ring-2',
          )}
        />
      </div>
      {isError ? (
        <p className="font-body text-error text-xs" role="alert">
          The medicine catalog couldn't be loaded.
        </p>
      ) : results.length > 0 ? (
        <ul className="neo-extruded bg-surface max-h-44 overflow-y-auto rounded-xl p-1">
          {results.map((medicine) => {
            const taken = excludeIds.includes(medicine.id)
            const detail = medicineDetail(medicine)
            return (
              <li key={medicine.id}>
                <button
                  type="button"
                  disabled={taken}
                  title={taken ? 'Already on this list' : undefined}
                  onClick={() => {
                    setChosen(medicine)
                    onChange(medicine)
                  }}
                  className="hover:bg-secondary/10 focus-visible:ring-secondary flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left transition-colors outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
                >
                  <span className="font-body text-body-sm text-on-surface min-w-0">
                    {medicine.name}
                    {detail && <span className="text-on-surface-variant"> · {detail}</span>}
                    {taken && <span className="text-outline"> — already added</span>}
                  </span>
                  <span className="text-outline shrink-0 font-mono text-xs">{medicine.sku}</span>
                </button>
              </li>
            )
          })}
        </ul>
      ) : data && q ? (
        <p className="font-body text-outline text-xs">
          No active medicine matches “{q}”. Search matches the start of a name or generic name, or
          a whole SKU.
        </p>
      ) : data ? (
        <p className="font-body text-outline text-xs">There are no active medicines in the catalog.</p>
      ) : null}
    </div>
  )
}
