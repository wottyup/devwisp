import { useEffect, useId, useRef, useState } from 'react'
import { getUiLanguage, ui, useUiLanguage } from '../lib/uiLanguage'
import './UsageTrendChart.css'

export interface UsageTrendPoint {
  id: string
  time: number
  label: string
  shortLabel: string
  totalTokens: number | null
  inputTokens?: number | null
  outputTokens?: number | null
}

/** Shared by the project ledger and provider analytics; missing values remain gaps. */
export function UsageTrendChart({ points }: { points: UsageTrendPoint[] }) {
  useUiLanguage()
  const container = useRef<HTMLDivElement>(null)
  const tooltipId = useId()
  const [width, setWidth] = useState(640)
  const [activeId, setActiveId] = useState<string | null>(null)
  useEffect(() => {
    const element = container.current
    if (!element) return
    const measure = () => { if (element.clientWidth > 0) setWidth(element.clientWidth) }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  const data = [...points].sort((a, b) => a.time - b.time)
  const height = 240
  const left = Math.min(58, width * .2), right = Math.max(left + 1, width - 18)
  const top = 18, bottom = height - 34
  const known = (value: number | null | undefined): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
  const maximum = Math.ceil(Math.max(1, ...data.map((point) => known(point.totalTokens) ? point.totalTokens : 0)) / 4) * 4
  const first = data[0]?.time ?? 0, last = data.at(-1)?.time ?? first
  const coordinates = data.map((point) => ({ ...point,
    x: last === first ? (left + right) / 2 : left + (point.time - first) / (last - first) * (right - left),
    y: known(point.totalTokens) ? bottom - point.totalTokens / maximum * (bottom - top) : null,
  }))
  let connected = false
  const path = coordinates.map((point) => {
    if (point.y === null) { connected = false; return '' }
    const command = `${connected ? 'L' : 'M'}${point.x},${point.y}`
    connected = true
    return command
  }).join(' ')
  const active = coordinates.find((point) => point.id === activeId)
  const format = (value: number | null | undefined) => known(value) ? value.toLocaleString(getUiLanguage()) : '—'
  const compact = new Intl.NumberFormat(getUiLanguage(), { notation: 'compact', maximumFractionDigits: 1 })
  const labelEvery = Math.max(1, Math.ceil(data.length / Math.max(2, Math.floor((right - left) / 90))))
  const tooltipWidth = Math.min(240, width - 16)

  const selectAt = (clientX: number) => {
    const bounds = container.current?.getBoundingClientRect()
    if (!bounds || !coordinates.length) return
    const x = clientX - bounds.left
    const nearest = coordinates.reduce((best, point) => Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best)
    setActiveId(nearest.id)
  }

  return <div className="usage-trend" ref={container}
    onPointerLeave={(event) => { if (event.pointerType !== 'touch' && !event.currentTarget.contains(document.activeElement)) setActiveId(null) }}
    onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setActiveId(null) }}>
    {data.length ? <>
      <svg className="usage-trend__plot" width="100%" height={height} viewBox={`0 0 ${width} ${height}`} role="group" aria-label={ui('Token 用量趋势图')}
        onPointerMove={(event) => { if (event.pointerType !== 'touch') selectAt(event.clientX) }}
        onPointerDown={(event) => { if (event.pointerType === 'touch') selectAt(event.clientX) }}>
        {[0, .25, .5, .75, 1].map((ratio) => {
          const y = bottom - ratio * (bottom - top)
          return <g key={ratio} className="usage-trend__axis" aria-hidden="true">
            <line x1={left} x2={right} y1={y} y2={y} />
            <text x={left - 10} y={y + 4} textAnchor="end">{compact.format(maximum * ratio)}</text>
          </g>
        })}
        <path className="usage-trend__line" d={path} fill="none" vectorEffect="non-scaling-stroke" aria-hidden="true" />
        {coordinates.map((point, index) => <g key={point.id}>
          {point.y !== null && <circle className="usage-trend__dot" cx={point.x} cy={point.y} r={data.length > 60 ? 2 : 3} aria-hidden="true" />}
          {(index % labelEvery === 0 && index < data.length - 1 - labelEvery / 2 || index === data.length - 1) &&
            <text className="usage-trend__label" x={point.x} y={height - 8} textAnchor={index === 0 ? 'start' : index === data.length - 1 ? 'end' : 'middle'} aria-hidden="true">{point.shortLabel}</text>}
          <rect className="usage-trend__target" x={index === 0 ? left : (coordinates[index - 1].x + point.x) / 2} y={top}
            width={Math.max(1, (index === data.length - 1 ? right : (coordinates[index + 1].x + point.x) / 2) - (index === 0 ? left : (coordinates[index - 1].x + point.x) / 2))} height={bottom - top} fill="transparent"
            role="button" tabIndex={index === 0 ? 0 : -1} data-point-index={index}
            aria-label={`${point.label} · Token ${format(point.totalTokens)}`}
            aria-describedby={active?.id === point.id ? tooltipId : undefined}
            onFocus={() => setActiveId(point.id)} onClick={() => setActiveId(point.id)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') { setActiveId(null); return }
              if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setActiveId(point.id); return }
              const next = event.key === 'Home' ? 0 : event.key === 'End' ? data.length - 1
                : event.key === 'ArrowRight' ? Math.min(data.length - 1, index + 1) : event.key === 'ArrowLeft' ? Math.max(0, index - 1) : null
              if (next !== null) {
                event.preventDefault()
                container.current?.querySelector<SVGElement>(`[data-point-index="${next}"]`)?.focus()
              }
            }} />
        </g>)}
        {active && <g className="usage-trend__active" aria-hidden="true">
          <line x1={active.x} x2={active.x} y1={top} y2={bottom} />
          {active.y !== null && <circle cx={active.x} cy={active.y} r="5" />}
        </g>}
      </svg>
      {active && <div id={tooltipId} className="usage-trend__tooltip" role="tooltip" style={{
        width: tooltipWidth, left: Math.max(8, Math.min(width - tooltipWidth - 8, active.x - tooltipWidth / 2)),
        top: Math.max(4, Math.min(height - 126, (active.y ?? bottom) - 100)),
      }}>
        <strong>{active.label}</strong>
        <span>{ui('总 Token')}<b>{format(active.totalTokens)}</b></span>
        {active.inputTokens !== undefined && <span>{ui('输入')}<b>{format(active.inputTokens)}</b></span>}
        {active.outputTokens !== undefined && <span>{ui('输出')}<b>{format(active.outputTokens)}</b></span>}
      </div>}
    </> : <div className="cpa-usage-empty">{ui('所选时间段暂无趋势数据。')}</div>}
  </div>
}
