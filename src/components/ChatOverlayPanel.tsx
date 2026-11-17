import { ui, uiFormat, useUiLanguage } from '../lib/uiLanguage'
import { Maximize2, Minimize2, Move, MoveDiagonal2, RotateCcw, X } from 'lucide-react'
import { useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode, type RefObject } from 'react'
import { clampOverlayRect, defaultOverlayRect, parseOverlayLayout, resizeOverlayRect, type DesktopOverlayLayout, type OverlayRect } from '../lib/chatOverlayLayout'
import { isMobileLayout, mobileLayoutChangedEvent } from '../lib/mobileLayout'

const heightStorageKey = 'devwisp:chat-overlay-height'
const minimumPanelHeight = 180

interface ChatOverlayPanelProps {
  layoutKey: 'file-preview' | 'terminal'
  ariaLabel: string
  backdropLabel: string
  header: ReactNode
  children: ReactNode
  footer?: ReactNode
  onClose: () => void
  onEscape?: () => void
  hidden?: boolean
  showCloseButton?: boolean
  closeButtonRef?: RefObject<HTMLButtonElement | null>
  ariaModal?: boolean
  className?: string
  onKeyDown?: (event: ReactKeyboardEvent<HTMLElement>) => void
  onPanelPointerDown?: (event: ReactPointerEvent<HTMLElement>) => void
}

type OverlayEscapeEntry = { close: () => void; onTopChange: (top: boolean) => void }
type FloatingGesture = {
  pointerId: number
  x: number
  y: number
  rect: OverlayRect
  nextRect: OverlayRect
  kind: 'move' | 'size' | 'height'
}

const overlayEscapeStack: OverlayEscapeEntry[] = []
let overlayEscapeListenerAttached = false

function handleOverlayEscape(event: KeyboardEvent) {
  if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return
  if (event.target instanceof Element && event.target.closest('[role="dialog"][aria-modal="true"]')
    && !event.target.closest('.chat-overlay-panel')) return
  const activeOverlay = overlayEscapeStack[overlayEscapeStack.length - 1]
  if (!activeOverlay) return
  event.preventDefault()
  event.stopImmediatePropagation()
  activeOverlay.close()
}

function notifyOverlayOrder() {
  for (const entry of overlayEscapeStack) entry.onTopChange(entry === overlayEscapeStack[overlayEscapeStack.length - 1])
}

function registerOverlayEscape(close: () => void, onTopChange: (top: boolean) => void) {
  const entry = { close, onTopChange }
  overlayEscapeStack.push(entry)
  notifyOverlayOrder()
  if (!overlayEscapeListenerAttached) {
    document.addEventListener('keydown', handleOverlayEscape, true)
    overlayEscapeListenerAttached = true
  }
  return {
    entry,
    unregister: () => {
      const index = overlayEscapeStack.indexOf(entry)
      if (index >= 0) overlayEscapeStack.splice(index, 1)
      notifyOverlayOrder()
      if (overlayEscapeStack.length === 0 && overlayEscapeListenerAttached) {
        document.removeEventListener('keydown', handleOverlayEscape, true)
        overlayEscapeListenerAttached = false
      }
    },
  }
}

function activateOverlayEscape(entry: OverlayEscapeEntry) {
  const index = overlayEscapeStack.indexOf(entry)
  if (index >= 0 && index !== overlayEscapeStack.length - 1) {
    overlayEscapeStack.splice(index, 1)
    overlayEscapeStack.push(entry)
    notifyOverlayOrder()
  }
}

function isDesktopWindow() {
  return typeof window !== 'undefined' && !isMobileLayout() && window.matchMedia('(pointer: fine)').matches
}

function readStoredLayout(key: string) {
  try {
    return parseOverlayLayout(window.localStorage.getItem(key))
  } catch {
    return null
  }
}

