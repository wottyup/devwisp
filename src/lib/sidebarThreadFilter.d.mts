import type { CodexThread } from './devwispClient'

export type SidebarThreadEngineFilter = 'codex' | 'claude-code' | 'deepseek-harness' | null

export function filterSidebarThreads<T extends CodexThread>(threads: T[], engineFilter?: SidebarThreadEngineFilter, search?: string): T[]
