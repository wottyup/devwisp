import type { CollaborationEngine } from '../lib/devwispClient'
import { ui } from '../lib/uiLanguage'

const names = { codex: 'Codex', 'claude-code': 'Claude Code', 'deepseek-harness': 'DeepSeek Harness' }
const roles: Record<string, string> = { coder: '实现者', reviewer: '审查者' }

export function CollaborationModelIcons({ members }: { members: Record<string, { engine: CollaborationEngine; model: string; name?: string }> }) {
  return <span className="collaboration-model-icons">
    {Object.entries(members).map(([role, member]) => {
      const label = `${member.name || ui(roles[role] ?? role)}: ${names[member.engine]} · ${member.model || ui('默认模型')}`
      return <span key={role} className="collaboration-model-icons__member" role="img" aria-label={label} title={label}>
        <span className={`engine-brand-icon engine-brand-icon--${member.engine}`} aria-hidden="true" />
      </span>
    })}
  </span>
}
