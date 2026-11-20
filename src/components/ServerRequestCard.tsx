import { QuestionFields } from './QuestionFields'
import { QuestionAnswerImages, useQuestionAnswerImages } from './QuestionAnswerImages'
import { ui, useUiLanguage } from '../lib/uiLanguage'
import { useRef, useState } from 'react'
import { AlertTriangle, Check, X } from 'lucide-react'
import type { MCPApprovalScope } from '../lib/devwispClient'

type Decision = 'accept' | 'decline' | 'cancel'
type RequestResponse = Decision | Record<string, unknown>

interface ServerRequest {
  requestId: string | number
  method: string
  params: Record<string, unknown>
  approvalScope?: MCPApprovalScope | null
}

interface Props {
  token: string
  request: ServerRequest
  source: string | null
  sourceIsCurrent: boolean
  onOpenSource: () => void
  onRespond: (response: RequestResponse, remember?: 'tool' | 'server') => boolean
}

interface ToolQuestion {
  id: string
  header: string
  question: string
  options: Array<{ label: string; description: string }>
  isOther: boolean
  isSecret: boolean
}

const otherChoice = '\u0000other'

function objectValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function toolQuestions(value: unknown): ToolQuestion[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    const question = objectValue(item)
    if (!question || typeof question.id !== 'string' || typeof question.question !== 'string') return []
    const options = Array.isArray(question.options) ? question.options.flatMap((entry) => {
      const option = objectValue(entry)
      return option && typeof option.label === 'string'
        ? [{ label: option.label, description: typeof option.description === 'string' ? option.description : '' }]
        : []
    }) : []
    return [{
      id: question.id,
      header: typeof question.header === 'string' ? question.header : '',
      question: question.question,
      options,
      isOther: question.isOther === true,
      isSecret: question.isSecret === true,
    }]
  })
}

function ToolQuestionFields({ token, questions, onRespond }: { token: string; questions: ToolQuestion[]; onRespond: Props['onRespond'] }) {
  useUiLanguage()
  const [selected, setSelected] = useState<Record<string, string>>({})
  const [custom, setCustom] = useState<Record<string, string>>({})
  const images = useQuestionAnswerImages()
  const [sending, setSending] = useState(false)
  const inFlight = useRef(false)
  const [error, setError] = useState('')
  const complete = questions.length > 0 && questions.every((question) => {
    const answer = selected[question.id]
    return !question.options.length ? Boolean(custom[question.id]?.trim() || images.has(question.id))
      : answer && (answer !== otherChoice || Boolean(custom[question.id]?.trim() || images.has(question.id)))
  })

  const submit = async () => {
    if (!complete || images.isBusy() || inFlight.current) return
    inFlight.current = true
    setSending(true)
    setError('')
    try {
      const answers = Object.fromEntries(await Promise.all(questions.map(async (question) => [question.id, {
        answers: [selected[question.id] === otherChoice || !question.options.length
          ? await images.withReferences(token, question.id, custom[question.id] ?? '') : selected[question.id]],
      }])))
      if (!onRespond({ answers })) setError(ui('回答没有送达，请重试。'))
    } catch (reason) { setError(reason instanceof Error ? reason.message : ui('回答没有送达，请重试。')) }
    finally { inFlight.current = false; setSending(false) }
  }

  return <>
    {questions.length ? questions.map((question) => <QuestionFields key={question.id} title={question.header || ui('请选择')}
      disabled={sending || images.busy}
      description={question.question}
      options={[...question.options.map((option) => ({ value: option.label, ...option })),
        ...(question.isOther && question.options.length ? [{ value: otherChoice, label: ui('其他') }] : [])]}
      selected={selected[question.id] ? [selected[question.id]] : []}
      onToggle={(value) => setSelected((current) => ({ ...current, [question.id]: current[question.id] === value ? '' : value }))}>
      {(question.isOther && selected[question.id] === otherChoice) || !question.options.length ? <><input
        className="message-choices__custom"
        type={question.isSecret ? 'password' : 'text'}
        aria-label={question.question}
        value={custom[question.id] ?? ''}
        disabled={sending || images.busy}
        onPaste={question.isSecret ? undefined : (event) => images.paste(question.id, event)}
        onChange={(event) => {
          const value = event.target.value
          setCustom((current) => ({ ...current, [question.id]: value }))
          if (!question.options.length) setSelected((current) => ({ ...current, [question.id]: otherChoice }))
        }}
      /><QuestionAnswerImages images={images.get(question.id)} disabled={sending || images.busy} onRemove={(index) => images.remove(question.id, index)} />
      {!question.isSecret ? <small className="message-choices__custom-hint">{ui('支持粘贴图片')}</small> : null}</> : null}
    </QuestionFields>) : <p>{ui("工具问题格式无法显示，可以取消这次请求。")}</p>}
    {error || images.error ? <p role="alert">{error || images.error}</p> : null}
    <div className="message-choices__footer">
      <button type="button" disabled={sending} onClick={() => onRespond({ answers: {} })}><X size={14} />{ui("取消")}</button>
      <button type="button" disabled={!complete || sending || images.busy} onClick={() => void submit()}><Check size={14} />{ui("发送并继续")}</button>
    </div>
  </>
}

