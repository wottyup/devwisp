import type { CodexThread, CollaborationEngine, CollaborationTask } from './devwispClient'

export interface SidebarConversation extends CodexThread {
  collaboration?: CollaborationTask
  participantEngines?: CollaborationEngine[]
}

export function mergeSidebarConversations(threads: CodexThread[], tasks: CollaborationTask[]): SidebarConversation[]
