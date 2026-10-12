import {
  Area,
  AreaChart as ReAreaChart,
  Bar,
  BarChart as ReBarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart as ReLineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { AXIS_PROPS, CHART_COLORS, GRID_STROKE, TOOLTIP_STYLE } from './chart-theme'

export interface Series {
  key: string
  label?: string
  color?: string
}

interface CartesianChartProps {
  data: Array<Record<string, string | number>>
  xKey: string
  series: Series[]
  height?: number
  /** Hide the legend when a single series makes it redundant. */
  showLegend?: boolean
  /** How a value reads on the Y axis and in the tooltip (e.g. compact money). Formatting only. */
  valueFormatter?: (n: number) => string
  /** How an X value reads on the axis and as the tooltip's heading. */
  xTickFormatter?: (v: string) => string
  /** Room for the Y axis labels; widen it for formatted amounts. */
  yAxisWidth?: number
}

/**
 * These charts are a picture of figures the page already holds. A screen that
 * shows one also shows the same rows as a table (see `ChartCard`), which is
 * what a screen reader, a test and anyone who cannot tell the series colours
 * apart reads; the legend and the tooltip name every series in words.
 */

const DEFAULT_Y_AXIS_WIDTH = 40

/** Tooltip values in the caller's format; anything that is not a number is shown as sent. */
function tooltipValue(valueFormatter: ((n: number) => string) | undefined) {
  if (!valueFormatter) return undefined
  return (value: unknown) => (typeof value === 'number' ? valueFormatter(value) : String(value ?? ''))
}

/** The tooltip's heading, formatted like the X axis tick it belongs to. */
function tooltipLabel(xTickFormatter: ((v: string) => string) | undefined) {
  if (!xTickFormatter) return undefined
  return (label: unknown) => (typeof label === 'string' ? xTickFormatter(label) : String(label ?? ''))
}

/** Line patterns, so two lines differ by more than their colour. */
const LINE_DASHES = [undefined, '6 4', '2 4', '10 4 2 4', '1 5'] as const

const legendStyle = { fontFamily: 'var(--font-label)', fontSize: 12 }

function color(s: Series, i: number) {
  return s.color ?? CHART_COLORS[i % CHART_COLORS.length]
}

export function LineChart({
  data,
  xKey,
  series,
  height = 280,
  showLegend = true,
  valueFormatter,
  xTickFormatter,
  yAxisWidth = DEFAULT_Y_AXIS_WIDTH,
}: CartesianChartProps) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ReLineChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
        <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey={xKey} {...AXIS_PROPS} tickFormatter={xTickFormatter} />
        <YAxis {...AXIS_PROPS} width={yAxisWidth} tickFormatter={valueFormatter} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          cursor={{ stroke: GRID_STROKE }}
          formatter={tooltipValue(valueFormatter)}
          labelFormatter={tooltipLabel(xTickFormatter)}
        />
        {showLegend && series.length > 1 && <Legend wrapperStyle={legendStyle} />}
        {series.map((s, i) => (
          <Line
            key={s.key}
            type="monotone"
            dataKey={s.key}
            name={s.label ?? s.key}
            stroke={color(s, i)}
            strokeWidth={2.5}
            strokeDasharray={LINE_DASHES[i % LINE_DASHES.length]}
            dot={false}
            activeDot={{ r: 4 }}
          />
        ))}
      </ReLineChart>
    </ResponsiveContainer>
  )
}

export function AreaChart({
  data,
  xKey,
  series,
  height = 280,
  showLegend = true,
  valueFormatter,
  xTickFormatter,
  yAxisWidth = DEFAULT_Y_AXIS_WIDTH,
}: CartesianChartProps) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ReAreaChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
        <defs>
          {series.map((s, i) => (
            <linearGradient key={s.key} id={`fill-${s.key}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={color(s, i)} stopOpacity={0.35} />
              <stop offset="95%" stopColor={color(s, i)} stopOpacity={0} />
            </linearGradient>
          ))}
        </defs>
        <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey={xKey} {...AXIS_PROPS} tickFormatter={xTickFormatter} />
        <YAxis {...AXIS_PROPS} width={yAxisWidth} tickFormatter={valueFormatter} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          cursor={{ stroke: GRID_STROKE }}
          formatter={tooltipValue(valueFormatter)}
          labelFormatter={tooltipLabel(xTickFormatter)}
        />
        {showLegend && series.length > 1 && <Legend wrapperStyle={legendStyle} />}
        {series.map((s, i) => (
          <Area
            key={s.key}
            type="monotone"
            dataKey={s.key}
            name={s.label ?? s.key}
            stroke={color(s, i)}
            strokeWidth={2.5}
            fill={`url(#fill-${s.key})`}
          />
        ))}
      </ReAreaChart>
    </ResponsiveContainer>
  )
}

interface BarChartProps extends CartesianChartProps {
  /**
   * Draw the series of one X value on top of each other instead of side by
   * side. The bar's height is then drawn by the chart from the series it is
   * given; the total a user reads is still the server's, in the table.
   */
  stacked?: boolean
}

export function BarChart({
  data,
  xKey,
  series,
  height = 280,
  showLegend = true,
  valueFormatter,
  xTickFormatter,
  yAxisWidth = DEFAULT_Y_AXIS_WIDTH,
  stacked = false,
}: BarChartProps) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ReBarChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
        <CartesianGrid stroke={GRID_STROKE} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey={xKey} {...AXIS_PROPS} tickFormatter={xTickFormatter} />
        <YAxis {...AXIS_PROPS} width={yAxisWidth} tickFormatter={valueFormatter} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          cursor={{ fill: 'var(--color-surface-container)' }}
          formatter={tooltipValue(valueFormatter)}
          labelFormatter={tooltipLabel(xTickFormatter)}
        />
        {showLegend && series.length > 1 && <Legend wrapperStyle={legendStyle} />}
        {series.map((s, i) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            name={s.label ?? s.key}
            fill={color(s, i)}
            // Stacked segments are square and outlined in the surface colour,
            // so neighbours are told apart by an edge as well as by colour.
            {...(stacked
              ? { stackId: 'stack', stroke: 'var(--color-surface)', strokeWidth: 1 }
              : { radius: [6, 6, 0, 0] as [number, number, number, number] })}
          />
        ))}
      </ReBarChart>
    </ResponsiveContainer>
  )
}
