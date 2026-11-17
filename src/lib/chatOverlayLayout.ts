export interface OverlayBounds {
  width: number
  height: number
}

export interface OverlayRect extends OverlayBounds {
  x: number
  y: number
}

export interface DesktopOverlayLayout {
  version: 1
  rect: OverlayRect
  maximized: boolean
}

const minimumWidth = 320
const minimumHeight = 180

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), maximum)
}

export function clampOverlayRect(rect: OverlayRect, bounds: OverlayBounds): OverlayRect {
  const width = clamp(rect.width, Math.min(minimumWidth, bounds.width), bounds.width)
  const height = clamp(rect.height, Math.min(minimumHeight, bounds.height), bounds.height)
  return {
    x: clamp(rect.x, 0, bounds.width - width),
    y: clamp(rect.y, 0, bounds.height - height),
    width,
    height,
  }
}

export function defaultOverlayRect(bounds: OverlayBounds): OverlayRect {
  const rect = clampOverlayRect({ x: 0, y: 0, width: Math.min(960, bounds.width * .86), height: Math.min(720, bounds.height * .82) }, bounds)
  return { ...rect, x: (bounds.width - rect.width) / 2, y: (bounds.height - rect.height) / 2 }
}

export function resizeOverlayRect(rect: OverlayRect, deltaWidth: number, deltaHeight: number, bounds: OverlayBounds): OverlayRect {
  const start = clampOverlayRect(rect, bounds)
  const availableWidth = bounds.width - start.x
  const availableHeight = bounds.height - start.y
  return {
    ...start,
    width: clamp(start.width + deltaWidth, Math.min(minimumWidth, availableWidth), availableWidth),
    height: clamp(start.height + deltaHeight, Math.min(minimumHeight, availableHeight), availableHeight),
  }
}

export function parseOverlayLayout(raw: string | null): DesktopOverlayLayout | null {
  if (!raw) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (!value || typeof value !== 'object') return null
    const layout = value as Partial<DesktopOverlayLayout>
    const rect = layout.rect
    if (layout.version !== 1 || typeof layout.maximized !== 'boolean' || !rect
      || ![rect.x, rect.y, rect.width, rect.height].every((number) => typeof number === 'number' && Number.isFinite(number))
      || rect.width <= 0 || rect.height <= 0) return null
    return { version: 1, rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height }, maximized: layout.maximized }
  } catch {
    return null
  }
}
