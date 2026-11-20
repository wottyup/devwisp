import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode, type RefObject } from 'react'
import { FolderOpen, LoaderCircle, Menu, Plus, Terminal, UsersRound, X } from 'lucide-react'
import { devwispClient, RemoteApiError, type CodexDocumentAttachment, type CodexThread, type CollaborationEngine, type CollaborationMember, type CollaborationTask } from '../lib/devwispClient'
import { collaborationId } from '../lib/collaborationId'
import { CollaborationModelIcons } from './CollaborationModelIcons'
import { ClaudeReplyText } from './ClaudeReplyText'
import { ImagePreviewButton } from './ImagePreviewButton'
import type { ComposerHandle } from './DevWispPage'
import { ui, useUiLanguage } from '../lib/uiLanguage'
import './collaboration.css'

interface Props {
  token: string; visible: boolean; selectedId: string | null
  onSelect: (id: string | null) => void; onRefresh: () => Promise<void>
  onCreate: () => void; creating: boolean
  onOpenChat: (thread?: CodexThread) => void; onUnauthorized: () => void
  onOpenSidebar: () => void; onOpenWorkspace: () => void; onOpenTerminal: () => void
  onDraftCwdChange: (cwd: string) => void
  renderComposer: (task: CollaborationTask, ref: RefObject<ComposerHandle | null>, refresh: () => Promise<void>) => ReactNode
}
const engines: Array<{ id: CollaborationEngine; name: string }> = [
  { id: 'codex', name: 'Codex' }, { id: 'claude-code', name: 'Claude' }, { id: 'deepseek-harness', name: 'DeepSeek' },
]
const roles: Record<string, string> = { coder: '实现者', reviewer: '审查者', system: '任务动态', human: '你' }
export interface CollaborationHandle {
  appendText: (text: string, focus?: boolean) => void
  addDocument: (document: CodexDocumentAttachment, focus?: boolean) => boolean
}