function McpElicitationFields({ params, approvalScope, onRespond }: { params: Record<string, unknown>; approvalScope?: MCPApprovalScope | null; onRespond: Props['onRespond'] }) {
  useUiLanguage()
  const [values, setValues] = useState<Record<string, string | boolean>>({})
  const schema = objectValue(params.requestedSchema)
  const properties = objectValue(schema?.properties)
  const fields = properties ? Object.entries(properties) : []
  const required = Array.isArray(schema?.required) ? schema.required.filter((value): value is string => typeof value === 'string') : []
  const formMode = params.mode === 'form' || params.mode === 'openai/form' || params.mode === 'openaiForm'
  const supported = formMode && (params.requestedSchema === true || schema !== null) && fields.every(([, value]) => {
    const field = objectValue(value)
    return field !== null && ['string', 'number', 'integer', 'boolean'].includes(String(field.type))
  })
  const complete = supported && required.every((name) => {
    const field = objectValue(properties?.[name])
    return field?.type === 'boolean' || String(values[name] ?? '').trim().length > 0
  }) && fields.every(([name, definition]) => {
    const field = objectValue(definition)
    const value = values[name]
    return value === undefined || value === '' || (field?.type !== 'number' && field?.type !== 'integer')
      || (Number.isFinite(Number(value)) && (field.type !== 'integer' || Number.isInteger(Number(value))))
  })

  const accept = () => {
    if (!complete) return
    const content: Record<string, unknown> = {}
    for (const [name, definition] of fields) {
      const field = objectValue(definition)
      const value = values[name]
      if (field?.type === 'boolean') {
        content[name] = name === 'confirm' ? true : value === true
      } else if (value !== undefined && String(value).trim()) {
        content[name] = field?.type === 'integer' || field?.type === 'number' ? Number(value) : value
      }
    }
    onRespond({ action: 'accept', content })
  }

  return <>
    {typeof params.serverName === 'string' ? <small>{ui("服务器：")}{params.serverName}</small> : null}
    <p>{typeof params.message === 'string' ? params.message : typeof params.description === 'string' ? params.description : ui('MCP 服务器请求你确认当前操作。')}</p>
    {fields.map(([name, definition]) => {
      const field = objectValue(definition)
      if (!field || !['string', 'number', 'integer', 'boolean'].includes(String(field.type))) return null
      if (name === 'confirm' && field.type === 'boolean') return null
      const label = typeof field.title === 'string' ? field.title : name
      const choices = Array.isArray(field.enum) ? field.enum.filter((value): value is string => typeof value === 'string') : []
      return <label className="approval-card__field" key={name}>
        <span>{label}{required.includes(name) ? ' *' : ''}</span>
        {field.type === 'boolean' ? <input type="checkbox" checked={values[name] === true} onChange={(event) => setValues((current) => ({ ...current, [name]: event.target.checked }))} />
          : choices.length ? <select value={String(values[name] ?? '')} onChange={(event) => setValues((current) => ({ ...current, [name]: event.target.value }))}>
            <option value="">{ui("请选择")}</option>{choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
          </select>
            : <input type={field.type === 'number' || field.type === 'integer' ? 'number' : field.format === 'password' ? 'password' : 'text'} step={field.type === 'integer' ? '1' : 'any'} value={String(values[name] ?? '')} onChange={(event) => setValues((current) => ({ ...current, [name]: event.target.value }))} />}
        {typeof field.description === 'string' ? <small>{field.description}</small> : null}
      </label>
    })}
    {!supported ? <p>{ui("当前网页无法确认这种 MCP 验证请求，请取消后改用受支持的验证方式。")}</p> : null}
    {approvalScope ? <small>{ui("同类型指")} {approvalScope.serverName} {ui("的")} {approvalScope.toolName}{ui("；服务器级授权涵盖它的所有工具。")}</small> : null}
    <div className="approval-card__actions">
      <button type="button" onClick={() => onRespond({ action: 'decline' })}><X size={14} />{ui("拒绝")}</button>
      {supported ? <button className="approval-card__accept" type="button" disabled={!complete} onClick={accept}><Check size={14} />{ui("允许一次")}</button> : null}
      {supported && complete && approvalScope ? <>
        <button className="approval-card__accept" type="button" onClick={() => onRespond({ action: 'accept', content: {} }, 'tool')}><Check size={14} />{ui("此工具始终允许")}</button>
        <button className="approval-card__accept" type="button" onClick={() => onRespond({ action: 'accept', content: {} }, 'server')}><Check size={14} />{ui("该 MCP 服务器始终允许")}</button>
      </> : null}
    </div>
  </>
}

export function ServerRequestCard({ token, request, source, sourceIsCurrent, onOpenSource, onRespond }: Props) {
  useUiLanguage()
  const isMcp = request.method === 'mcpServer/elicitation/request'
  const isToolQuestion = request.method === 'item/tool/requestUserInput'
  const title = isMcp ? ui('MCP 工具需要你的批准') : isToolQuestion ? ui('Codex 需要你的回答') : ui('Codex 需要你的批准')
  return <div className="approval-card" role="alert">
    <div className="approval-card__heading"><AlertTriangle size={17} /><strong>{title}</strong></div>
    {source ? <button className="approval-card__source" type="button" onClick={onOpenSource} disabled={sourceIsCurrent} title={sourceIsCurrent ? ui('来自当前对话') : ui('打开请求审批的对话')}>{ui("来自：")}{source}</button> : null}
    {isMcp ? <McpElicitationFields params={request.params} approvalScope={request.approvalScope} onRespond={onRespond} />
      : isToolQuestion ? <ToolQuestionFields token={token} questions={toolQuestions(request.params.questions)} onRespond={onRespond} />
        : <>
          <p>{typeof request.params.reason === 'string' ? request.params.reason : ui('Codex 请求执行一项需要批准的操作。')}</p>
          {typeof request.params.command === 'string' ? <code>{request.params.command}</code> : null}
          {typeof request.params.cwd === 'string' ? <small>{ui("目录：")}{request.params.cwd}</small> : null}
          <div className="approval-card__actions">
            <button type="button" onClick={() => onRespond('decline')}><X size={14} />{ui("拒绝")}</button>
            <button className="approval-card__accept" type="button" onClick={() => onRespond('accept')}><Check size={14} />{ui("允许一次")}</button>
          </div>
        </>}
  </div>
}
