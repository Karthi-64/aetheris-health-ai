import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { ChevronLeft, ChevronRight, Search, SearchX, Stethoscope, X } from 'lucide-react'
import { useLocation, useNavigationType, useParams, useSearchParams } from 'react-router-dom'
import type { Paginated } from '@atheris/api-core'
import { Button, cn, EmptyState, Skeleton } from '@atheris/ui'
import { DOCTOR_SEARCH_MAX_LENGTH, useDepartments, useDoctors, type PatientDoctor } from '@/api/doctors'
import type { PatientHospital } from '@/api/hospitals'
import { BackLink } from '@/components/BackLink'
import { fieldControlClass } from '@/components/fieldStyles'
import { FormField } from '@/components/FormField'
import { PageHeading } from '@/components/PageHeading'
import { DoctorCard } from '@/pages/doctors/DoctorCard'
import { readDoctorFilters, withDoctorFilters, type DoctorFilters } from '@/pages/doctors/filters'
import { doctorStrings as S } from '@/pages/doctors/strings'
import { HospitalLoader } from '@/pages/hospitals/HospitalLoader'
import { loadFailureOf } from '@/pages/hospitals/loadFailure'
import { LoadProblem } from '@/pages/hospitals/LoadProblem'
import { hospitalPath } from '@/pages/hospitals/paths'

/** How long typing must pause before the search is run. Enter does not wait. */
export const DOCTOR_SEARCH_DEBOUNCE_MS = 350

/**
 * Carried in router state by every change this page makes to its own query
 * string, so that it can tell those from the URL changing under it.
 */
const OWN_CHANGE = { doctorFilters: true } as const

const isOwnChange = (state: unknown) => (state as { doctorFilters?: unknown } | null)?.doctorFilters === true

/** Where the patient is in the results, in the server's own numbers. */
function positionOf({ items, pagination }: Paginated<PatientDoctor>, isFiltered: boolean): string {
  const { page, pageSize, total } = pagination
  if (total === 0) return isFiltered ? S.noMatchStatus : S.noneStatus
  if (items.length === 0) return S.pastEndStatus
  const from = (page - 1) * pageSize + 1
  const to = from + items.length - 1
  return from === 1 && to === total ? S.count(total) : S.range(from, to, total)
}

/**
 * Where "View Doctors" leads: the doctors listed at one hospital. The hospital
 * is read first, like on any other hospital page, so one that is unknown or
 * unavailable is the same "not available" page and nothing is asked about its
 * doctors.
 */
export function HospitalDoctorsPage() {
  const { hospitalRef = '' } = useParams()
  return (
    // Keyed by the hospital, so moving to another one starts a fresh page.
    <HospitalLoader key={hospitalRef} hospitalRef={hospitalRef} title={() => S.title}>
      {(hospital) => <DoctorList hospital={hospital} />}
    </HospitalLoader>
  )
}

/**
 * Search by name or specialisation, filter by department, page through the
 * result. As on the hospital list, the query string is the state — what is
 * searched for is whatever the URL says — so back, forward and reload all come
 * back to the same list.
 */
