import { useEffect, useState } from 'react'
import { Search, X } from 'lucide-react'
import { usePatients, type PatientSummary } from '@/api/patients'
import { cn } from '@/lib/utils'

/** Searchable patient picker. Shows the chosen patient as a chip once selected. */
export function PatientPicker({
  value,
  onChange,
  invalid,
  id,
}: {
  value: string
  onChange: (id: string) => void
  invalid?: boolean
  id?: string
}) {
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [chosen, setChosen] = useState<PatientSummary | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setQ(search.trim()), 250)
    return () => clearTimeout(t)
  }, [search])

  const { data } = usePatients({ q: q || undefined, page: 1, page_size: 8 })
  const results = data?.items ?? []

  if (value && chosen) {
    return (
      <div className="neo-pressed bg-surface flex items-center justify-between rounded-xl px-4 py-2.5">
        <span className="font-body text-body-sm text-on-surface">
          {chosen.full_name} <span className="font-mono text-outline">· {chosen.mrn}</span>
        </span>
        <button
          type="button"
          onClick={() => {
            setChosen(null)
            onChange('')
          }}
          aria-label="Change patient"
          className="text-outline hover:text-error transition-colors"
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
          placeholder="Search name, MRN, phone…"
          aria-invalid={invalid}
          className={cn(
            'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant w-full rounded-xl py-2.5 pr-4 pl-9 outline-none focus-visible:ring-2 focus-visible:ring-secondary',
            invalid && 'ring-2 ring-error',
          )}
        />
      </div>
      {q && results.length > 0 && (
        <ul className="neo-extruded bg-surface max-h-44 overflow-y-auto rounded-xl p-1">
          {results.map((p) => (
            <li key={p.id}>
              <button
                type="button"
                onClick={() => {
                  setChosen(p)
                  onChange(p.id)
                }}
                className="hover:bg-secondary/10 flex w-full items-center justify-between rounded-lg px-3 py-2 text-left transition-colors"
              >
                <span className="font-body text-body-sm text-on-surface">{p.full_name}</span>
                <span className="font-mono text-outline text-xs">{p.mrn}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {q && results.length === 0 && (
        <p className="font-body text-outline text-xs">
          No patients match “{q}”. Try the first or last name on its own, or the full MRN or phone
          number.
        </p>
      )}
    </div>
  )
}
