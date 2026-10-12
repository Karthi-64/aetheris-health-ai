import { createElement } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { FigureCell, PeriodCell } from './reportParts'

/**
 * Column builders for the report tables. Rows arrive display-ready: every
 * cell is a string the page formatted from a server value. Sorting is off —
 * the server's order (by period, by size) is the report's order.
 */

/** What every row of a period table carries besides its figures. */
export interface PeriodRow {
  label: string
  /** The server's flag: the figures cover only part of this period. */
  partial: boolean
  /** The closing row, holding the server's summary. */
  isTotal: boolean
}

export function periodColumn<T extends PeriodRow>(): ColumnDef<T> {
  return {
    id: 'period',
    header: 'Period',
    enableSorting: false,
    cell: ({ row }) =>
      createElement(PeriodCell, {
        label: row.original.label,
        partial: row.original.partial,
        isTotal: row.original.isTotal,
      }),
  }
}

/** A column of figures, shown as sent. A Total row's figure is set in bold. */
export function figureColumn<T extends object>(key: keyof T & string, header: string): ColumnDef<T> {
  return {
    id: key,
    header,
    enableSorting: false,
    cell: ({ row }) => {
      const original = row.original as T & { isTotal?: boolean }
      return createElement(FigureCell, { strong: original.isTotal === true, children: String(original[key]) })
    },
  }
}
