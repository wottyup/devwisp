import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { devwispClient } from '../lib/devwispClient'
import { subscribeLocalUsage, type LocalUsageResponse, type UsageEngine } from '../lib/localUsage'
import { getUiLanguage, ui, useUiLanguage } from '../lib/uiLanguage'
import './ComposerUsageDetails.css'

/** Fetch the ledger's complete model aggregation only while details are visible. */
export function ComposerUsageDetails({ token, engine, threadId, label, description, children }: {
  token: string
  engine: UsageEngine | ''
  threadId?: string
  label: string
  description: string
  children: ReactNode
}) {
  useUiLanguage()
  const id = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const popup = useRef<HTMLDivElement>(null)
  const wasOpenOnPointerDown = useRef(false)
  const hoverDismissed = useRef(false)
  const closeTimer = useRef<number | undefined>(undefined)
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<LocalUsageResponse | null>(null)
  const [failed, setFailed] = useState(false)
  const [retry, setRetry] = useState(0)
  const supported = typeof HTMLElement !== 'undefined' && typeof HTMLElement.prototype.showPopover === 'function'
  const cancelClose = () => window.clearTimeout(closeTimer.current)
  const hide = () => {
    cancelClose()
    hoverDismissed.current = Boolean(trigger.current?.matches(':hover'))
    if (supported && popup.current?.matches(':popover-open')) popup.current.hidePopover()
    setOpen(false)
  }
  const position = useCallback(() => {
    const menu = popup.current, button = trigger.current
    if (!menu || !button) return
    const bounds = button.getBoundingClientRect(), edge = 8, gap = 6
    const viewport = window.visualViewport
    const viewportLeft = viewport?.offsetLeft ?? 0, viewportTop = viewport?.offsetTop ?? 0
    const viewportWidth = viewport?.width ?? window.innerWidth, viewportHeight = viewport?.height ?? window.innerHeight
    const width = Math.min(420, viewportWidth - edge * 2)
    menu.style.width = `${width}px`
    menu.style.left = `${Math.max(viewportLeft + edge, Math.min(bounds.left + bounds.width / 2 - width / 2, viewportLeft + viewportWidth - width - edge))}px`
    const above = bounds.top - viewportTop - edge - gap
    const below = viewportTop + viewportHeight - bounds.bottom - edge - gap
    const openAbove = above >= below
    menu.style.maxHeight = `${Math.max(48, Math.min(360, openAbove ? above : below))}px`
    const height = menu.getBoundingClientRect().height
    menu.style.top = `${Math.max(viewportTop + edge, Math.min(openAbove ? bounds.top - height - gap : bounds.bottom + gap, viewportTop + viewportHeight - height - edge))}px`
  }, [])
  const show = () => {
    cancelClose()
    hoverDismissed.current = false
    if (supported && popup.current && !popup.current.matches(':popover-open')) popup.current.showPopover()
    setOpen(true)
    position()
  }
  const scheduleClose = () => {
    cancelClose()
    closeTimer.current = window.setTimeout(() => {
      if (!popup.current?.contains(document.activeElement) && trigger.current !== document.activeElement) hide()
    }, 180)
  }

  useEffect(() => () => window.clearTimeout(closeTimer.current), [])
  useEffect(() => {
    if (!open) return
    position()
    const observer = new ResizeObserver(position)
    if (popup.current) observer.observe(popup.current)
    window.addEventListener('resize', position)
    document.addEventListener('scroll', position, true)
    window.visualViewport?.addEventListener('resize', position)
    window.visualViewport?.addEventListener('scroll', position)
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !popup.current?.contains(event.target) && !trigger.current?.contains(event.target)) hide()
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') hide() }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', position)
      document.removeEventListener('scroll', position, true)
      window.visualViewport?.removeEventListener('resize', position)
      window.visualViewport?.removeEventListener('scroll', position)
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [open, position])

  useEffect(() => {
    if (!open || !token) return
    let active = true, refreshAgain = false, minimumRevision = 0
    let pending: AbortController | undefined, updateTimer: number | undefined
    const load = async () => {
      if (pending) { refreshAgain = true; return }
      const controller = new AbortController()
      pending = controller
      try {
        const response = await devwispClient.localUsage(token, { range: 'all', engine, model: '', offset: 0, threadId }, controller.signal)
        if (threadId && response.threadId !== threadId) throw new Error('Conversation usage filtering is unavailable')
        if (active && !controller.signal.aborted && (response.revision ?? 0) >= minimumRevision) {
          setData((previous) => (previous?.revision ?? -1) > (response.revision ?? 0) ? previous : response)
          setFailed(false)
        }
      } catch {
        if (active && !controller.signal.aborted) setFailed(true)
      } finally {
        pending = undefined
        if (active && refreshAgain) { refreshAgain = false; if (document.visibilityState === 'visible') void load() }
      }
    }
    const unsubscribe = subscribeLocalUsage(token, (update) => {
      if (update && threadId && (update.threadId !== threadId || update.engine !== engine)) return
      if (update) minimumRevision = Math.max(minimumRevision, update.revision)
      if (document.visibilityState !== 'visible' || updateTimer !== undefined) return
      updateTimer = window.setTimeout(() => { updateTimer = undefined; void load() }, 100)
    })
    if (document.visibilityState === 'visible') void load()
    const visible = () => { if (document.visibilityState === 'visible') void load() }
    document.addEventListener('visibilitychange', visible)
    const timer = window.setInterval(visible, 30000)
    return () => {
      active = false
      pending?.abort()
      unsubscribe()
      window.clearTimeout(updateTimer)
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', visible)
    }
  }, [open, token, engine, threadId, retry])

  const count = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString(getUiLanguage())
  // Closing a top-layer popup can synthesize pointer entry; only movement reopens it.
  return <span className="composer-usage-anchor" onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) hide()
  }}>
    <button ref={trigger} className="composer-token-usage__trigger" type="button" aria-label={label} aria-haspopup="dialog" aria-expanded={open} aria-controls={id}
      onPointerEnter={cancelClose}
      onPointerMove={(event) => { if (event.pointerType !== 'touch' && !open && !hoverDismissed.current) show() }}
      onPointerLeave={() => { hoverDismissed.current = false; scheduleClose() }}
      onPointerDown={() => { wasOpenOnPointerDown.current = open }}
      onKeyDown={(event) => {
        if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { event.preventDefault(); show(); popup.current?.focus({ preventScroll: true }) }
      }} onClick={(event) => { if (event.detail > 0 && wasOpenOnPointerDown.current) hide(); else show() }}>
      {children}
    </button>
    <div ref={popup} id={id} popover={supported ? 'auto' : undefined} role="dialog" tabIndex={0} aria-label={ui('按模型统计')}
      className={`composer-menu__popover composer-menu__popover--floating composer-usage-details${open ? ' composer-usage-details--open' : ''}`}
      onToggle={(event) => { if (supported) setOpen(event.currentTarget.matches(':popover-open')) }}
      onPointerEnter={cancelClose} onPointerLeave={scheduleClose}>
      <div className="composer-usage-details__heading"><strong>{threadId ? ui('当前对话 Token') : ui('总用量')} · {ui('按模型统计')}</strong></div>
      {failed ? <p className="composer-usage-details__status" role="status">{ui('暂时无法读取用量，将自动重试。')} <button type="button" onClick={() => setRetry((value) => value + 1)}>{ui('重试')}</button></p> : null}
      {!data && !failed ? <p className="composer-usage-details__status" role="status">{ui('正在读取统计…')}</p> : null}
      {data && !data.models.length ? <p className="composer-usage-details__status">{ui('尚无已记录用量；仅统计功能启用后本项目发起的聊天。')}</p> : null}
      {data?.models.map((model) => {
        const counters: Array<[string, number | null]> = [[ui('总 Token'), model.totalTokens], [ui('输入'), model.inputTokens], [ui('输出'), model.outputTokens]]
        return <div className="composer-usage-details__model" key={model.model}>
          <strong>{model.model === 'unknown' || model.model === 'default' ? ui('未提供模型') : model.model}</strong>
          <dl>{counters.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{count(value)}</dd></div>)}</dl>
        </div>
      })}
      <p className="composer-usage-details__note">{description}</p>
    </div>
  </span>
}
