import { QuestionFields } from './QuestionFields'
import { QuestionAnswerImages, useQuestionAnswerImages } from './QuestionAnswerImages'
import { useRef, useState } from 'react'
import { Check, X } from 'lucide-react'
import { ui, useUiLanguage } from '../lib/uiLanguage'
import type { HarnessQuestionRequest, HarnessQuestionResponse } from '../lib/devwispClient'

interface Props {
  token: string
  request: HarnessQuestionRequest
  onRespond: (id: string, response: HarnessQuestionResponse) => Promise<boolean>
}

export function HarnessQuestionCard({ token, request, onRespond }: Props) {
  useUiLanguage()
  const [answers, setAnswers] = useState<Record<string, { selected: string[]; custom?: string }>>({})
  const [submitting, setSubmitting] = useState(false)
  const [customOpen, setCustomOpen] = useState<Record<string, boolean>>({})
  const composing = useRef(false)
  const compositionEndedAt = useRef(0)
  const [error, setError] = useState('')
  const inFlight = useRef(false)
  const images = useQuestionAnswerImages()
  const respond = async (cancelled = false) => {
    if (inFlight.current || images.isBusy() || (!cancelled && !complete)) return
    inFlight.current = true
    setSubmitting(true)
    setError('')
    try {
      const delivered = await onRespond(request.questionId, cancelled ? { cancelled: true } : {
        answers: await Promise.all(request.questions.map(async (q) => {
          const customActive = q.intent?.kind !== 'approval' && (!q.options?.length || customOpen[q.id])
          const custom = customActive ? await images.withReferences(token, q.id, answers[q.id]?.custom ?? '') : ''
          return { id: q.id, selected: answers[q.id]?.selected ?? [], ...(custom ? { custom } : {}) }
        })),
      })
      if (!delivered) setError(ui('问题已结束，请刷新后重试。'))
    } catch (reason) { setError(reason instanceof Error ? reason.message : ui('回答没有送达，请重试。')) }
    finally { inFlight.current = false; setSubmitting(false) }
  }
  const complete = request.questions.length > 0 && request.questions.every((q) =>
    customOpen[q.id] && !q.multiSelect ? Boolean(answers[q.id]?.custom?.trim() || images.has(q.id))
      : Boolean(answers[q.id]?.selected.length || answers[q.id]?.custom?.trim() || (!q.options?.length || customOpen[q.id]) && images.has(q.id)))
  return <form className="message-choices" aria-label={ui('DeepSeek 提问与计划审核')} onSubmit={(event) => { event.preventDefault(); void respond() }} onKeyDown={(event) => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); void respond(true) }
  }}>
    <div className="message-choices__heading"><strong>{request.questions.some((q) => q.intent?.kind === 'plan-review') ? ui('DeepSeek 计划审核') : ui('快速选择')}</strong></div>
    <div className="message-choices__groups">
      {request.questions.map((q) => <QuestionFields key={q.id} title={q.header || q.question}
        description={q.header ? q.question : undefined} detail={q.detail} multiple={q.multiSelect} disabled={submitting || images.busy}
        options={[...(q.options ?? []).map((option) => ({ value: option.label, ...option })),
          ...(q.intent?.kind !== 'approval' && q.options?.length ? [{ value: '\u0000other', label: ui('其他答案（没有合适选项时填写）') }] : [])]}
        selected={[...(answers[q.id]?.selected ?? []), ...(customOpen[q.id] ? ['\u0000other'] : [])]}
        onToggle={(value) => {
          if (value === '\u0000other') {
            const open = !customOpen[q.id]
            setCustomOpen((current) => ({ ...current, [q.id]: open }))
            setAnswers((current) => ({ ...current, [q.id]: {
              selected: q.multiSelect ? current[q.id]?.selected ?? [] : [],
              custom: open ? current[q.id]?.custom ?? '' : '',
            } }))
            return
          }
          if (!q.multiSelect) setCustomOpen((current) => ({ ...current, [q.id]: false }))
          setAnswers((current) => ({ ...current, [q.id]: {
          selected: q.multiSelect
            ? current[q.id]?.selected.includes(value) ? current[q.id].selected.filter((item) => item !== value) : [...(current[q.id]?.selected ?? []), value]
            : current[q.id]?.selected.includes(value) ? [] : [value],
          custom: q.multiSelect ? current[q.id]?.custom : '',
        } }))}}>
        {q.intent?.kind !== 'approval' && (!q.options?.length || customOpen[q.id]) ? <label className="message-choices__custom-label">{ui('自定义回答 / 反馈')}
          <textarea className="message-choices__custom" value={answers[q.id]?.custom ?? ''} maxLength={30000} rows={2}
            disabled={submitting || images.busy} onPaste={(event) => images.paste(q.id, event)}
            aria-keyshortcuts="Enter" enterKeyHint="send"
            onCompositionStart={() => { composing.current = true }}
            onCompositionEnd={() => { composing.current = false; compositionEndedAt.current = Date.now() }}
            onBlur={() => { composing.current = false }}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey
                || composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return
              event.preventDefault(); event.stopPropagation()
              if (!event.repeat && Date.now() - compositionEndedAt.current >= 100) void respond()
            }} onChange={(event) => {
            const custom = event.target.value
            setAnswers((current) => ({ ...current, [q.id]: { selected: !q.multiSelect && custom.trim() ? [] : current[q.id]?.selected ?? [], custom } }))
          }} />
          <QuestionAnswerImages images={images.get(q.id)} disabled={submitting || images.busy} onRemove={(index) => images.remove(q.id, index)} />
          <small className="message-choices__custom-hint">{ui('Enter 发送 · Shift+Enter 换行')} · {ui('支持粘贴图片')}</small>
        </label> : null}
      </QuestionFields>)}
    </div>
    {error || images.error ? <p role="alert">{error || images.error}</p> : null}
      <div className="message-choices__footer">
        <button className="message-choices__submit" type="submit" disabled={submitting || images.busy || !complete}><Check size={14} />{submitting ? ui('正在提交…') : request.questions.some((q) => q.intent?.kind === 'plan-review') ? ui('提交审核结果') : ui('发送并继续')}</button>
        <button type="button" disabled={submitting} onClick={() => void respond(true)}><X size={14} />{ui('取消')}</button>
      </div>
  </form>
}
