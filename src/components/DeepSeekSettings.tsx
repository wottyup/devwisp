import { useEffect, useRef, useState, type FormEvent } from 'react'
import { CircleCheck, CircleX, LoaderCircle, PlugZap, Plus, RefreshCw, Save, Trash2 } from 'lucide-react'
import { devwispClient, type DeepSeekConfig, type DeepSeekProfile, type DeepSeekProfileInput, type NativeEngineConnectionResult, type NativeEngineProtocol } from '../lib/devwispClient'
import { ui, uiFormat, uiMessage, useUiLanguage } from '../lib/uiLanguage'
import { TranslationSelect } from './TranslationSelect'
import { HarnessSetupCard } from './HarnessSetupCard'

type Draft = Omit<DeepSeekProfileInput, 'profileId' | 'expectedRevision' | 'apply'>
function draftFor(profile?: DeepSeekProfile): Draft {
  return { name: profile?.name ?? '', model: profile?.model ?? 'deepseek-v4-flash', baseUrl: profile?.baseUrl ?? 'https://api.deepseek.com', protocol: profile?.protocol ?? 'anthropic', apiKey: '', clearKey: false }
}
export function DeepSeekSettings({ token, onDirtyChange }: { token: string; onDirtyChange: (dirty: boolean) => void }) {
  useUiLanguage()
  const [config, setConfig] = useState<DeepSeekConfig | null>(null)
  const [profileId, setProfileId] = useState('')
  const [draft, setDraft] = useState<Draft>(() => draftFor())
  const [busy, setBusy] = useState<string | null>(null)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const [result, setResult] = useState<NativeEngineConnectionResult | null>(null)
  const form = useRef<HTMLFormElement>(null)
  const mounted = useRef(false)
  const testController = useRef<AbortController | null>(null)
  const selected = config?.profiles.find((profile) => profile.id === profileId)
  const active = config?.profiles.find((profile) => profile.id === config.activeProfileId)
  const dirty = Boolean(config && JSON.stringify(draft) !== JSON.stringify(draftFor(selected)))
  const disabled = !config || Boolean(busy)
  function receive(value: DeepSeekConfig, preferred?: string) {
    const profile = value.profiles.find((p) => p.id === (preferred ?? value.activeProfileId)) ?? value.profiles[0]
    setConfig(value); setProfileId(profile?.id ?? ''); setDraft(draftFor(profile)); setResult(null)
  }
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    void devwispClient.deepseekProfiles(token, controller.signal).then((value) => {
      if (!controller.signal.aborted) receive(value)
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setMessage({ text: reason instanceof Error ? reason.message : '读取配置失败。', error: true })
    })
    return () => { mounted.current = false; controller.abort(); testController.current?.abort() }
  }, [token])
  useEffect(() => {
    onDirtyChange(dirty || busy === 'save' || busy === 'apply' || busy === 'delete')
    return () => onDirtyChange(false)
  }, [dirty, busy, onDirtyChange])
  function edit(change: Partial<Draft>) { setDraft((current) => ({ ...current, ...change })); setMessage(null); setResult(null) }
  function choose(id: string) {
    if (disabled || id === profileId) return
    if (dirty && !window.confirm(ui('当前修改还没保存，放弃修改并切换配置吗？'))) return
    setProfileId(id); setDraft(draftFor(config?.profiles.find((p) => p.id === id))); setMessage(null); setResult(null)
  }
  function input(): DeepSeekProfileInput { return { ...draft, profileId: profileId || undefined, expectedRevision: config!.profilesRevision } }
  async function perform(kind: string, action: () => Promise<void>) {
    setBusy(kind); setMessage(null); setResult(null)
    try { await action() }
    catch (reason) { if (mounted.current) setMessage({ text: reason instanceof Error ? reason.message : '操作失败，请重试。', error: true }) }
    finally { if (mounted.current) setBusy(null) }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (disabled) return
    const apply = ((event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null)?.value !== 'save'
    await perform(apply ? 'apply' : 'save', async () => {
      const value = await devwispClient.saveDeepSeekProfile(token, { ...input(), apply })
      if (!mounted.current) return
      receive(value.config, value.profileId)
      setMessage({ text: apply ? '已保存，下一次发送时生效。' : '配置已保存，可随时选择并应用。', error: false })
      if (value.applied) window.dispatchEvent(new CustomEvent('devwisp:deepseek-config-changed', { detail: { model: value.config.model } }))
    })
  }
  function refresh() {
    if (dirty && !window.confirm(ui('重新读取会丢弃当前未保存的修改，继续吗？'))) return
    void perform('refresh', async () => {
      const value = await devwispClient.deepseekProfiles(token)
      if (mounted.current) receive(value, profileId)
    })
  }
  function remove() {
    if (disabled || !selected || !config) return
    if (!window.confirm(uiFormat('删除配置“{0}”{1}？', selected.name, dirty ? ui('并放弃未保存的修改') : ''))) return
    void perform('delete', async () => {
      const value = await devwispClient.deleteDeepSeekProfile(token, profileId, config.profilesRevision)
      if (mounted.current) { receive(value); setMessage({ text: '配置已删除。', error: false }) }
    })
  }
  function test() {
    if (disabled || !form.current?.reportValidity()) return
    const controller = new AbortController()
    testController.current = controller
    void perform('test', async () => {
      try {
        const value = await devwispClient.testDeepSeekProfile(token, input(), controller.signal)
        if (!controller.signal.aborted && mounted.current) setResult(value)
      } finally { if (testController.current === controller) testController.current = null }
    })
  }
  return <section className="theme-settings-card theme-settings-card--page native-config" aria-label="DeepSeek Harness">
    <div className="theme-settings-card__heading"><div>
      <h2><span className="engine-brand-icon engine-brand-icon--deepseek-harness" aria-hidden="true" />DeepSeek Harness</h2>
      <p>{uiFormat('保存多套接口配置，按需要切换本机 {0} 使用的配置。', 'DeepSeek Harness')}</p>
    </div></div>
    <div className="native-config__body">
      <HarnessSetupCard token={token} />
      <div className="native-config__profiles">
        <TranslationSelect label={ui('已保存配置')} value={profileId || '__new'} disabled={disabled} onChange={(value) => choose(value === '__new' ? '' : value)} options={[
          ...(config?.profiles ?? []).map((p) => ({ value: p.id, label: `${p.name}${p.id === config?.activeProfileId ? ui(' · 当前使用') : ''}` })),
          ...(!profileId ? [{ value: '__new', label: ui('新增配置') }] : []),
        ]} />
        <div className="native-config__profile-actions">
          <button className="theme-notification-toggle" type="button" disabled={disabled || !profileId} onClick={() => choose('')}><Plus size={16} />{ui('新增')}</button>
          <button className="theme-notification-toggle" type="button" disabled={disabled || !selected || profileId === config?.activeProfileId} onClick={remove} title={profileId === config?.activeProfileId ? ui('先应用另一套配置，再删除当前配置') : ui('删除此配置')}><Trash2 size={15} />{ui('删除')}</button>
        </div>
      </div>
      <p className="native-config__current" aria-hidden={!config}>{ui('当前使用：')}<strong>{active?.name}{config?.activeProfileModified ? ui('（有未应用的修改）') : ''}</strong><span>{ui('选择配置用于编辑，点击“保存并应用”才会切换。')}</span></p>
      <form ref={form} className="native-config__form" onSubmit={(event) => { void save(event) }}>
        <label className="native-config__field"><span>{ui('配置名称')}</span><input value={draft.name} onChange={(e) => edit({ name: e.target.value })} placeholder={ui('例如：日常使用、备用接口')} maxLength={80} required disabled={disabled} /></label>
        <section className="native-config__group" aria-label={ui('接口配置')}><h3>{ui('接口配置')}</h3>
          <div className="native-config__field"><TranslationSelect label={ui('接口协议')} value={draft.protocol} disabled={disabled} onChange={(value) => edit({ protocol: value as NativeEngineProtocol })} options={[
            { value: 'responses', label: 'OpenAI Responses' }, { value: 'anthropic', label: 'Anthropic Messages' }, { value: 'chat', label: 'OpenAI Chat Completions' },
          ]} /></div>
          <label className="native-config__field"><span>{ui('API 地址')}</span><input type="url" value={draft.baseUrl} onChange={(e) => edit({ baseUrl: e.target.value })} maxLength={2048} required disabled={disabled} autoCapitalize="none" autoCorrect="off" spellCheck={false} /><small>{ui('填写 API 根地址（可带 /v1），不要包含 /responses、/messages 或 /chat/completions。')}</small></label>
          <label className="native-config__field"><span>{ui('API 密钥')}{selected?.hasKey ? <em>{ui('已保存 · 留空保留')}</em> : null}</span><input type="password" autoComplete="new-password" value={draft.apiKey} onChange={(e) => edit({ apiKey: e.target.value })} placeholder={selected?.hasKey ? ui('留空保留此配置的密钥') : ui('输入 API 密钥')} maxLength={8192} disabled={disabled || draft.clearKey} /></label>
          <label className="native-config__hint deepseek-key-clear"><input type="checkbox" checked={draft.clearKey} onChange={(e) => edit({ clearKey: e.target.checked, apiKey: '' })} disabled={disabled || !selected?.hasKey} />{ui('移除已保存的密钥')}</label>
        </section>
        <section className="native-config__group" aria-label={ui('模型配置')}><h3>{ui('模型配置')}</h3>
          <label className="native-config__field"><span>{ui('默认模型')}</span><input value={draft.model} onChange={(e) => edit({ model: e.target.value })} maxLength={200} required disabled={disabled} autoCapitalize="none" autoCorrect="off" spellCheck={false} /></label>
        </section>
        <div className="native-config__actions">
          <button className="theme-notification-toggle" type="submit" value="apply" disabled={disabled}><Save size={15} />{busy === 'apply' ? ui('保存中…') : ui('保存并应用')}</button>
          <button className="theme-notification-toggle" type="submit" value="save" disabled={disabled}><Save size={15} />{busy === 'save' ? ui('保存中…') : ui('保存配置')}</button>
          <button className="theme-notification-toggle" type="button" disabled={disabled} onClick={test}>{busy === 'test' ? <LoaderCircle className="spin" size={15} /> : <PlugZap size={15} />}{busy === 'test' ? ui('正在测试…') : ui('测试连通性')}</button>
          <button className="theme-notification-toggle" type="button" disabled={Boolean(busy)} onClick={refresh}><RefreshCw size={15} />{ui('重新读取')}</button>
        </div>
      </form>
      {!config && !message ? <p className="native-config__message" role="status">{ui('正在读取配置…')}</p> : null}
      {config ? <p className="native-config__hint">{config.available ? ui('Harness 已安装') : uiMessage(config.error ?? '')}</p> : null}
      {message ? <p className={`native-config__message${message.error ? ' native-config__message--error' : ''}`} role={message.error ? 'alert' : 'status'}>{uiMessage(message.text)}</p> : null}
      {result ? <div className="native-config__test-results" role="status" aria-live="polite">
        <strong>{result.ok ? ui('连通性测试通过') : ui('连通性测试未全部通过')}</strong>
        {result.results.map((entry) => <div className={`native-config__test-row${entry.ok ? '' : ' native-config__message--error'}`} key={entry.model}>
          {entry.ok ? <CircleCheck size={17} /> : <CircleX size={17} />}<div><strong>{entry.model}</strong><p>{uiMessage(entry.message)}</p></div><small>{(entry.latencyMs / 1000).toFixed(1)} {ui('秒')}</small>
        </div>)}
      </div> : null}
      <div className="native-config__footnote"><p className="native-config__hint">{ui('测试使用当前表单发起简短请求，可能产生少量 API 费用；不修改正在使用的配置。')}</p><p className="native-config__hint">{ui('第三方接口使用此处保存的密钥，也可使用后台 DEEPSEEK_API_KEY 环境变量；留空保留已保存密钥。')}</p></div>
    </div>
  </section>
}
