import type { PatientHospital } from '@/api/hospitals'
import { ok, okPage, type Routes } from '@/test/fakeApi'

const lower = (value: unknown) => String(value ?? '').toLowerCase()

/**
 * The discovery endpoints over a fixed set of hospitals, answering the way the
 * backend contract says they do, so a test can type a search and see what a
 * patient would:
 *
 * - `GET /hospitals` — `search` is a case-insensitive substring of the name,
 *   `city` a case-insensitive exact match; ordered by name then reference;
 *   paged by `page` / `page_size`, with the true total.
 * - `GET /hospital-cities` — the distinct cities of those hospitals, sorted.
 * - `GET /hospitals/{ref}` — one route per hospital; any other reference is
 *   left unrouted on purpose, so a test must say what an unknown one answers.
 */
export function hospitalDirectory(hospitals: PatientHospital[]): Routes {
  const ordered = [...hospitals].sort(
    (a, b) => lower(a.name).localeCompare(lower(b.name)) || a.ref.localeCompare(b.ref),
  )
  const cities = [...new Set(ordered.map((h) => h.address.city).filter((city): city is string => !!city))].sort()

  return {
    'GET /hospitals': (config) => {
      const { search, city, page = 1, page_size: pageSize = 20 } = (config.params ?? {}) as {
        search?: string
        city?: string
        page?: number
        page_size?: number
      }
      const matches = ordered.filter(
        (h) => lower(h.name).includes(lower(search)) && (!city || lower(h.address.city) === lower(city)),
      )
      return okPage(matches.slice((page - 1) * pageSize, page * pageSize), page, pageSize, matches.length)
    },
    'GET /hospital-cities': ok({ cities }),
    ...Object.fromEntries(ordered.map((h) => [`GET /hospitals/${h.ref}`, ok(h)])),
  }
}
