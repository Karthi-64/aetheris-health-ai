import type { PatientDepartment, PatientDoctor } from '@/api/doctors'
import { availabilityRoute, type FakeSchedule } from '@/test/availability'
import { ok, okPage, type Routes } from '@/test/fakeApi'

const lower = (value: unknown) => String(value ?? '').toLowerCase()

/**
 * The doctor endpoints of one hospital over a fixed set of doctors, answering
 * the way the backend contract says they do, so a test can type a search and
 * see what a patient would:
 *
 * - `GET /hospitals/{ref}/doctors` — `search` is a case-insensitive substring
 *   of the name or the specialisation, `department` a department's reference;
 *   ordered by name; paged by `page` / `page_size`, with the true total.
 * - `GET /hospitals/{ref}/departments` — the departments those doctors are in,
 *   sorted by name. `described` supplies the descriptions.
 * - `GET /hospitals/{ref}/doctors/{doctor}` — one route per doctor; any other
 *   reference is left unrouted on purpose, so a test must say what an unknown
 *   one answers.
 * - `GET /hospitals/{ref}/doctors/{doctor}/availability` — one route per
 *   doctor, over `schedule` (see `src/test/availability.ts`).
 */
export function doctorDirectory(
  hospitalRef: string,
  doctors: PatientDoctor[],
  described: PatientDepartment[] = [],
  schedule: Partial<FakeSchedule> = {},
): Routes {
  const base = `GET /hospitals/${hospitalRef}`
  const ordered = [...doctors].sort((a, b) => lower(a.name).localeCompare(lower(b.name)) || a.ref.localeCompare(b.ref))
  const departments = [...new Map(ordered.flatMap((d) => (d.department ? [[d.department.ref, d.department]] : []))).values()]
    .map(({ ref, name }) => ({ ref, name, description: described.find((each) => each.ref === ref)?.description ?? null }))
    .sort((a, b) => lower(a.name).localeCompare(lower(b.name)))

  return {
    [`${base}/doctors`]: (config) => {
      const { search, department, page = 1, page_size: pageSize = 20 } = (config.params ?? {}) as {
        search?: string
        department?: string
        page?: number
        page_size?: number
      }
      const matches = ordered.filter(
        (d) =>
          (lower(d.name).includes(lower(search)) || lower(d.specialization).includes(lower(search))) &&
          (!department || d.department?.ref === department),
      )
      return okPage(matches.slice((page - 1) * pageSize, page * pageSize), page, pageSize, matches.length)
    },
    [`${base}/departments`]: ok({ departments }),
    ...Object.fromEntries(ordered.map((d) => [`${base}/doctors/${d.ref}`, ok(d)])),
    ...Object.assign({}, ...ordered.map((d) => availabilityRoute(hospitalRef, d.ref, schedule))),
  }
}
