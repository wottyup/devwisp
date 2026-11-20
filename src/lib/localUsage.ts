export type UsageEngine = 'codex' | 'claude-code' | 'deepseek-harness'
export const LOCAL_USAGE_PAGE_SIZE = 50
export type LocalUsageRange = 'today' | 'yesterday' | '7d' | '30d' | 'all'

export const usageEngineLabels: Record<UsageEngine, string> = {
  codex: 'Codex', 'claude-code': 'Claude Code', 'deepseek-harness': 'DeepSeek Harness',
}

export interface UsageCounters {
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheWriteTokens: number | null
  reasoningTokens: number | null
  totalTokens: number | null
}

export interface LocalUsageResponse {
  enabledAt: number
  revision?: number
  threadId?: string | null
  totals: UsageCounters & {
    turns: number
    reportedTurns: number | null
    missingTurns: number | null
    partialTurns: number | null
    activeTurns: number | null
  }
  engines: Array<UsageCounters & { engine: UsageEngine; turns: number }>
  models: Array<UsageCounters & { model: string; turns: number }>
  daily: Array<UsageCounters & { day: string; turns: number }>
  turns: Array<UsageCounters & {
    engine: UsageEngine
    threadId: string
    turnId: string
    model: string
    models: string | null
    startedAt: number
    finishedAt: number | null
    status: string
    samples: number
    partial: number
  }>
  modelOptions: string[]
  offset: number
  limit: number
}

export interface LocalUsageUpdate {
  engine: UsageEngine
  threadId: string
  revision: number
  totals: LocalUsageResponse['totals']
}

type UsageListener = (update: LocalUsageUpdate | null) => void
const usageListeners = new Map<string, Set<UsageListener>>()
const usageRevisions = new Map<string, Map<string, number>>()

export function subscribeLocalUsage(token: string, listener: UsageListener): () => void {
  const listeners = usageListeners.get(token) ?? new Set<UsageListener>()
  listeners.add(listener)
  usageListeners.set(token, listeners)
  return () => {
    listeners.delete(listener)
    if (!listeners.size) { usageListeners.delete(token); usageRevisions.delete(token) }
  }
}

/** Existing paired chat sockets carry usage; no extra connection is opened. */
export function publishLocalUsage(token: string, params: Record<string, unknown>): void {
  const { engine, threadId, revision, totals } = params
  const validCount = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
  if (typeof engine !== 'string' || !Object.hasOwn(usageEngineLabels, engine)
    || typeof threadId !== 'string' || !threadId || !validCount(revision)
    || !totals || typeof totals !== 'object' || Array.isArray(totals)) return
  const counters = totals as Record<string, unknown>
  if (!validCount(counters.turns) || !['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens', 'totalTokens', 'reportedTurns', 'missingTurns', 'partialTurns', 'activeTurns']
    .every((key) => counters[key] === null || validCount(counters[key]))) return
  const listeners = usageListeners.get(token)
  if (!listeners?.size) return
  const revisions = usageRevisions.get(token) ?? new Map<string, number>()
  const key = JSON.stringify([engine, threadId])
  if ((revisions.get(key) ?? -1) >= (revision as number)) return
  revisions.delete(key)
  revisions.set(key, revision as number)
  if (revisions.size > 256) revisions.delete(revisions.keys().next().value!)
  usageRevisions.set(token, revisions)
  const update = params as unknown as LocalUsageUpdate
  for (const listener of listeners) listener(update)
}

export function refreshLocalUsage(token: string): void {
  for (const listener of usageListeners.get(token) ?? []) listener(null)
}

export interface LocalUsageFilter {
  range: LocalUsageRange
  engine: UsageEngine | ''
  model: string
  offset: number
  threadId?: string
}

export function localUsageQuery(filter: LocalUsageFilter, now = new Date()): string {
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const end = new Date(now)
  if (filter.range === 'yesterday') {
    end.setTime(start.getTime())
    start.setDate(start.getDate() - 1)
  } else if (filter.range === '7d' || filter.range === '30d') {
    start.setDate(start.getDate() - (filter.range === '7d' ? 6 : 29))
  }
  const query = new URLSearchParams({
    since: String(filter.range === 'all' ? 0 : start.getTime()),
    until: String(end.getTime() + (filter.range === 'yesterday' ? 0 : 1)),
    offset_minutes: String(now.getTimezoneOffset()), offset: String(filter.offset), limit: String(LOCAL_USAGE_PAGE_SIZE),
  })
  if (filter.engine) query.set('engine', filter.engine)
  if (filter.model) query.set('model', filter.model)
  if (filter.threadId) query.set('thread_id', filter.threadId)
  return query.toString()
}
