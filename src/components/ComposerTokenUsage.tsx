import { useEffect, useState } from 'react'
import { devwispClient } from '../lib/devwispClient'
import { getUiLanguage, ui, uiFormat, useUiLanguage } from '../lib/uiLanguage'
import { subscribeLocalUsage, type LocalUsageResponse, type UsageEngine } from '../lib/localUsage'
import { ComposerUsageDetails } from './ComposerUsageDetails'

type Totals = LocalUsageResponse['totals']
type Snapshot = { scope: string; totals: Totals | null; failed: boolean; revision: number }
// Keep the last total through composer remounts when switching conversations.
// This is memory-only and never reused for a different pairing token.
let lastProjectSnapshot: Snapshot | null = null

/** Read-only conversation and project totals from the same usage ledger. */
export function ComposerTokenUsage({ token, threadId, engine, isRunning }: {
  token: string
  threadId?: string
  engine: UsageEngine
  isRunning: boolean
}) {
  useUiLanguage()
  const scope = JSON.stringify([token, engine, threadId ?? ''])
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [projectSnapshot, setProjectSnapshot] = useState<Snapshot | null>(() => lastProjectSnapshot?.scope === token ? lastProjectSnapshot : null)

  useEffect(() => {
    if (!token) return
    let active = true
    let pending: AbortController | undefined
    let refreshAgain = false
    let updateTimer: number | undefined
    let minimumRevision = 0
    const load = async () => {
      // Serialize refreshes so frequent usage events cannot continually abort a slow request.
      if (pending) { refreshAgain = true; return }
      const controller = new AbortController()
      pending = controller
      try {
        const result = await devwispClient.localUsage(token, { range: 'all', engine: '', model: '', offset: 0 }, controller.signal)
        const revision = result.revision ?? 0
        if (active && !controller.signal.aborted && revision >= minimumRevision) {
          setProjectSnapshot((previous) => {
            if (previous?.scope === token && previous.revision > revision) return previous
            const next = { scope: token, totals: result.totals, revision, failed: false }
            lastProjectSnapshot = next
            return next
          })
        }
      } catch {
        if (active && !controller.signal.aborted) setProjectSnapshot((previous) =>
          previous?.scope === token ? { ...previous, failed: true } : { scope: token, totals: null, failed: true, revision: -1 })
      } finally {
        pending = undefined
        if (active && refreshAgain) {
          refreshAgain = false
          if (document.visibilityState === 'visible') void load()
        }
      }
    }
    const unsubscribe = subscribeLocalUsage(token, (update) => {
      if (update) minimumRevision = Math.max(minimumRevision, update.revision)
      if (document.visibilityState !== 'visible' || updateTimer !== undefined) return
      updateTimer = window.setTimeout(() => { updateTimer = undefined; void load() }, 100)
    })
    if (document.visibilityState === 'visible') void load()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load() }, 30000)
    const visible = () => { if (document.visibilityState === 'visible') void load() }
    document.addEventListener('visibilitychange', visible)
    return () => {
      active = false
      unsubscribe()
      pending?.abort()
      window.clearTimeout(updateTimer)
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', visible)
    }
  }, [token])

  useEffect(() => {
    if (!threadId || !token) return
    let active = true
    let pending: AbortController | undefined
    let pushes = 0
    const apply = (totals: Totals, revision: number) => setSnapshot((previous) =>
      previous?.scope === scope && previous.revision > revision ? previous : { scope, totals, revision, failed: false })
    const load = async () => {
      pending?.abort()
      const controller = new AbortController()
      pending = controller
      const startedPushes = pushes
      try {
        const result = await devwispClient.localUsage(token, { range: 'all', engine, model: '', offset: 0, threadId }, controller.signal)
        // Older backends ignore unknown filters and return project totals.
        // Never label that response as consumption for this conversation.
        if (result.threadId !== threadId) throw new Error('Conversation usage filtering is unavailable')
        if (active && !controller.signal.aborted) apply(result.totals, result.revision ?? 0)
      } catch {
        if (active && !controller.signal.aborted && pushes === startedPushes) setSnapshot((previous) =>
          previous?.scope === scope ? { ...previous, failed: true } : { scope, totals: null, failed: true, revision: -1 })
      }
    }
    const unsubscribe = subscribeLocalUsage(token, (update) => {
      if (!update) { if (document.visibilityState === 'visible') void load(); return }
      if (update.engine !== engine || update.threadId !== threadId) return
      pushes += 1
      apply(update.totals, update.revision)
    })
    if (document.visibilityState === 'visible') void load()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load() }, 30000)
    const visible = () => { if (document.visibilityState === 'visible') void load() }
    document.addEventListener('visibilitychange', visible)
    return () => {
      active = false
      unsubscribe()
      pending?.abort()
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', visible)
    }
  }, [token, threadId, engine, isRunning, scope])

  const number = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString(getUiLanguage())
  const renderUsage = (current: Snapshot | null, project: boolean) => {
    const totals = current?.totals
    const partial = Boolean(totals && ((totals.missingTurns ?? 0) > 0 || (totals.partialTurns ?? 0) > 0 || (totals.activeTurns ?? 0) > 0))
    const title = current?.failed ? ui('暂时无法读取用量，将自动重试。') : !totals?.turns ? ui('尚无已记录用量；仅统计功能启用后本项目发起的聊天。')
      : `${uiFormat(project ? '全部对话累计 · 输入 {0} · 输出 {1}' : '当前对话累计 · 输入 {0} · 输出 {1}', number(totals.inputTokens), number(totals.outputTokens))}${partial ? ` · ${ui('部分轮次尚未提供完整用量')}` : ''} · ${ui('仅统计功能启用后的已回传用量')}`
    return <ComposerUsageDetails key={project ? token : scope} token={token} engine={project ? '' : engine} threadId={project ? undefined : threadId}
      description={title} label={`${ui(project ? '全部对话 Token 总用量' : '当前对话 Token 用量')}：${number(totals?.totalTokens)}${partial ? ` · ${ui('部分用量')}` : ''}`}>
      {ui(project ? '总用量' : '当前对话 Token')}：<span className="composer-token-usage__value">{number(totals?.totalTokens)}</span>
    </ComposerUsageDetails>
  }
  return <div className="composer-token-usage">
    {threadId ? renderUsage(snapshot?.scope === scope ? snapshot : null, false) : null}
    {renderUsage(projectSnapshot?.scope === token ? projectSnapshot : null, true)}
  </div>
}
