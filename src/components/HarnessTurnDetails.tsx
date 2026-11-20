import type { HarnessTelemetry, HarnessUsage } from '../lib/harnessTelemetry'
import { harnessPhaseLabels } from '../lib/harnessTelemetry'
import { ui, getUiLanguage, useUiLanguage } from '../lib/uiLanguage'

function Usage({ value }: { value: HarnessUsage }) {
  const entries: Array<[keyof HarnessUsage, string]> = [
    ['inputTokens', '输入（未缓存）'], ['outputTokens', '输出'], ['cacheReadTokens', '缓存读取'],
    ['cacheWriteTokens', '缓存写入'], ['reasoningTokens', '思考 Token'], ['totalTokens', '总计'],
  ]
  return <span className="harness-turn__usage">{entries.flatMap(([key, label]) => value[key] === undefined ? [] : [
    <span key={key}>{ui(label)} {value[key]!.toLocaleString(getUiLanguage())}</span>,
  ])}</span>
}

export function HarnessTurnDetails({ value, streaming, onToggleDetails }: { value: HarnessTelemetry; streaming?: boolean; onToggleDetails?: () => void }) {
  useUiLanguage()
  const { pressureTokens, contextWindow } = value.context
  return <div className="harness-turn">
    <details className="harness-turn__metrics">
      <summary onClick={onToggleDetails}>{ui('运行详情')}</summary>
      <div className="harness-turn__metrics-content">
        <div className="harness-turn__status">
          <span role={streaming ? 'status' : undefined}>{ui(harnessPhaseLabels[value.phase] ?? '正在处理')}</span>
          {value.continuity === 'restored' ? <span title={ui('对话记录已恢复；原进程结束或配置变化后，内部 shell 状态需要重新建立。')}>{ui('进程已重建')}</span>
            : value.continuity === 'reused' ? <span>{ui('进程已复用')}</span> : null}
        </div>
        {Object.keys(value.usage).length ? <div title={ui('主 Agent 本轮已报告的用量；子任务单独列出。思考 Token 已包含在输出中。')}><Usage value={value.usage} />{value.usagePartial ? <span>{ui('部分请求未报告用量')}</span> : null}</div> : null}
        {pressureTokens !== undefined ? <div>{ui('最近请求上下文')} {pressureTokens.toLocaleString(getUiLanguage())}{contextWindow ? ` / ${contextWindow.toLocaleString(getUiLanguage())} (${Math.round(100 * pressureTokens / contextWindow)}%)` : ''} Token</div> : null}
      </div>
    </details>
    {value.reasoning ? <details className="harness-turn__reasoning"><summary onClick={onToggleDetails}>{ui('思考内容（接口返回）')}</summary><pre>{value.reasoning}</pre></details> : null}
    {value.children.length ? <details className="harness-turn__children"><summary onClick={onToggleDetails}>{ui('子 Agent')} · {value.children.length}</summary>
      {value.children.map((child, index) => <section key={child.id}>
        <div className="harness-turn__status"><strong>{ui('子任务')} {index + 1}</strong><span>{ui(child.status === 'running' ? '正在运行' : child.status === 'failed' ? '执行失败' : child.status === 'stopped' ? '已停止' : '已完成')}</span></div>
        {child.usage ? <Usage value={child.usage} /> : null}
        {child.tools.map((tool) => <div className="harness-turn__status" key={tool.id}><span>{tool.name}</span><span>{ui(tool.status === 'running' ? '正在运行' : tool.status === 'failed' ? '执行失败' : '已完成')}</span></div>)}
        {child.reasoning ? <details><summary onClick={onToggleDetails}>{ui('思考内容（接口返回）')}</summary><pre>{child.reasoning}</pre></details> : null}
        {child.text ? <pre>{child.text}</pre> : null}
      </section>)}
    </details> : null}
  </div>
}