function readStoredHeight() {
  if (typeof window === 'undefined') return null
  try {
    const value = Number(window.sessionStorage.getItem(heightStorageKey))
    return Number.isFinite(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

function panelMinimumHeight(maximum: number) {
  return Math.min(minimumPanelHeight, maximum)
}

export function ChatOverlayPanel({
  layoutKey,
  ariaLabel,
  backdropLabel,
  header,
  children,
  footer,
  onClose,
  onEscape,
  hidden = false,
  showCloseButton = true,
  closeButtonRef,
  ariaModal,
  className = '',
  onKeyDown,
  onPanelPointerDown,
}: ChatOverlayPanelProps) {
  useUiLanguage()
  const backdrop = useRef<HTMLDivElement>(null)
  const panel = useRef<HTMLElement>(null)
  const escapeHandler = useRef(onEscape ?? onClose)
  const escapeEntry = useRef<OverlayEscapeEntry | null>(null)
  const storageKey = `devwisp:chat-overlay-layout:${layoutKey}`
  const [desktop, setDesktop] = useState(isDesktopWindow)
  const [layout, setLayout] = useState<DesktopOverlayLayout | null>(() => readStoredLayout(storageKey))
  const [layer, setLayer] = useState(0)
  const gesture = useRef<FloatingGesture | null>(null)
  const gestureFrame = useRef<number | null>(null)
  const [interaction, setInteraction] = useState<'move' | 'size' | 'height' | null>(null)
  const resizeStart = useRef<{ pointerId: number; clientY: number; height: number } | null>(null)
  const [bounds, setBounds] = useState({ width: 0, height: 0 })
  const [height, setHeight] = useState<number | null>(readStoredHeight)
  const [resizing, setResizing] = useState(false)
  escapeHandler.current = onEscape ?? onClose

  useLayoutEffect(() => {
    if (hidden) return
    const registration = registerOverlayEscape(() => escapeHandler.current(), (top) => setLayer(top ? 1 : 0))
    escapeEntry.current = registration.entry
    return () => {
      escapeEntry.current = null
      registration.unregister()
    }
  }, [hidden])

  useLayoutEffect(() => {
    finishFloatingGesture()
    setInteraction(null)
    resizeStart.current = null
    setResizing(false)
  }, [desktop, hidden, storageKey, bounds.width, bounds.height])

  useLayoutEffect(() => () => {
    cancelGestureFrame()
    // Closing a window during a drag must not leave a callback touching its DOM.
    const pending = gesture.current
    gesture.current = null
    if (pending) persistLayout({ version: 1, rect: pending.nextRect, maximized: false })
  }, [storageKey])

  useLayoutEffect(() => {
    if (hidden) return
    const stored = readStoredLayout(storageKey)
    setLayout(stored)
  }, [hidden, storageKey])

  useLayoutEffect(() => {
    // A terminal recovery boundary adds an unpositioned wrapper. Measure the
    // actual containing block instead of that wrapper's content height.
    const host = backdrop.current?.offsetParent
      ?? backdrop.current?.closest('.conversation-pane, .remote-pane-grid')
      ?? backdrop.current?.parentElement
    if (!host) return
    const updateAvailableHeight = () => {
      const rectangle = host.getBoundingClientRect()
      const nextMaximum = Math.max(0, rectangle.height)
      setBounds({ width: Math.max(0, rectangle.width), height: nextMaximum })
      setDesktop(isDesktopWindow())
      setHeight((current) => current === null || nextMaximum <= 0
        ? current
        : Math.min(Math.max(current, panelMinimumHeight(nextMaximum)), nextMaximum))
    }
    updateAvailableHeight()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(updateAvailableHeight)
    observer?.observe(host)
    window.addEventListener('resize', updateAvailableHeight)
    window.addEventListener(mobileLayoutChangedEvent, updateAvailableHeight)
    const pointerQuery = window.matchMedia('(pointer: fine)')
    pointerQuery.addEventListener('change', updateAvailableHeight)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', updateAvailableHeight)
      window.removeEventListener(mobileLayoutChangedEvent, updateAvailableHeight)
      pointerQuery.removeEventListener('change', updateAvailableHeight)
    }
  }, [])

  const maximized = layout?.maximized ?? false
  const floatingRect = clampOverlayRect(layout?.rect ?? defaultOverlayRect(bounds), bounds)
  const displayedRect = maximized ? { x: 0, y: 0, ...bounds } : floatingRect
  const maximumHeight = Math.max(bounds.height, 0)
  const minimumHeight = panelMinimumHeight(maximumHeight)
  const currentHeight = height === null
    ? maximumHeight
    : maximumHeight > 0
      ? Math.min(Math.max(height, minimumHeight), maximumHeight)
      : height

  function persistLayout(next: DesktopOverlayLayout | null) {
    try {
      if (next) window.localStorage.setItem(storageKey, JSON.stringify(next))
      else window.localStorage.removeItem(storageKey)
    } catch {
      // Keep the current window usable even when browser storage is unavailable.
    }
  }

  function updateLayout(next: DesktopOverlayLayout | null, persist = false) {
    setLayout(next)
    if (persist) persistLayout(next)
  }

  function activateWindow() {
    if (desktop && escapeEntry.current) activateOverlayEscape(escapeEntry.current)
  }

  function beginFloating(event: ReactPointerEvent<HTMLElement>, kind: 'move' | 'size' | 'height') {
    if (!desktop || maximized || !event.isPrimary || (event.button !== 0 && event.pointerType !== 'touch')) return
    const target = event.target as HTMLElement
    if (kind === 'move' && target.closest('button, a, input, textarea, select, [contenteditable="true"]')
      && !target.closest('.chat-overlay-panel__move')) return
    event.preventDefault()
    event.stopPropagation()
    gesture.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, rect: floatingRect, nextRect: floatingRect, kind }
    event.currentTarget.setPointerCapture(event.pointerId)
    setInteraction(kind)
  }

  function updateFloatingTarget(event: ReactPointerEvent<HTMLElement>) {
    const start = gesture.current
    if (!start || start.pointerId !== event.pointerId) return
    const dx = event.clientX - start.x
    const dy = event.clientY - start.y
    start.nextRect = start.kind === 'move'
      ? clampOverlayRect({ ...start.rect, x: start.rect.x + dx, y: start.rect.y + dy }, bounds)
      : resizeOverlayRect(start.rect, start.kind === 'height' ? 0 : dx, dy, bounds)
  }

  function paintFloatingGesture() {
    gestureFrame.current = null
    const start = gesture.current
    const element = panel.current
    if (!start || !element) return
    if (start.kind === 'move') {
      // Moving a large editor/terminal should only composite its existing pixels.
      // Keep React and layout properties out of the pointermove hot path.
      element.style.transform = `translate3d(${start.nextRect.x - start.rect.x}px, ${start.nextRect.y - start.rect.y}px, 0)`
    } else {
      element.style.width = `${start.nextRect.width}px`
      element.style.height = `${start.nextRect.height}px`
    }
  }

  function cancelGestureFrame() {
    if (gestureFrame.current !== null) window.cancelAnimationFrame(gestureFrame.current)
    gestureFrame.current = null
  }

  function moveFloating(event: ReactPointerEvent<HTMLElement>) {
    if (gesture.current?.pointerId !== event.pointerId) return
    updateFloatingTarget(event)
    if (gestureFrame.current === null) gestureFrame.current = window.requestAnimationFrame(paintFloatingGesture)
  }

  function finishFloatingGesture() {
    cancelGestureFrame()
    const start = gesture.current
    if (!start) return
    gesture.current = null
    const element = panel.current
    if (element) {
      // Flush the final position before removing the temporary transform so a
      // fast release never jumps back or loses a not-yet-painted pointermove.
      if (desktop) {
        const visible = clampOverlayRect(start.nextRect, bounds)
        element.style.left = `${visible.x}px`
        element.style.top = `${visible.y}px`
        element.style.width = `${visible.width}px`
        element.style.height = `${visible.height}px`
      }
      element.style.removeProperty('transform')
    }
    setInteraction(null)
    updateLayout({ version: 1, rect: start.nextRect, maximized: false }, true)
  }

  function finishFloating(event: ReactPointerEvent<HTMLElement>) {
    if (gesture.current?.pointerId !== event.pointerId) return
    if (event.type === 'pointerup') updateFloatingTarget(event)
    finishFloatingGesture()
  }

  function handleFloatingKey(event: ReactKeyboardEvent<HTMLElement>, kind: 'move' | 'size' | 'height') {
    if (!desktop || maximized || !['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
    event.preventDefault()
    event.stopPropagation()
    const step = event.shiftKey ? 64 : 16
    const dx = event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0
    const dy = event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0
    const rect = kind === 'move'
      ? clampOverlayRect({ ...floatingRect, x: floatingRect.x + dx, y: floatingRect.y + dy }, bounds)
      : resizeOverlayRect(floatingRect, kind === 'height' ? 0 : dx, dy, bounds)
    updateLayout({ version: 1, rect, maximized: false }, true)
  }

  function saveHeight(nextHeight: number) {
    const maximum = panel.current?.parentElement?.getBoundingClientRect().height ?? maximumHeight
    const next = Math.min(Math.max(nextHeight, panelMinimumHeight(maximum)), maximum)
    setHeight(next)
    try {
      window.sessionStorage.setItem(heightStorageKey, String(Math.round(next)))
    } catch {
      // The current tab still keeps the chosen height when storage is unavailable.
    }
  }

  function beginResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0 && event.pointerType !== 'touch') return
    event.preventDefault()
    event.stopPropagation()
    const startingHeight = panel.current?.getBoundingClientRect().height ?? currentHeight
    resizeStart.current = { pointerId: event.pointerId, clientY: event.clientY, height: startingHeight }
    event.currentTarget.setPointerCapture(event.pointerId)
    setResizing(true)
  }

  function moveResize(event: ReactPointerEvent<HTMLDivElement>) {
    const start = resizeStart.current
    if (!start || start.pointerId !== event.pointerId) return
    saveHeight(start.height + event.clientY - start.clientY)
  }

  function finishResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (resizeStart.current?.pointerId !== event.pointerId) return
    resizeStart.current = null
    setResizing(false)
  }

  function handleResizeKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const increment = event.shiftKey ? 96 : 32
    const next = event.key === 'ArrowUp'
      ? currentHeight - increment
      : event.key === 'ArrowDown'
        ? currentHeight + increment
        : event.key === 'Home'
          ? minimumHeight
          : event.key === 'End'
            ? maximumHeight
            : null
    if (next === null) return
    event.preventDefault()
    saveHeight(next)
  }

  function resetHeight() {
    setHeight(null)
    try {
      window.sessionStorage.removeItem(heightStorageKey)
    } catch {
      // A full-height panel still works when storage is unavailable.
    }
  }

  return (
    <div ref={backdrop} className={`chat-overlay-backdrop${resizing ? ' chat-overlay-backdrop--resizing' : ''}${desktop ? ' chat-overlay-backdrop--floating' : ''}${interaction ? ` chat-overlay-backdrop--${interaction}` : ''}`} hidden={hidden} style={{ ...(hidden ? { display: 'none' } : {}), ...(desktop ? { zIndex: 31 + layer } : {}) }}>
      <button className="chat-overlay-backdrop__dismiss" type="button" aria-label={backdropLabel} tabIndex={-1} onClick={onClose} />
      <section
        ref={panel}
        className={`chat-overlay-panel${className ? ` ${className}` : ''}${desktop ? ' chat-overlay-panel--floating' : ''}${desktop && maximized ? ' chat-overlay-panel--maximized' : ''}`}
        style={(desktop ? { left: displayedRect.x, top: displayedRect.y, width: displayedRect.width, height: displayedRect.height } : { '--chat-overlay-height': height === null ? '100%' : `${currentHeight}px` }) as CSSProperties}
        role="dialog"
        aria-modal={desktop ? undefined : ariaModal}
        aria-label={ariaLabel}
        onKeyDown={onKeyDown}
        onPointerDown={onPanelPointerDown}
        onPointerDownCapture={activateWindow}
        onFocusCapture={activateWindow}
      >
        <header className="chat-overlay-panel__header" onPointerDown={(event) => beginFloating(event, 'move')} onPointerMove={moveFloating} onPointerUp={finishFloating} onPointerCancel={finishFloating} onLostPointerCapture={finishFloating}>
          {desktop ? <button className="chat-overlay-panel__move" type="button" disabled={maximized} aria-label={ui('移动浮窗')} title={ui('拖动标题栏移动；方向键微调位置')} onKeyDown={(event) => handleFloatingKey(event, 'move')}><Move size={15} /></button> : null}
          <div className="chat-overlay-panel__heading">{header}</div>
          <div className="chat-overlay-panel__window-actions">
          {desktop ? <>
            <button className="icon-button chat-overlay-panel__close" type="button" onClick={() => updateLayout(null, true)} aria-label={ui('重置浮窗位置和大小')} title={ui('重置浮窗位置和大小')}><RotateCcw size={15} /></button>
            <button className="icon-button chat-overlay-panel__close" type="button" onClick={() => updateLayout({ version: 1, rect: layout?.rect ?? floatingRect, maximized: !maximized }, true)} aria-label={maximized ? ui('还原浮窗') : ui('铺满聊天区域')} title={maximized ? ui('还原浮窗') : ui('铺满聊天区域')}>{maximized ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
          </> : null}
          {showCloseButton || desktop ? (
            <button ref={closeButtonRef} className="icon-button chat-overlay-panel__close" type="button" onClick={onClose} aria-label={uiFormat("关闭{0}", ariaLabel)} title={uiFormat("关闭{0}", ariaLabel)}>
              <X size={16} />
            </button>
          ) : null}
          </div>
        </header>
        <div className="chat-overlay-panel__body">{children}</div>
        {footer !== undefined ? <footer className="chat-overlay-panel__footer">{footer}</footer> : null}
        <div
          className="chat-overlay-panel__resize-handle"
          role="separator"
          aria-label={ui("调整浮层高度")}
          aria-orientation="horizontal"
          aria-valuemin={minimumHeight}
          aria-valuemax={desktop ? bounds.height - displayedRect.y : maximumHeight}
          aria-valuenow={Math.round(desktop ? displayedRect.height : currentHeight)}
          aria-valuetext={uiFormat("{0} 像素", Math.round(desktop ? displayedRect.height : currentHeight))}
          aria-disabled={desktop && maximized || undefined}
          tabIndex={desktop && maximized ? -1 : 0}
          onPointerDown={(event) => desktop ? beginFloating(event, 'height') : beginResize(event)}
          onPointerMove={(event) => desktop ? moveFloating(event) : moveResize(event)}
          onPointerUp={(event) => desktop ? finishFloating(event) : finishResize(event)}
          onPointerCancel={(event) => desktop ? finishFloating(event) : finishResize(event)}
          onLostPointerCapture={(event) => desktop ? finishFloating(event) : finishResize(event)}
          onDoubleClick={() => desktop ? updateLayout({ version: 1, rect: layout?.rect ?? floatingRect, maximized: !maximized }, true) : resetHeight()}
          onKeyDown={(event) => desktop ? handleFloatingKey(event, 'height') : handleResizeKeyDown(event)}
          title={desktop ? ui('拖动调整高度；方向键微调，Shift 加大步长') : ui("拖动调整高度，双击恢复铺满；方向键微调，Shift 加大步长")}
        />
        {desktop && !maximized ? <button className="chat-overlay-panel__resize-corner" type="button" aria-label={ui('调整浮窗大小')} title={ui('拖动调整宽高；方向键微调大小')} onPointerDown={(event) => beginFloating(event, 'size')} onPointerMove={moveFloating} onPointerUp={finishFloating} onPointerCancel={finishFloating} onLostPointerCapture={finishFloating} onKeyDown={(event) => handleFloatingKey(event, 'size')}><MoveDiagonal2 size={13} /></button> : null}
      </section>
    </div>
  )
}
