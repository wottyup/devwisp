import { useEffect, useState, type ReactNode } from 'react'
import { Activity, ChartLine, ChevronLeft, ChevronRight, RefreshCw, X } from 'lucide-react'
import { devwispClient } from '../lib/devwispClient'
import { UsageTrendChart } from './UsageTrendChart'
import { TranslationSelect } from './TranslationSelect'
import { getUiLanguage, ui, uiFormat, uiMessage, useUiLanguage } from '../lib/uiLanguage'
import { LOCAL_USAGE_PAGE_SIZE, subscribeLocalUsage, usageEngineLabels, type LocalUsageRange, type LocalUsageResponse, type UsageCounters, type UsageEngine } from '../lib/localUsage'

function count(value: number | null | undefined) {
  return value == null ? '—' : value.toLocaleString(getUiLanguage())
}

function date(value: number) {
  return new Date(value).toLocaleString(getUiLanguage(), { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const ranges: Array<[LocalUsageRange, string]> = [['today', '今天'], ['yesterday', '昨天'], ['7d', '近 7 天'], ['30d', '近 30 天'], ['all', '全部']]

function Breakdown({ title, rows }: { title: string; rows: Array<UsageCounters & { label: string; turns: number }> }) {
  return <section className="theme-settings-card cpa-usage-section">
    <div className="theme-settings-card__heading"><div><h2>{ui(title)}</h2></div></div>
    <div className="cpa-usage-table-wrap"><table className="cpa-usage-table">
      <thead><tr><th>{ui('名称')}</th><th>{ui('总 Token')}</th><th>{ui('输入')}</th><th>{ui('输出')}</th><th>{ui('轮次')}</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={row.label}><td title={row.label}>{row.label === 'unknown' ? ui('未提供模型') : row.label}</td>
        <td>{count(row.totalTokens)}</td><td>{count(row.inputTokens)}</td><td>{count(row.outputTokens)}</td><td>{count(row.turns)}</td></tr>)}</tbody>
    </table></div>
  </section>
}

export function LocalUsageStatsPanel({ token, onClose, navigation }: { token: string; onClose: () => void; navigation: ReactNode }) {
  useUiLanguage()
  const [range, setRange] = useState<LocalUsageRange>('today')
  const [engine, setEngine] = useState<UsageEngine | ''>('')
  const [model, setModel] = useState('')
  const [offset, setOffset] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const [data, setData] = useState<LocalUsageResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)

  useEffect(() => {
    let active = true
    let controller: AbortController | undefined
    setData(null)
    setUpdatedAt(null)
    const load = async () => {
      controller?.abort()
      const pending = new AbortController()
      controller = pending
      setLoading(true)
      setError(null)
      try {
        const response = await devwispClient.localUsage(token, { range, engine, model, offset }, pending.signal)
        if (!active || pending.signal.aborted) return
        setData(response)
        setUpdatedAt(Date.now())
      } catch (reason) {
        if (active && !pending.signal.aborted) setError(reason instanceof Error ? reason.message : '读取本项目用量失败。')
      } finally {
        if (active && !pending.signal.aborted) setLoading(false)
      }
    }
    void load()
    const timer = window.setInterval(() => { if (document.visibilityState === 'visible') void load() }, 30_000)
    let updateTimer: number | undefined
    const unsubscribe = subscribeLocalUsage(token, (update) => {
      if (document.visibilityState !== 'visible' || update && engine && update.engine !== engine) return
      if (updateTimer !== undefined) return
      updateTimer = window.setTimeout(() => { updateTimer = undefined; void load() }, 100)
    })
    const visible = () => { if (document.visibilityState === 'visible') void load() }
    document.addEventListener('visibilitychange', visible)
    return () => { active = false; unsubscribe(); window.clearTimeout(updateTimer); controller?.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', visible) }
  }, [token, range, engine, model, offset, refresh])

  const totals = data?.totals
  const days = data?.daily ?? []
  const selectedRows = data?.turns ?? []
  const statusLabel = (row: LocalUsageResponse['turns'][number]) => row.status === 'inProgress' ? ui('进行中')
    : row.status === 'failed' ? ui('失败') : row.status === 'stopped' || row.status === 'interrupted' ? ui('已停止') : ui('已完成')
  const coverageLabel = (row: LocalUsageResponse['turns'][number]) => row.totalTokens == null
    ? row.inputTokens != null || row.outputTokens != null ? ui('部分用量') : row.status === 'inProgress' ? ui('等待回传') : ui('未提供用量')
    : row.partial > 0 ? ui('部分用量') : ui('已记录')
  const metrics: Array<[string, number | null | undefined, string]> = [
    ['已记录 Token', totals?.totalTokens, uiFormat('{0} 轮已回传用量', count(totals?.reportedTurns))],
    ['输入', totals?.inputTokens, ui('包含缓存读取和写入')],
    ['输出', totals?.outputTokens, ui('包含已回传的思考 Token')],
    ['缓存读取', totals?.cacheReadTokens, ui('输入中的其中项，不重复累加')],
    ['缓存写入', totals?.cacheWriteTokens, ui('输入中的其中项，不重复累加')],
    ['思考 Token', totals?.reasoningTokens, ui('输出中的其中项，不重复累加')],
  ]
  return <main className="theme-settings-panel cpa-usage-panel local-usage-panel" aria-label={ui('Token 用量统计')}>
    <header className="theme-settings-panel__header"><div>
      <span className="theme-settings-panel__eyebrow">TOKEN USAGE</span><h1>{ui('Token 用量统计')}</h1>
      <p>{ui('统计通过 Dev Wisp 聊天产生的用量。')}</p>
    </div><div className="cpa-usage-panel__header-actions">
      <button className="icon-button theme-settings-panel__close" type="button" aria-label={ui('刷新统计')} title={ui('刷新统计')} disabled={loading} onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={16} className={loading ? 'spin' : undefined} /></button>
      <button className="icon-button theme-settings-panel__close" type="button" aria-label={ui('返回对话')} onClick={onClose}><X size={18} /></button>
    </div></header>
    <div className="theme-settings-panel__content cpa-usage-panel__content">
      {navigation}
      <section className="cpa-usage-toolbar"><div className="cpa-usage-toolbar__controls">
        <div className="cpa-usage-range-tabs cpa-usage-source-tabs" role="group" aria-label={ui('时间范围')}>
          {ranges.map(([value, label]) => <button type="button" key={value} aria-pressed={range === value}
            className={range === value ? 'cpa-usage-range-tabs__active' : ''} onClick={() => { setRange(value); setOffset(0) }}>{ui(label)}</button>)}
        </div>
        <TranslationSelect label={ui('引擎')} value={engine} className="cpa-usage-key-filter" menuWidth={208}
          options={[{ value: '', label: ui('全部') }, ...Object.entries(usageEngineLabels).map(([value, label]) => ({ value, label }))]}
          onChange={(value) => { setEngine(value as UsageEngine | ''); setOffset(0) }} />
        <TranslationSelect label={ui('模型')} value={model} className="cpa-usage-key-filter" menuWidth={260}
          options={[{ value: '', label: ui('全部') }, ...[...new Set([...(data?.modelOptions ?? []), ...(model ? [model] : [])])].map((value) => ({ value, label: value === 'unknown' ? ui('未提供模型') : value }))]}
          onChange={(value) => { setModel(value); setOffset(0) }} />
      </div>{updatedAt ? <span>{ui('更新于')} {date(updatedAt)}</span> : null}</section>
      {error ? <div className="cpa-usage-error" role="alert"><strong>{ui('读取失败')}</strong><span>{uiMessage(error)}</span><button type="button" onClick={() => setRefresh((value) => value + 1)}>{ui('重试')}</button></div> : null}
      {loading && !data ? <div className="cpa-usage-loading" role="status"><RefreshCw size={16} className="spin" />{ui('正在读取统计…')}</div> : null}
      {data && totals ? <>
        <p className="theme-settings-hint local-usage-coverage" role="status">{uiFormat('共 {0} 轮 · 未提供 {1} 轮 · 部分用量 {2} 轮 · 进行中 {3} 轮', count(totals.turns), count(totals.missingTurns ?? 0), count(totals.partialTurns ?? 0), count(totals.activeTurns ?? 0))}</p>
        {totals.turns === 0 ? <div className="cpa-usage-empty">{ui('这个时间范围还没有聊天记录。通过本项目发送消息后，用量会自动记录，无需配置统计平台。')}</div> : <>
          <section className="cpa-usage-summary-grid" aria-label={ui('Token 用量概览')}>
            {metrics.map(([label, value, hint], index) => <article key={label} className={`theme-settings-card cpa-usage-summary-card${index === 0 ? ' cpa-usage-summary-card--primary' : ''}`}>
              <span>{index === 0 ? <Activity size={14} /> : null}{ui(label)}</span><strong>{count(value)}</strong><small>{hint}</small>
            </article>)}
          </section>
          <section className="theme-settings-card cpa-usage-section"><div className="theme-settings-card__heading"><div><h2><ChartLine size={16} />{ui('Token 趋势')}</h2><p>{ui('按轮次开始日期汇总，使用当前设备时区。')}</p></div></div>
            <div className="cpa-usage-section__body"><UsageTrendChart points={days.map((day) => ({
              id: day.day, time: Date.parse(day.day), label: day.day, shortLabel: day.day.slice(5),
              totalTokens: day.totalTokens, inputTokens: day.inputTokens, outputTokens: day.outputTokens,
            }))} /></div>
          </section>
          <div className="cpa-usage-breakdown-grid">
            <Breakdown title="按引擎统计" rows={data.engines.map((row) => ({ ...row, label: usageEngineLabels[row.engine] }))} />
            <Breakdown title="按模型统计" rows={data.models.map((row) => ({ ...row, label: row.model }))} />
          </div>
          <section className="theme-settings-card cpa-usage-section"><div className="theme-settings-card__heading"><div><h2>{ui('轮次明细')}</h2><p>{ui('失败或停止的轮次，已回传的消耗仍会保留。')}</p></div></div>
            <div className="cpa-usage-table-wrap"><table className="cpa-usage-table"><thead><tr>
              {['对话 / 模型', '开始时间', '状态', '用量记录', '总 Token', '输入', '输出', '缓存读取', '缓存写入', '思考 Token'].map((label) => <th key={label}>{ui(label)}</th>)}
            </tr></thead><tbody>{selectedRows.map((row) => <tr key={`${row.engine}:${row.threadId}:${row.turnId}`}>
              <td><button className="local-usage-thread-link" type="button" title={row.threadId} onClick={() => window.DevWispOpenThread?.(row.threadId)}>{usageEngineLabels[row.engine]} · {row.threadId.replace(/^(claude|deepseek):/, '').slice(0, 8)}</button>
                <small title={row.models ?? row.model}>{!row.models || row.models === 'unknown' ? row.model === 'unknown' || row.model === 'default' ? ui('未提供模型') : row.model : row.models}</small></td>
              <td>{date(row.startedAt)}</td><td>{statusLabel(row)}</td><td>{coverageLabel(row)}</td><td>{count(row.totalTokens)}</td><td>{count(row.inputTokens)}</td><td>{count(row.outputTokens)}</td><td>{count(row.cacheReadTokens)}</td><td>{count(row.cacheWriteTokens)}</td><td>{count(row.reasoningTokens)}</td>
            </tr>)}</tbody></table></div>
            <div className="local-usage-pagination"><span>{uiFormat('第 {0}–{1} 轮，共 {2} 轮', Math.min(offset + 1, totals.turns), Math.min(offset + selectedRows.length, totals.turns), count(totals.turns))}</span>
              <button type="button" className="icon-button" aria-label={ui('上一页')} disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - LOCAL_USAGE_PAGE_SIZE))}><ChevronLeft size={16} /></button>
              <button type="button" className="icon-button" aria-label={ui('下一页')} disabled={offset + selectedRows.length >= totals.turns || loading} onClick={() => setOffset(offset + LOCAL_USAGE_PAGE_SIZE)}><ChevronRight size={16} /></button>
            </div>
          </section>
        </>}
        <p className="theme-settings-hint cpa-usage-disclaimer">{uiFormat('从 {0} 起记录本项目发起的聊天。', new Date(data.enabledAt).toLocaleString(getUiLanguage()))} {ui('仅统计引擎实际回传的用量；未提供的字段显示为 —。历史 CLI 聊天不会导入，平台账单不会重复相加。')}</p>
      </> : null}
    </div>
  </main>
}