export const CollaborationPage = forwardRef<CollaborationHandle, Props>(function CollaborationPage({ token, visible, selectedId, onRefresh, onCreate, creating, onOpenChat, onUnauthorized, onOpenSidebar, onOpenWorkspace, onOpenTerminal, onDraftCwdChange, renderComposer }, ref) {
  useUiLanguage()
  const [task, setTask] = useState<CollaborationTask | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState(false)
  const menu = useRef<HTMLDivElement>(null)
  const composer = useRef<ComposerHandle>(null)
  const feed = useRef<HTMLDivElement>(null)
  const follow = useRef(true)
  const selected = useRef(selectedId); selected.current = selectedId
  const pendingMember = useRef<{ group: string; engine: CollaborationEngine; requestId: string } | null>(null)
  const group = task?.kind === 'group'
  const active = task?.status === 'running' || task?.status === 'waiting'
  const workflowLabels = { discussion: '正在同时讨论…', planning: '正在整理方案与分工…', developing: '正在按分工开发…', summarizing: '正在核实并汇总…' }
  const canChangeMembers = group && task.status === 'idle' && !busy
  const members = Object.entries(task?.members ?? {})
  function name(id: string) { return task?.members[id]?.name || task?.departedMembers?.[id]?.name || ui(roles[id] ?? '群成员') }
  function handleError(cause: unknown) {
    if (cause instanceof RemoteApiError && cause.status === 401) onUnauthorized()
    return cause instanceof Error ? cause.message : ui('请求失败，请重试')
  }
  useImperativeHandle(ref, () => ({ appendText(text, focus = true) {
    composer.current?.appendDraft(text, focus)
  }, addDocument(document, focus = true) {
    return composer.current?.addDocumentAttachment(document, focus) ?? false
  } }), [])
  async function refreshSelected() {
    if (!selectedId) return
    const next = await devwispClient.collaborationTask(token, selectedId)
    if (selected.current === selectedId) setTask(next)
    await onRefresh()
  }

  useEffect(() => {
    let live = true
    void devwispClient.status(token).then((status) => { if (live && !selected.current) onDraftCwdChange(status.workspace ?? '') }).catch(() => {})
    return () => { live = false }
  }, [token, onDraftCwdChange])
  useEffect(() => {
    setTask(null); setError(''); setAdding(false); follow.current = true
  }, [token, selectedId])
  useEffect(() => {
    if (!visible || !selectedId) return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    const refresh = async () => {
      try {
        const next = await devwispClient.collaborationTask(token, selectedId, controller.signal)
        if (live) setTask(next)
      } catch (cause) { if (live) setError(handleError(cause)) }
      finally { if (live) timer = setTimeout(() => void refresh(), 2000) }
    }
    void refresh()
    return () => { live = false; clearTimeout(timer); controller.abort() }
    // Poll only the selected conversation and cancel reads on navigation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, selectedId, visible])
  useEffect(() => { if (follow.current && feed.current) feed.current.scrollTop = feed.current.scrollHeight }, [task?.events?.length])
  useEffect(() => {
    if (!adding) return
    const close = (event: PointerEvent) => { if (!menu.current?.contains(event.target as Node)) setAdding(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setAdding(false) }
    document.addEventListener('pointerdown', close); document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', escape) }
  }, [adding])

  async function apply(action: () => Promise<CollaborationTask>, id: string) {
    if (busy) return
    setBusy(true); setError('')
    try {
      const next = await action()
      if (selected.current === id) setTask(next)
      await onRefresh()
    } catch (cause) { if (selected.current === id) setError(handleError(cause)) }
    finally { setBusy(false) }
  }
  async function add(engine: CollaborationEngine) {
    if (!task || !canChangeMembers) return
    const id = task.id
    const previous = pendingMember.current
    const input = previous?.group === id && previous.engine === engine ? previous : { group: id, engine, requestId: collaborationId() }
    pendingMember.current = input
    setAdding(false)
    await apply(async () => {
      const next = await devwispClient.addCollaborationMember(token, id, { requestId: input.requestId, engine })
      pendingMember.current = null
      return next
    }, id)
  }
  async function openMember(member: CollaborationMember, id: string) {
    const threadId = task?.activeMemberThreads?.[id] || member.threadId
    const worker = Object.values(task?.workerMembers ?? {}).find((entry) => entry.threadId === threadId)
    if (!threadId || !task) return
    try {
      // Register newly created worker sessions in the shared sidebar state
      // before opening them, so their native composer stays group-managed.
      await onRefresh()
      onOpenChat({ id: threadId, engine: member.engine, cwd: worker?.cwd || task.cwd, name: name(id) })
    } catch (cause) { setError(handleError(cause)) }
  }
  return <section className="collaboration-main" hidden={!visible} aria-label={ui('协同会话')}>
    <header className="remote-chat__header collaboration-header">
      <div className="remote-chat__title-wrap">
        <button className="icon-button menu-button" type="button" aria-label={ui('打开对话列表')} onClick={onOpenSidebar}><Menu size={20} /></button>
        <div className="remote-chat__title"><div className="collaboration-title-line"><CollaborationModelIcons members={task?.members ?? {}} /><strong>{task?.title || ui('协同群聊')}</strong></div><span title={task?.cwd}>{task?.cwd}</span></div>
      </div>
      <div className="remote-chat__controls">
        {group && <div className="collaboration-add" ref={menu}><button className="icon-button" type="button" disabled={!canChangeMembers} aria-label={ui('添加群成员')} title={ui('添加群成员')} aria-expanded={adding} aria-haspopup="menu" onClick={() => setAdding(!adding)}><Plus size={18} /></button>{adding && <div className="collaboration-add-menu" role="menu">{engines.map((engine) => <button type="button" role="menuitem" key={engine.id} onClick={() => void add(engine.id)}><span className={`engine-brand-icon engine-brand-icon--${engine.id}`} aria-hidden="true" />{engine.name}</button>)}</div>}</div>}
        <button type="button" className="icon-button" aria-label={ui('新建协同群')} title={ui('新建协同群')} disabled={creating} onClick={onCreate}><UsersRound size={18} /></button>
        <button type="button" className="icon-button terminal-open-button" aria-label={ui('本机终端')} onClick={onOpenTerminal}><Terminal size={17} /></button>
        <button type="button" className="icon-button workspace-button" aria-label={ui('打开文件树')} onClick={onOpenWorkspace}><FolderOpen size={18} /></button>
      </div>
    </header>
    {!selectedId ? <div className="collaboration-empty"><UsersRound size={30} /><p>{ui('建个群，让助手一起聊。')}</p><button className="collaboration-primary" disabled={creating} onClick={onCreate}><Plus size={16} />{ui(creating ? '正在创建…' : '新建协同群')}</button></div> : !task ? <div className="collaboration-loading" role="status">{error || <LoaderCircle className="spin" size={22} />}</div> : <>
      {members.length > 0 && <div className="collaboration-group-members" aria-label={ui('群成员')}>{members.map(([id, member]) => <div className="collaboration-group-member" key={id}><button type="button" disabled={!member.threadId && !task.activeMemberThreads?.[id]} onClick={() => openMember(member, id)} title={ui('打开成员会话')}><span className={`engine-brand-icon engine-brand-icon--${member.engine}`} aria-hidden="true" />{name(id)}</button>{group && <button type="button" className="collaboration-member-remove" disabled={!canChangeMembers} aria-label={`${ui('移出群聊')} ${name(id)}`} onClick={() => void apply(() => devwispClient.removeCollaborationMember(token, task.id, id), task.id)}><X size={12} /></button>}</div>)}</div>}
      <div className="collaboration-feed" ref={feed} onScroll={() => { const element = feed.current; if (element) follow.current = element.scrollHeight - element.scrollTop - element.clientHeight < 90 }}>
        {!members.length && group ? <div className="collaboration-empty"><p>{ui('添加助手，开始群聊')}</p><div className="collaboration-quick-add">{engines.map((engine) => <button type="button" key={engine.id} disabled={!canChangeMembers} onClick={() => void add(engine.id)}><Plus size={14} /><span className={`engine-brand-icon engine-brand-icon--${engine.id}`} aria-hidden="true" />{engine.name}</button>)}</div><small>{ui('已有单聊可在左侧右键加入。')}</small></div> : !task.events?.length ? <div className="collaboration-empty"><p>{ui('发送消息，开始协同。')}</p></div> : null}
        {!group && <details className="collaboration-goal"><summary>{ui('任务目标')}</summary><p>{task.goal}</p></details>}
        {task.events?.map((entry) => <article key={entry.id} className={`collaboration-event collaboration-event--${entry.role === 'human' ? 'human' : entry.role === 'system' ? 'system' : 'assistant'}`}><div><strong>{entry.role === 'human' ? ui('你') : name(entry.role)}</strong><time dateTime={entry.createdAt}>{new Date(entry.createdAt).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}</time></div>{entry.images?.length ? <div className="chat-message__images">{entry.images.map((attachment, index) => <ImagePreviewButton key={`${attachment.name}-${index}`} src={`data:${attachment.mediaType};base64,${attachment.data}`} name={attachment.name} />)}</div> : null}{entry.documents?.map((document, index) => <div className="message-document" key={`${document.name}-${index}`}>{document.name}</div>)}<ClaudeReplyText text={group ? entry.text : entry.text.replace(/\n?(DEVWISP_HANDOFF|DEVWISP_REVIEW: (APPROVED|CHANGES_REQUESTED))\s*$/, '')} /></article>)}
        {active && <div className="collaboration-working" role="status"><LoaderCircle className="spin" size={16} />{task.status === 'waiting' ? ui('打开成员会话处理审批或问题') : task.workflowPhase ? `${ui(workflowLabels[task.workflowPhase])}${Object.keys(task.activeMemberThreads ?? {}).length ? ` · ${Object.keys(task.activeMemberThreads!).map(name).join(', ')}` : ''}` : `${name(task.phase)} · ${ui('正在回复…')}`}</div>}
      </div>
      {error && <div className="collaboration-error" role="alert">{error}</div>}
      {task.reason && <p className="collaboration-reason" role="status">{task.reason}</p>}
      {renderComposer(task, composer, refreshSelected)}
    </>}
  </section>
})
