import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { Building2, ChevronLeft, ChevronRight, Search, SearchX, X } from 'lucide-react'
import { useLocation, useNavigationType, useSearchParams } from 'react-router-dom'
import type { Paginated } from '@atheris/api-core'
import { Button, cn, EmptyState, Skeleton } from '@atheris/ui'
import {
  HOSPITAL_FILTER_MAX_LENGTH,
  useHospitalCities,
  useHospitals,
  type PatientHospital,
} from '@/api/hospitals'
import { fieldControlClass } from '@/components/fieldStyles'
import { FormField } from '@/components/FormField'
import { PageHeading } from '@/components/PageHeading'
import { usePageTitle } from '@/lib/usePageTitle'
import { readFilters, withFilters, type HospitalFilters } from '@/pages/hospitals/filters'
import { HospitalCard } from '@/pages/hospitals/HospitalCard'
import { loadFailureOf } from '@/pages/hospitals/loadFailure'
import { LoadProblem } from '@/pages/hospitals/LoadProblem'
import { hospitalStrings as S } from '@/pages/hospitals/strings'

/** How long typing must pause before the search is run. Enter does not wait. */
export const SEARCH_DEBOUNCE_MS = 350

/**
 * Carried in router state by every change this page makes to its own query
 * string, so that it can tell those from the URL changing under it.
 */
const OWN_CHANGE = { hospitalFilters: true } as const

const isOwnChange = (state: unknown) => (state as { hospitalFilters?: unknown } | null)?.hospitalFilters === true

/** Where the patient is in the results, in the server's own numbers. */
function positionOf({ items, pagination }: Paginated<PatientHospital>, isFiltered: boolean): string {
  const { page, pageSize, total } = pagination
  if (total === 0) return isFiltered ? S.noMatchStatus : S.noneStatus
  if (items.length === 0) return S.pastEndStatus
  const from = (page - 1) * pageSize + 1
  const to = from + items.length - 1
  return from === 1 && to === total ? S.count(total) : S.range(from, to, total)
}

/**
 * Hospital discovery: search by name, filter by city, page through the result.
 * The query string is the state — what is searched for is whatever the URL
 * says — so back, forward and reload all come back to the same list.
 */
export function HospitalsPage() {
  usePageTitle(S.listTitle)
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const navigationType = useNavigationType()
  const { search, city, page } = readFilters(params)
  const hospitals = useHospitals({ search, city, page })
  const cities = useHospitalCities()

  const resultsId = useId()
  const searchField = useRef<HTMLInputElement>(null)
  const resultsHeading = useRef<HTMLHeadingElement>(null)

  // What is typed runs ahead of what is searched for. This page's own changes
  // to the URL leave it alone; when the URL changes under the field instead —
  // back, forward, the Hospitals link in the navigation — the field follows.
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
      setParams((current) => withFilters(current, { search: term, page: 1 }), { replace: true, state: OWN_CHANGE })
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [draft, search, setParams])

  // Refining the list replaces the history entry; turning a page adds one, so
  // "back" returns to the page before.
  const show = (changes: Partial<HospitalFilters>, how: 'replace' | 'push' = 'replace') =>
    setParams((current) => withFilters(current, changes), { replace: how === 'replace', state: OWN_CHANGE })

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
    show({ search: '', city: '', page: 1 })
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
    void hospitals.refetch()
  }

  // The city in the URL is shown as chosen even when the list of cities has
  // not loaded, failed, or no longer has it: the filter in force is never hidden.
  const cityNames = cities.data ?? []
  const chosenCity = cityNames.find((name) => name.toLowerCase() === city.toLowerCase()) ?? city
  const cityOptions = chosenCity === '' || cityNames.includes(chosenCity) ? cityNames : [chosenCity, ...cityNames]

  const isFiltered = search !== '' || city !== ''
  // A request held back because the browser is offline never fails: it waits.
  const isHeldOffline = hospitals.isPending && hospitals.fetchStatus === 'paused'
  const isLoading = hospitals.isPending && !isHeldOffline
  const failure = isHeldOffline ? 'offline' : hospitals.isError ? loadFailureOf(hospitals.error) : null
  const result = !hospitals.isPending && !hospitals.isError ? hospitals.data : null

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <PageHeading>{S.listHeading}</PageHeading>
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
                maxLength={HOSPITAL_FILTER_MAX_LENGTH}
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

        <FormField label={S.cityLabel}>
          {(field) => (
            <select
              {...field}
              value={chosenCity}
              onChange={(event) => show({ city: event.target.value, page: 1 })}
              className={fieldControlClass}
            >
              <option value="">{S.allCities}</option>
              {cityOptions.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          )}
        </FormField>
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
            <Skeleton className="h-24 w-full rounded-2xl" />
            <Skeleton className="h-24 w-full rounded-2xl" />
            <Skeleton className="h-24 w-full rounded-2xl" />
          </div>
        )}

        {failure && (
          <LoadProblem
            failure={failure}
            failedMessage={S.listFailed}
            onRetry={retry}
            isRetrying={hospitals.isFetching}
          />
        )}

        {result && result.pagination.total === 0 && !isFiltered && (
          <EmptyState icon={Building2} title={S.noneTitle} description={S.noneBody} className="bg-card border py-10" />
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

        {/* A page past the end: the hospitals exist, just not here. */}
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
            {result.items.map((hospital) => (
              <HospitalCard key={hospital.ref} hospital={hospital} />
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
