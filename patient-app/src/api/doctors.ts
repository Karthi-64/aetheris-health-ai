import { useQuery } from '@tanstack/react-query'
import { ApiError, type Paginated } from '@atheris/api-core'
import { http } from '@/api/client'
import { isSendable as isHospitalRef } from '@/api/hospitals'

/**
 * Doctor discovery at one hospital. Like the hospitals, this is reference
 * data: the same for every signed-in patient. A hospital, a doctor and a
 * department are each named only by a public reference, and nothing here
 * sends an account or a patient.
 *
 * What a doctor has is exactly what is below. There is no rating, fee,
 * experience, availability, photo or contact detail in the contract, and a
 * response that carries one anyway has it dropped here, before any page sees it.
 */

/** A department of a hospital, as a filter option. */
export interface PatientDepartment {
  /** The public reference. It is sent as a filter and never shown. */
  ref: string
  name: string
  description: string | null
}

export interface DoctorQualification {
  degree: string
  institution: string | null
  year: number | null
}

/** One doctor, as the Patient App may show them. */
export interface PatientDoctor {
  /** The public reference. It names the doctor in an address and is never shown. */
  ref: string
  /** The name as the hospital keeps it; no title is added to it. */
  name: string
  specialization: string
  department: { ref: string; name: string } | null
  qualifications: DoctorQualification[]
  languages: string[]
  bio: string | null
}

/** What `GET /hospitals/{ref}/doctors` is asked for. Empty text means "no filter". */
export interface DoctorQuery {
  hospitalRef: string
  search: string
  /** A department's reference, from `GET /hospitals/{ref}/departments`. */
  department: string
  page: number
}

/** The limits of the doctor list: anything beyond them is a 422. */
export const DOCTORS_PAGE_SIZE = 20
export const DOCTOR_SEARCH_MAX_LENGTH = 80
export const DOCTORS_MAX_PAGE = 1000

export const doctorKeys = {
  list: (query: DoctorQuery) => ['patient', 'doctors', 'list', query] as const,
  detail: (hospitalRef: string, doctorRef: string) => ['patient', 'doctors', 'detail', hospitalRef, doctorRef] as const,
  departments: (hospitalRef: string) => ['patient', 'doctors', 'departments', hospitalRef] as const,
}

/**
 * A doctor's and a department's reference are UUID-shaped. Anything else names
 * neither, so it is never sent — for the reasons given at `isSendable` in
 * `hospitals.ts`: a reference is one path segment or it is nothing.
 */
export const isPublicRef = (ref: string) =>
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(ref)

/** The same answer the server gives for a reference it does not know. */
const notFound = () => new ApiError('Not found.', 'RESOURCE_NOT_FOUND', 404)

const badResponse = (what: string) => new ApiError(`Response is not ${what}`, 'bad_response')

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null

const textOrNull = (value: unknown) => (typeof value === 'string' && value.trim() !== '' ? value : null)

/**
 * A doctor from a response, field by field. Only what the contract names is
 * copied, so nothing else a response carries can reach the screen; a missing
 * optional part is its empty value. What cannot be shown or linked at all — no
 * reference, no name — is a failed read, not a blank card.
 */
function toDoctor(value: unknown): PatientDoctor {
  if (!isRecord(value) || typeof value.ref !== 'string' || !isPublicRef(value.ref)) throw badResponse('a doctor')
  if (typeof value.name !== 'string' || value.name.trim() === '') throw badResponse('a doctor')
  const { department } = value
  return {
    ref: value.ref,
    name: value.name,
    specialization: typeof value.specialization === 'string' ? value.specialization : '',
    department:
      isRecord(department) && typeof department.ref === 'string' && textOrNull(department.name) !== null
        ? { ref: department.ref, name: department.name as string }
        : null,
    qualifications: (Array.isArray(value.qualifications) ? value.qualifications : []).flatMap((entry: unknown) =>
      isRecord(entry) && textOrNull(entry.degree) !== null
        ? [
            {
              degree: entry.degree as string,
              institution: textOrNull(entry.institution),
              year: typeof entry.year === 'number' && Number.isInteger(entry.year) ? entry.year : null,
            },
          ]
        : [],
    ),
    languages: (Array.isArray(value.languages) ? value.languages : []).filter(
      (language: unknown): language is string => textOrNull(language) !== null,
    ),
    bio: textOrNull(value.bio),
  }
}

/** A filter that names no department matches no doctor; the server is not asked. */
const NO_DOCTORS: Paginated<PatientDoctor> = {
  items: [],
  pagination: { page: 1, pageSize: DOCTORS_PAGE_SIZE, total: 0, totalPages: 0 },
}

async function fetchDoctors({ hospitalRef, search, department, page }: DoctorQuery): Promise<Paginated<PatientDoctor>> {
  if (!isHospitalRef(hospitalRef)) throw notFound()
  if (department !== '' && !isPublicRef(department)) return NO_DOCTORS
  const result = await http.getPaginated<unknown>(`/hospitals/${encodeURIComponent(hospitalRef)}/doctors`, {
    params: {
      page,
      page_size: DOCTORS_PAGE_SIZE,
      ...(search ? { search } : {}),
      ...(department ? { department } : {}),
    },
  })
  // A page that is not a list can be neither shown nor counted: it is a failed read.
  if (!Array.isArray(result.items)) throw badResponse('a list')
  return { items: result.items.map(toDoctor), pagination: result.pagination }
}

/** One page of a hospital's doctors, by name, specialisation and department. */
export function useDoctors(query: DoctorQuery) {
  const { hospitalRef, search, department, page } = query
  return useQuery({
    queryKey: doctorKeys.list({ hospitalRef, search, department, page }),
    queryFn: () => fetchDoctors({ hospitalRef, search, department, page }),
  })
}

async function fetchDoctor(hospitalRef: string, doctorRef: string): Promise<PatientDoctor> {
  if (!isHospitalRef(hospitalRef) || !isPublicRef(doctorRef)) throw notFound()
  const doctor = await http.get<unknown>(
    `/hospitals/${encodeURIComponent(hospitalRef)}/doctors/${encodeURIComponent(doctorRef)}`,
  )
  return toDoctor(doctor)
}

/**
 * One doctor of one hospital. Unknown, hidden and "of another hospital" are
 * the same 404. Nothing is asked until the hospital is known (`hospitalRef`
 * is not empty).
 */
export function useDoctor(hospitalRef: string, doctorRef: string) {
  return useQuery({
    queryKey: doctorKeys.detail(hospitalRef, doctorRef),
    queryFn: () => fetchDoctor(hospitalRef, doctorRef),
    enabled: hospitalRef !== '',
  })
}

/** The departments that have a listed doctor: the options of the department filter. */
export function useDepartments(hospitalRef: string) {
  return useQuery({
    queryKey: doctorKeys.departments(hospitalRef),
    queryFn: async (): Promise<PatientDepartment[]> => {
      if (!isHospitalRef(hospitalRef)) throw notFound()
      const { departments } = await http.get<{ departments: unknown }>(
        `/hospitals/${encodeURIComponent(hospitalRef)}/departments`,
      )
      // Options are built from these; one that cannot be sent or named is not one.
      if (!Array.isArray(departments)) return []
      return departments.flatMap((entry: unknown) =>
        isRecord(entry) && typeof entry.ref === 'string' && isPublicRef(entry.ref) && textOrNull(entry.name) !== null
          ? [{ ref: entry.ref, name: entry.name as string, description: textOrNull(entry.description) }]
          : [],
      )
    },
  })
}
