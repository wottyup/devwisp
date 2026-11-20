import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { devwispClient, RemoteApiError, type CodexModel, type CodexQueuedMessage, type CollaborationEngine, type CollaborationEngineSettings, type CollaborationTask } from '../lib/devwispClient'
import { collaborationId } from '../lib/collaborationId'
import { queuedRequestText } from '../lib/threadMessageMerge'
import { ui, uiMessage } from '../lib/uiLanguage'
import type { ComposerHandle, ComposerSendInput, MessageComposerProps } from './DevWispPage'

type Settings = Partial<Record<CollaborationEngine, CollaborationEngineSettings>>
interface Props {
  task: CollaborationTask; token: string; visible: boolean
  composerRef: RefObject<ComposerHandle | null>; base: MessageComposerProps
  catalogs: Record<CollaborationEngine, CodexModel[]>
  permissions: Pick<MessageComposerProps, 'authorizationOptions'>['authorizationOptions']
  onRefresh: () => Promise<void>; onCreate: () => void; onUnauthorized: () => void
  render: (props: Partial<MessageComposerProps>, ref: RefObject<ComposerHandle | null>) => ReactNode
}
const labels = { codex: 'Codex', 'claude-code': 'Claude', 'deepseek-harness': 'DeepSeek' }

export function CollaborationComposer({ task, token, visible, composerRef, base, catalogs, permissions, onRefresh, onCreate, onUnauthorized, render }: Props) {
  const queueId = `collaboration:${task.id}`
  const storageKey = `devwisp.group-composer-settings:${task.id}`
  const engines = [...new Set(Object.values(task.members).map((member) => member.engine))]
  const [selectedEngine, setSelectedEngine] = useState<CollaborationEngine>(engines[0] ?? 'codex')
  const engine = engines.includes(selectedEngine) ? selectedEngine : engines[0] ?? 'codex'
  const [settings, setSettings] = useState<Settings>(() => {
    try { return JSON.parse(localStorage.getItem(storageKey) ?? '{}') ?? {} } catch { return {} }
  })
  const [temporary, setTemporary] = useState(false)
  const [temporaryModels, setTemporaryModels] = useState<Settings>({})
  const [queue, setQueue] = useState<CodexQueuedMessage[]>([])
  const [error, setError] = useState('')
  const [controlling, setControlling] = useState(false)
  const [editing, setEditing] = useState(false)
  const owner = useRef(collaborationId())
  const pending = useRef<{ fingerprint: string; payload: CodexQueuedMessage } | null>(null)
  const edits = useRef(new Set<string>())
  const queueRevision = useRef(0)
  const latest = useRef({ onRefresh, onUnauthorized }); latest.current = { onRefresh, onUnauthorized }
  const active = task.status === 'running' || task.status === 'waiting'
  const modelSettings = Object.fromEntries(engines.map((id) => [id, {
    ...(id === 'codex' ? { approvalPolicy: 'on-request' } : id === 'claude-code' ? { claudePermissionMode: 'default' } : { deepseekPermissionMode: 'workspace-write' }),
    ...settings[id], ...temporaryModels[id],
  }])) as Settings
  const chosen = modelSettings[engine] ?? {}
  const models = catalogs[engine]
  const defaultModel = models.find((item) => item.isDefault) ?? models[0]
  const model = chosen.model ?? (engine === 'claude-code' ? 'default' : defaultModel?.model || defaultModel?.id || '')
  const effort = chosen.effort ?? 'default'
  const currentModel = models.find((item) => (item.model || item.id) === model)
  const reasoningByModel = useMemo(() => Object.fromEntries(models.map((item) => [item.model || item.id || '', {
    options: (item.supportedReasoningEfforts ?? []).map((option) => ({ value: option.reasoningEffort, label: option.reasoningEffort })),
    defaultEffort: item.defaultReasoningEffort,
  }])), [models])
  function update(changes: CollaborationEngineSettings, modelOnly = false) {
    if (temporary && modelOnly) { setTemporaryModels((current) => ({ ...current, [engine]: { ...current[engine], ...changes } })); return }
    setSettings((current) => {
      const next = { ...current, [engine]: { ...current[engine], ...changes } }
      try { localStorage.setItem(storageKey, JSON.stringify(next)) } catch { /* Keep the working session usable. */ }
      return next
    })
  }
  function report(cause: unknown) {
    if (cause instanceof RemoteApiError && cause.status === 401) latest.current.onUnauthorized()
    setError(cause instanceof Error ? cause.message : ui('请求失败，请重试'))
    return false
  }
  async function refreshQueue() {
    const revision = ++queueRevision.current
    const result = await devwispClient.queuedMessages(token, queueId)
    if (revision === queueRevision.current) setQueue(result.queue)
  }
  useEffect(() => {
    if (!visible || task.kind !== 'group') return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { await refreshQueue() } catch (cause) { if (live) report(cause) }
      if (live) timer = setTimeout(() => void poll(), 2000)
    }
    void poll()
    return () => { live = false; clearTimeout(timer); ++queueRevision.current }
    // Each group owns its queue; changing native chat never selects this queue.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, queueId, visible, task.kind])
  useEffect(() => {
    const timer = setInterval(() => {
      for (const id of edits.current) void devwispClient.renewQueuedEdit(token, queueId, id, owner.current).catch(report)
    }, 10000)
    const editing = edits.current, ownerId = owner.current
    return () => {
      clearInterval(timer)
      for (const id of editing) void devwispClient.queueAction(token, queueId, 'edit/end', id, ownerId).catch(() => {})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, queueId])
  async function mutate(action: () => Promise<{ queue: CodexQueuedMessage[]; sent?: boolean }>, requireSent = false) {
    ++queueRevision.current
    try {
      const result = await action()
      ++queueRevision.current; setQueue(result.queue); setError('')
      await latest.current.onRefresh().catch(report)
      return requireSent ? Boolean(result.sent) : true
    } catch (cause) { return report(cause) }
  }
  function payloadFor(input: ComposerSendInput, previous?: CodexQueuedMessage): CodexQueuedMessage {
    const { prompt, sendDirectly: _direct, continuePaused: _continue, ...values } = input
    const text = prompt.trim()
    const snapshot = previous?.collaborationSettings ?? modelSettings
    const normalizedSettings = Object.fromEntries(Object.entries(snapshot).map(([id, value]) => [id, {
      ...value, model: !value.model || value.model === 'default' ? undefined : value.model,
      effort: !value.effort || value.effort === 'default' ? undefined : value.effort,
    }]))
    normalizedSettings[engine] = { ...normalizedSettings[engine],
      model: (engine === 'claude-code' ? input.claudeModel : input.model) || undefined,
      effort: (engine === 'claude-code' ? input.claudeEffort : input.effort) || undefined,
    }
    if (normalizedSettings[engine].model === 'default') normalizedSettings[engine].model = undefined
    if (normalizedSettings[engine].effort === 'default') normalizedSettings[engine].effort = undefined
    return { ...previous, originalText: undefined, translation: undefined, translationStatus: undefined, translationError: undefined,
      ...values, id: previous?.id ?? collaborationId(), createdAt: previous?.createdAt ?? new Date().toISOString(),
      text, requestText: queuedRequestText(text, input.documents) || ui('请查看附件。'), collaborationSettings: normalizedSettings }
  }
  async function send(input: ComposerSendInput) {
    if (task.kind !== 'group') {
      try { await devwispClient.noteCollaboration(token, task.id, input.prompt); await onRefresh(); return true } catch (cause) { return report(cause) }
    }
    const payload = payloadFor(input)
    const { id: _id, createdAt: _at, ...content } = payload
    const fingerprint = JSON.stringify(content)
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, payload }
    const request = pending.current.payload
    let accepted = false
    if (input.translationStatus === 'pending') accepted = await mutate(() => devwispClient.enqueueTranslatingMessage(token, queueId, request))
    else if (input.scheduledAt) accepted = await mutate(() => devwispClient.scheduleMessage(token, queueId, request))
    else {
      accepted = await mutate(async () => {
        const result = await devwispClient.submitMessage(token, queueId, request)
        if (!result.sent && input.sendDirectly) return devwispClient.queueAction(token, queueId, 'send-now', request.id)
        return result
      })
    }
    if (accepted) { pending.current = null; setTemporaryModels({}) }
    return accepted
  }
  async function control(action: 'pause' | 'resume') {
    if (controlling) return
    setControlling(true)
    try { await devwispClient.controlCollaboration(token, task.id, action); setError(''); await onRefresh(); await refreshQueue() }
    catch (cause) { report(cause) } finally { setControlling(false) }
  }
  const paused = task.status === 'paused' || task.status === 'failed'
  return <div className="collaboration-composer">
    {engines.length > 1 && <div className="collaboration-composer-engines" role="group" aria-label={ui('成员模型设置')}>{engines.map((id) => <button key={id} type="button" disabled={editing} aria-pressed={engine === id} onClick={() => setSelectedEngine(id)}><span className={`engine-brand-icon engine-brand-icon--${id}`} aria-hidden="true" />{labels[id]}</button>)}</div>}
    {error && <div className="collaboration-error" role="alert">{uiMessage(error)}</div>}
    {render({
      mentionSuggestions: task.kind === 'group' ? [...Object.values(task.members).map((member) => member.name!).filter(Boolean), ui('所有人')] : undefined,
      threadId: queueId, draftScope: `thread:${queueId}`, onDraftChange: () => {},
      connection: task.status === 'completed' || !engines.length ? 'disconnected' : 'connected', managedTask: false,
      isRunning: active, isReplying: active, canInterrupt: true, isInterrupting: controlling, writerConflict: false,
      fileChangeSummary: null, fileChangeSummaryKey: null, reserveFileChangeSummarySpace: false, stableQueueHeight: false,
      composerEngine: engine, showCodexPreferences: engine !== 'claude-code',
      modelSupportsImages: task.kind === 'group' && (!currentModel?.inputModalities || currentModel.inputModalities.includes('image')),
      modelOptions: models.flatMap((item) => item.model || item.id ? [{ value: item.model || item.id!, label: item.displayName || item.model || item.id! }] : []),
      selectedModel: model, selectedEffort: effort,
      reasoningOptions: reasoningByModel[model]?.options ?? [], reasoningOptionsByModel: reasoningByModel,
      selectedApprovalPolicy: chosen.approvalPolicy ?? 'on-request', selectedClaudePermissionMode: chosen.claudePermissionMode ?? 'default',
      selectedDeepseekPermissionMode: chosen.deepseekPermissionMode ?? 'workspace-write', selectedDeepseekAgentPreset: chosen.deepseekAgentPreset ?? 'standard',
      deepseekAgentPresetLocked: Object.values(task.members).some((member) => member.engine === 'deepseek-harness' && Boolean(member.threadId)),
      deepseekAgentPresetsAvailable: true, authorizationOptions: engine === 'deepseek-harness' ? permissions : base.authorizationOptions,
      onModelChange: (value) => update({ model: value }, true), onEffortChange: (value) => update({ effort: value }, true),
      onApprovalPolicyChange: (value) => update({ approvalPolicy: value }), onClaudePermissionModeChange: (value) => update({ claudePermissionMode: value }),
      onDeepseekPermissionModeChange: (value) => update({ deepseekPermissionMode: value }), onDeepseekAgentPresetChange: (value) => update({ deepseekAgentPreset: value }),
      temporaryPreferences: temporary, onTemporaryPreferencesChange: (enabled) => { setTemporary(enabled); setTemporaryModels({}) },
      approvalPolicyUpdating: false, onSend: send, queuedMessages: queue.map((item) => {
        const preferences = item.collaborationSettings?.[engine]
        return { ...item, ...(engine === 'claude-code'
          ? { claudeModel: preferences?.model, claudeEffort: preferences?.effort }
          : { model: preferences?.model, effort: preferences?.effort }) }
      }),
      onSendQueuedNow: (id, mode) => mutate(() => mode === 'start' ? devwispClient.startQueuedIfIdle(token, queueId, id) : devwispClient.queueAction(token, queueId, 'send-now', id), true),
      onRetryQueuedTranslation: (id) => mutate(() => devwispClient.retryQueuedTranslation(token, queueId, id)),
      onBeginQueuedEdit: async (id) => { const ok = await mutate(() => devwispClient.queueAction(token, queueId, 'edit/begin', id, owner.current)); if (ok) { edits.current.add(id); setEditing(true) } return ok },
      onEndQueuedEdit: async (id) => { const ok = await mutate(() => devwispClient.queueAction(token, queueId, 'edit/end', id, owner.current)); if (ok) { edits.current.delete(id); setEditing(false) } return ok },
      onUpdateQueuedMessage: (id, input) => {
        const previous = queue.find((item) => item.id === id)
        return previous ? mutate(() => devwispClient.updateQueuedMessage(token, queueId, payloadFor(input, previous))) : Promise.resolve(false)
      },
      onRemoveQueued: async (id) => { const ok = await mutate(() => devwispClient.queueAction(token, queueId, 'remove', id, owner.current)); if (ok) { edits.current.delete(id); setEditing(edits.current.size > 0) } return ok },
      onMoveQueued: (id, adjacent, direction) => mutate(() => devwispClient.moveQueuedMessage(token, queueId, id, adjacent, direction)),
      onInterrupt: () => void control('pause'), onContinue: () => void control('resume'), canContinue: paused && !controlling, continuationPending: paused,
      onRefresh: () => { void onRefresh(); void refreshQueue().catch(report) }, onCreateThread: onCreate, createThreadDisabled: false, createThreadTitle: ui('新建协同群'),
    }, composerRef)}
  </div>
}
