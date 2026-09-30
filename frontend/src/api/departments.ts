import { useQuery } from '@tanstack/react-query'
import { http } from '@/api/http'
import type { Paginated } from '@/api/types'

/** Department contract (`backend/app/api/v1/departments.py`). */
export type DepartmentStatus = 'active' | 'inactive'

export interface DepartmentSummary {
  id: string
  code: string
  name: string
  location: string | null
  status: DepartmentStatus
}

export const departmentKeys = {
  all: ['departments'] as const,
  list: () => [...departmentKeys.all, 'list'] as const,
}

/**
 * Active departments for filters and selects. One large page is enough — a
 * hospital has tens of departments, not thousands.
 */
export function useDepartments() {
  return useQuery<DepartmentSummary[]>({
    queryKey: departmentKeys.list(),
    queryFn: async () => {
      const res = await http.getPaginated<DepartmentSummary>('/departments', {
        params: { page: 1, page_size: 100 },
      })
      return (res as Paginated<DepartmentSummary>).items
    },
    staleTime: 5 * 60_000,
  })
}
