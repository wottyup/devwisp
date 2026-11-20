export interface HarnessUsage {
  inputTokens?: number
  outputTokens?: number
  totalTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  reasoningTokens?: number
}

export interface HarnessChild {
  id: string
  parentId: string
  status: 'running' | 'completed' | 'failed' | 'stopped'
  text: string
  reasoning: string
  usage?: HarnessUsage
  tools: Array<{ id: string; name: string; status: string }>
}

export interface HarnessTelemetry {
  phase: string
  continuity: 'new' | 'reused' | 'restored'
  reasoning: string
  usage: HarnessUsage
  usagePartial?: boolean
  context: { pressureTokens?: number; contextWindow?: number }
  children: HarnessChild[]
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function counters(value: unknown, keys: string[]): Record<string, number> {
  const raw = object(value)
  return Object.fromEntries(keys.flatMap((key) => typeof raw[key] === 'number' && Number.isSafeInteger(raw[key]) && raw[key] >= 0 ? [[key, raw[key]]] : []))
}

const usageKeys = ['inputTokens', 'outputTokens', 'totalTokens', 'cacheReadTokens', 'cacheWriteTokens', 'reasoningTokens']
export const harnessPhaseLabels: Record<string, string> = {
  starting: '正在启动 DeepSeek', thinking: '正在思考', responding: '正在回复', tool: '正在调用工具',
  'waiting-approval': '等待权限批准', 'waiting-answer': '等待你的回答',
  completed: '已完成', failed: '执行失败', stopped: '已停止',
}

export function parseHarnessTelemetry(value: unknown): HarnessTelemetry | undefined {
  const raw = object(value)
  if (typeof raw.phase !== 'string' || !Object.hasOwn(harnessPhaseLabels, raw.phase)) return undefined
  return {
    phase: raw.phase,
    continuity: raw.continuity === 'reused' || raw.continuity === 'restored' ? raw.continuity : 'new',
    reasoning: typeof raw.reasoning === 'string' ? raw.reasoning.slice(-64000) : '',
    usage: counters(raw.usage, usageKeys),
    usagePartial: raw.usagePartial === true,
    context: counters(raw.context, ['pressureTokens', 'contextWindow']),
    children: Array.isArray(raw.children) ? raw.children.slice(0, 100).flatMap((value): HarnessChild[] => {
      const child = object(value)
      if (typeof child.id !== 'string' || typeof child.parentId !== 'string') return []
      return [{ id: child.id, parentId: child.parentId,
        status: child.status === 'running' || child.status === 'failed' || child.status === 'stopped' ? child.status : 'completed',
        text: typeof child.text === 'string' ? child.text.slice(-16000) : '',
        reasoning: typeof child.reasoning === 'string' ? child.reasoning.slice(-16000) : '',
        usage: counters(child.usage, usageKeys),
        tools: Array.isArray(child.tools) ? child.tools.slice(-30).flatMap((value) => {
          const tool = object(value)
          return typeof tool.id === 'string' && typeof tool.name === 'string' ? [{
            id: tool.id, name: tool.name.slice(0, 200), status: tool.status === 'running' || tool.status === 'failed' ? tool.status : 'completed',
          }] : []
        }) : [],
      }]
    }) : [],
  }
}
