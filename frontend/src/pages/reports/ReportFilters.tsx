import { X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useDepartments } from '@/api/departments'
import { useDoctors } from '@/api/doctors'
import type { Granularity, NamedRef } from '@/api/reports'
import { usePermissions } from '@/hooks/usePermissions'
import {
  GRANULARITIES,
  GRANULARITY_LABELS,
  PERIOD_PRESETS,
  isGranularity,
  presetRange,
} from '@/lib/reportPeriod'
import type { ReportFilters as ReportFiltersState } from './useReportFilters'

/** The select value standing for "no filter"; Radix does not allow an empty one. */
const ALL = '__all__'

interface Option {
  id: string
  name: string
}

/**
 * The list a filter chooses from, plus the filter's own subject when the list
 * lacks it. The server accepts any doctor or department of the hospital —
 * deactivated ones too — and names the one it filtered on, so an id that
 * arrived in the address always shows a name.
 */
function withEchoed(listed: Option[], value: string | undefined, echoed: NamedRef | null | undefined): Option[] {
  if (!value || !echoed || echoed.id !== value || listed.some((option) => option.id === value)) return listed
  return [{ id: echoed.id, name: echoed.name }, ...listed]
}

function OptionSelect({
  id,
  describedBy,
  value,
  options,
  allLabel,
  onChange,
}: {
  id: string
  describedBy: string | undefined
  value: string | undefined
  options: Option[]
  allLabel: string
  onChange: (id: string | undefined) => void
}) {
  return (
    <Select value={value ?? ALL} onValueChange={(next) => onChange(next === ALL ? undefined : next)}>
      <SelectTrigger id={id} aria-describedby={describedBy}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>{allLabel}</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.id} value={option.id}>
            {option.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/**
 * A filter the user cannot choose from a list (the list needs a permission
 * they lack) but which is applied all the same, because it is in the address.
 * It is shown, and can be removed, so no figure is narrowed out of sight.
 */
function AppliedFilter({
  label,
  name,
  onClear,
}: {
  label: string
  name: string
  onClear: () => void
}) {
  return (
    <Field label={label}>
      {(p) => (
        <div
          id={p.id}
          role="group"
          aria-label={`${label} filter: ${name}`}
          className="neo-pressed bg-surface flex items-center justify-between gap-3 rounded-xl px-4 py-2.5"
        >
          <span className="font-body text-body-sm text-on-surface min-w-0 [overflow-wrap:anywhere]">{name}</span>
          <button
            type="button"
            onClick={onClear}
            aria-label={`Clear the ${label.toLowerCase()} filter`}
            className="text-outline hover:text-error focus-visible:ring-secondary shrink-0 rounded transition-colors outline-none focus-visible:ring-2"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>
      )}
    </Field>
  )
}

/** Mounted only for `department.read` holders: the list is requested on mount. */
function DepartmentSelect({
  value,
  echoed,
  onChange,
}: {
  value: string | undefined
  echoed: NamedRef | null | undefined
  onChange: (id: string | undefined) => void
}) {
  const { data, isError } = useDepartments()
  const options = withEchoed(
    (data ?? []).map((department) => ({ id: department.id, name: department.name })),
    value,
    echoed,
  )
  return (
    <Field label="Department" hint={isError ? "Couldn't load the department list." : undefined}>
      {(p) => (
        <OptionSelect
          id={p.id}
          describedBy={p['aria-describedby']}
          value={value}
          options={options}
          allLabel="All departments"
          onChange={onChange}
        />
      )}
    </Field>
  )
}

/** Mounted only for `doctor.read` holders: the list is requested on mount. */
function DoctorSelect({
  value,
  departmentId,
  echoed,
  onChange,
}: {
  value: string | undefined
  /** Narrows the list to one department's doctors. */
  departmentId: string | undefined
  echoed: NamedRef | null | undefined
  onChange: (id: string | undefined) => void
}) {
  // Deactivated doctors are listed too: their past appointments are in the report.
  const { data, isError } = useDoctors({ department: departmentId, include_inactive: true, page_size: 100 })
  const options = withEchoed(
    (data?.items ?? []).map((doctor) => ({
      id: doctor.id,
      name: doctor.status === 'inactive' ? `${doctor.full_name} (inactive)` : doctor.full_name,
    })),
    value,
    echoed,
  )
  return (
    <Field label="Doctor" hint={isError ? "Couldn't load the doctor list." : undefined}>
      {(p) => (
        <OptionSelect
          id={p.id}
          describedBy={p['aria-describedby']}
          value={value}
          options={options}
          allLabel="All doctors"
          onChange={onChange}
        />
      )}
    </Field>
  )
}

/**
 * The period of a report, and on Appointments the doctor and department.
 *
 * The inputs show what is in the address; where the address says nothing they
 * show what the server used (`resolved`, the report's own `filters`), and
 * editing one date then fixes the other at the value on screen. The
 * presets are counted from the hospital's day (`today`, the report's
 * `meta.today`), never the browser's, so they wait for the first answer.
 */
export function ReportFilters({
  filters,
  resolved,
  today,
  doctor,
  department,
}: {
  filters: ReportFiltersState
  /** The period the server resolved for the report on screen. */
  resolved?: { from: string; to: string; granularity: Granularity }
  /** `meta.today`: the hospital's day. Undefined until a report has answered. */
  today?: string
  /** Present on a report that can be narrowed to a doctor; `echoed` is the report's `filters.doctor`. */
  doctor?: { echoed: NamedRef | null | undefined }
  department?: { echoed: NamedRef | null | undefined }
}) {
  const { can } = usePermissions()
  const from = filters.from ?? resolved?.from ?? ''
  const to = filters.to ?? resolved?.to ?? ''
  const granularity = filters.granularity ?? resolved?.granularity

  // The inputs show a pair of dates. When one is edited while the other is only
  // the server's default, both go into the address: otherwise the server would
  // resolve the missing end afresh from the edited one, and the date the user
  // left alone would move. Emptying an input still just removes that filter.
  const changeDate = (key: 'from' | 'to', value: string) => {
    if (!value) return filters.update({ [key]: undefined })
    const other = key === 'from' ? 'to' : 'from'
    const shown = filters[other] ?? resolved?.[other]
    if (!shown) return filters.update({ [key]: value })
    // `from` first, whichever was edited, so one period has one address.
    filters.update(key === 'from' ? { from: value, to: shown } : { from: shown, to: value })
  }

  return (
    <section aria-label="Report filters" className="neo-extruded bg-surface mb-6 space-y-4 rounded-2xl p-5">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
        <Field label="From" className="min-w-0">
          {(p) => (
            <Input
              {...p}
              type="date"
              value={from}
              onChange={(e) => changeDate('from', e.target.value)}
            />
          )}
        </Field>
        {/* The server reports both period rules against `to`. */}
        <Field label="To" error={filters.error ?? undefined} className="min-w-0">
          {(p) => (
            <Input
              {...p}
              type="date"
              value={to}
              onChange={(e) => changeDate('to', e.target.value)}
            />
          )}
        </Field>
        <Field label="Group by" className="min-w-0">
          {(p) => (
            <Select
              value={granularity ?? ''}
              onValueChange={(next) => {
                if (isGranularity(next)) filters.update({ granularity: next })
              }}
            >
              <SelectTrigger id={p.id} aria-describedby={p['aria-describedby']}>
                <SelectValue placeholder="Choose…" />
              </SelectTrigger>
              <SelectContent>
                {GRANULARITIES.map((g) => (
                  <SelectItem key={g} value={g}>
                    {GRANULARITY_LABELS[g]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </Field>
        {department &&
          (can('department.read') ? (
            <DepartmentSelect
              value={filters.departmentId}
              echoed={department.echoed}
              onChange={(id) => filters.update({ department_id: id })}
            />
          ) : (
            filters.departmentId && (
              <AppliedFilter
                label="Department"
                name={department.echoed?.name ?? 'One department'}
                onClear={() => filters.update({ department_id: undefined })}
              />
            )
          ))}
        {doctor &&
          (can('doctor.read') ? (
            <DoctorSelect
              value={filters.doctorId}
              departmentId={filters.departmentId}
              echoed={doctor.echoed}
              onChange={(id) => filters.update({ doctor_id: id })}
            />
          ) : (
            filters.doctorId && (
              <AppliedFilter
                label="Doctor"
                name={doctor.echoed?.name ?? 'One doctor'}
                onClear={() => filters.update({ doctor_id: undefined })}
              />
            )
          ))}
      </div>

      <div role="group" aria-label="Period presets" className="flex flex-wrap gap-2">
        {PERIOD_PRESETS.map((preset) => {
          const range = today ? presetRange(preset.id, today) : undefined
          return (
            <Button
              key={preset.id}
              type="button"
              variant="outline"
              size="sm"
              disabled={!range}
              aria-pressed={!!range && range.from === from && range.to === to}
              className="aria-pressed:border-secondary aria-pressed:text-secondary"
              onClick={() => range && filters.update(range)}
            >
              {preset.label}
            </Button>
          )
        })}
      </div>
    </section>
  )
}
