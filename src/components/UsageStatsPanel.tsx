import { useState } from 'react'
import { ui, useUiLanguage } from '../lib/uiLanguage'
import type { CPAUsageSettings, CPAUsageSource } from '../lib/cpaUsageStats'
import { CPAUsageStatsPanel } from './CPAUsageStatsPanel'
import { LocalUsageStatsPanel } from './LocalUsageStatsPanel'

export function UsageStatsPanel(props: {
  settings: CPAUsageSettings
  token: string
  onSourceChange: (source: CPAUsageSource) => void
  onOpenSettings: () => void
  onClose: () => void
}) {
  useUiLanguage()
  const [scope, setScope] = useState<'local' | 'platform'>('local')
  const navigation = <nav className="cpa-usage-range-tabs cpa-usage-source-tabs cpa-usage-panel__sources" aria-label={ui('用量统计范围')}>
    {(['local', 'platform'] as const).map((value) => <button key={value} type="button"
      aria-pressed={scope === value} className={scope === value ? 'cpa-usage-range-tabs__active' : ''}
      onClick={() => setScope(value)}>{ui(value === 'local' ? '本项目用量' : '平台用量')}</button>)}
  </nav>
  return scope === 'local'
    ? <LocalUsageStatsPanel token={props.token} onClose={props.onClose} navigation={navigation} />
    : <CPAUsageStatsPanel {...props} navigation={navigation} />
}
