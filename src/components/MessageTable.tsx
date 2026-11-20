import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import { copyTextToClipboard } from '../lib/clipboard'
import { ui, useUiLanguage } from '../lib/uiLanguage'

export function MessageTable({ children, copyEnabled, source }: { children: ReactNode; copyEnabled: boolean; source: string }) {
  useUiLanguage()
  const scroll = useRef<HTMLDivElement>(null)
  const resetTimer = useRef<number | undefined>(undefined)
  const copyAttempt = useRef(0)
  const [feedback, setFeedback] = useState<'copied' | 'failed' | null>(null)

  useEffect(() => {
    copyAttempt.current += 1
    setFeedback(null)
    if (resetTimer.current !== undefined) window.clearTimeout(resetTimer.current)
    return () => {
      copyAttempt.current += 1
      if (resetTimer.current !== undefined) window.clearTimeout(resetTimer.current)
    }
  }, [source])

  async function copyTable() {
    const table = scroll.current?.querySelector('table')
    if (!table) return
    const attempt = ++copyAttempt.current
    // Copy rendered cell text rather than Markdown syntax or the action label.
    // Tabs retain columns (including empty cells) when pasted into spreadsheets.
    const text = Array.from(table.rows, (row) => Array.from(row.cells,
      (cell) => cell.innerText.replace(/[\t\r\n]+/g, ' ').trim()).join('\t')).join('\n')
    let copied = false
    try {
      copied = await copyTextToClipboard(text)
    } catch {
      // A browser/native clipboard failure must not look like a successful copy.
    }
    if (attempt !== copyAttempt.current) return
    setFeedback(copied ? 'copied' : 'failed')
    if (resetTimer.current !== undefined) window.clearTimeout(resetTimer.current)
    resetTimer.current = window.setTimeout(() => setFeedback(null), 1600)
  }

  return <div className="chat-message__table-wrap">
    <div className="chat-message__table-scroll" ref={scroll}>{children}</div>
    {copyEnabled ? <div className="chat-message__actions chat-message__table-actions">
      <button type="button" onClick={() => void copyTable()} aria-label={ui('复制表格')} title={ui('复制表格')}>
        {feedback === 'copied' ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
        <span role="status">{feedback === 'copied' ? ui('已复制') : feedback === 'failed' ? ui('复制失败') : ui('复制')}</span>
      </button>
    </div> : null}
  </div>
}