function DoctorList({ hospital }: { hospital: PatientHospital }) {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const navigationType = useNavigationType()
  const { search, department, page } = readDoctorFilters(params)
  // The hospital is named by the reference the server gave, not by what was in the address bar.
  const doctors = useDoctors({ hospitalRef: hospital.ref, search, department, page })
  const departments = useDepartments(hospital.ref)

  const resultsId = useId()
  const searchField = useRef<HTMLInputElement>(null)
  const resultsHeading = useRef<HTMLHeadingElement>(null)

  // What is typed runs ahead of what is searched for. This page's own changes
  // to the URL leave it alone; when the URL changes under the field instead —
  // back, forward, a link followed — the field follows.
  const [draft, setDraft] = useState(search)
  const [seenKey, setSeenKey] = useState(location.key)
  if (location.key !== seenKey) {
    setSeenKey(location.key)
    if (navigationType === 'POP' || !isOwnChange(location.state)) setDraft(search)
  }

  useEffect(() => {
    const term = draft.trim()
    if (term === search) return
    const timer = setTimeout(() => {
      setParams((current) => withDoctorFilters(current, { search: term, page: 1 }), { replace: true, state: OWN_CHANGE })
    }, DOCTOR_SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [draft, search, setParams])

  // Refining the list replaces the history entry; turning a page adds one, so
  // "back" returns to the page before.
  const show = (changes: Partial<DoctorFilters>, how: 'replace' | 'push' = 'replace') =>
    setParams((current) => withDoctorFilters(current, changes), { replace: how === 'replace', state: OWN_CHANGE })

  const submitSearch = (event: FormEvent) => {
    event.preventDefault()
    const term = draft.trim()
    if (term !== search) show({ search: term, page: 1 })
  }

  const clearSearch = () => {
    setDraft('')
    show({ search: '', page: 1 })
    searchField.current?.focus()
  }

  const clearFilters = () => {
    setDraft('')
    show({ search: '', department: '', page: 1 })
    searchField.current?.focus()
  }

  // The control that was pressed is about to change or go: focus moves to the
  // top of the results, which is also where the new ones start.
  const goToPage = (next: number) => {
    show({ page: next }, 'push')
    resultsHeading.current?.focus()
  }

  const retry = () => {
    resultsHeading.current?.focus()
    void doctors.refetch()
  }

  // The filter is offered only when there is something to choose. A department
  // in the URL is shown as chosen even when the departments have not loaded,
  // failed, or do not have it — the filter in force is never hidden — but it
  // is shown by a neutral label: its reference is not for the screen.
  const listed = departments.data ?? []
  const chosen = listed.find((each) => each.ref.toLowerCase() === department.toLowerCase())
  const departmentValue = chosen?.ref ?? department
  const showDepartments = listed.length > 0 || department !== ''

  const isFiltered = search !== '' || department !== ''
  // A request held back because the browser is offline never fails: it waits.
  const isHeldOffline = doctors.isPending && doctors.fetchStatus === 'paused'
  const isLoading = doctors.isPending && !isHeldOffline
  const failure = isHeldOffline ? 'offline' : doctors.isError ? loadFailureOf(doctors.error) : null
  const result = !doctors.isPending && !doctors.isError ? doctors.data : null

  return (
    <div className="space-y-6">
      <BackLink to={hospitalPath(hospital.ref)}>
        {hospital.name ? S.backToHospital(hospital.name) : S.backToHospitalFallback}
      </BackLink>

      <div className="space-y-2">
        <PageHeading>{hospital.name ? S.heading(hospital.name) : S.headingFallback}</PageHeading>
        <p className="text-body-lg text-on-surface-variant">{S.listIntro}</p>
      </div>

      <form
        role="search"
        aria-label={S.searchFormLabel}
        onSubmit={submitSearch}
        className="bg-card space-y-4 rounded-2xl border p-5"
      >
        <FormField label={S.searchLabel}>
          {(field) => (
            <div className="relative">
              <Search
                className="text-outline pointer-events-none absolute top-1/2 left-3.5 size-5 -translate-y-1/2"
                aria-hidden
              />
              <input
                {...field}
                ref={searchField}
                type="search"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                maxLength={DOCTOR_SEARCH_MAX_LENGTH}
                enterKeyHint="search"
                autoComplete="off"
                autoCorrect="off"
                spellCheck={false}
                className={cn(fieldControlClass, 'pr-12 pl-11 [&::-webkit-search-cancel-button]:appearance-none')}
              />
              {draft !== '' && (
                <button
                  type="button"
                  onClick={clearSearch}
                  aria-label={S.clearSearch}
                  className="text-on-surface-variant hover:text-on-surface absolute top-0 right-0 flex size-11 items-center justify-center rounded-xl"
                >
                  <X className="size-5" aria-hidden />
                </button>
              )}
            </div>
          )}
        </FormField>

        {showDepartments && (
          <FormField label={S.departmentLabel}>
            {(field) => (
              <select
                {...field}
                value={departmentValue}
                onChange={(event) => show({ department: event.target.value, page: 1 })}
                className={fieldControlClass}
              >
                <option value="">{S.allDepartments}</option>
                {department !== '' && !chosen && <option value={department}>{S.selectedDepartment}</option>}
                {listed.map((each) => (
                  <option key={each.ref} value={each.ref}>
                    {each.name}
                  </option>
                ))}
              </select>
            )}
          </FormField>
        )}
      </form>

      <section aria-labelledby={resultsId} className="space-y-4">
        <div className="space-y-1">
          <h2
            id={resultsId}
            ref={resultsHeading}
            tabIndex={-1}
            className="font-display text-title-lg text-primary scroll-mt-24"
          >
            {S.resultsHeading}
          </h2>
          {/* Always here, so a change of count is announced, not a new element. */}
          <p role="status" className="text-body-sm text-on-surface-variant min-h-5">
            {isLoading ? S.loading : result ? positionOf(result, isFiltered) : ''}
          </p>
        </div>

        {isLoading && (
          <div aria-hidden className="space-y-3">
            <Skeleton className="h-36 w-full rounded-2xl" />
            <Skeleton className="h-36 w-full rounded-2xl" />
            <Skeleton className="h-36 w-full rounded-2xl" />
          </div>
        )}

        {failure && (
          <LoadProblem
            // Here the hospital is known: whatever went wrong, it is the list that did not load.
            failure={failure === 'not_found' ? 'error' : failure}
            failedMessage={S.listFailed}
            onRetry={retry}
            isRetrying={doctors.isFetching}
          />
        )}

        {result && result.pagination.total === 0 && !isFiltered && (
          <EmptyState icon={Stethoscope} title={S.noneTitle} description={S.noneBody} className="bg-card border py-10" />
        )}

        {result && result.pagination.total === 0 && isFiltered && (
          <EmptyState
            icon={SearchX}
            title={S.noMatchTitle}
            description={S.noMatchBody}
            className="bg-card border py-10"
            action={
              <Button variant="outline" size="touch" onClick={clearFilters}>
                {S.clearFilters}
              </Button>
            }
          />
        )}

        {/* A page past the end: the doctors exist, just not here. */}
        {result && result.pagination.total > 0 && result.items.length === 0 && (
          <EmptyState
            title={S.pastEndTitle}
            description={S.pastEndBody(result.pagination.total)}
            className="bg-card border py-10"
            action={
              <Button variant="outline" size="touch" onClick={() => goToPage(1)}>
                {S.firstPage}
              </Button>
            }
          />
        )}

        {result && result.items.length > 0 && (
          <ul aria-labelledby={resultsId} className="space-y-3">
            {result.items.map((doctor) => (
              <DoctorCard key={doctor.ref} hospitalRef={hospital.ref} doctor={doctor} />
            ))}
          </ul>
        )}

        {result && result.items.length > 0 && result.pagination.totalPages > 1 && (
          <nav aria-label={S.paginationLabel} className="space-y-3">
            <p className="text-body-sm text-on-surface-variant text-center">
              {S.pageOf(result.pagination.page, result.pagination.totalPages)}
            </p>
            <div className="grid grid-cols-2 gap-3">
              <Button
                variant="outline"
                size="touch"
                onClick={() => goToPage(result.pagination.page - 1)}
                disabled={result.pagination.page <= 1}
              >
                <ChevronLeft aria-hidden />
                {S.previous}
              </Button>
              <Button
                variant="outline"
                size="touch"
                onClick={() => goToPage(result.pagination.page + 1)}
                disabled={result.pagination.page >= result.pagination.totalPages}
              >
                {S.next}
                <ChevronRight aria-hidden />
              </Button>
            </div>
          </nav>
        )}
      </section>
    </div>
  )
}
