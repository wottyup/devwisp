import { useEffect, useState, type FormEvent } from 'react'
import { ChevronDown, LoaderCircle, Plus, Save, Trash2 } from 'lucide-react'
import { ui, uiFormat, uiMessage, useUiLanguage } from '../lib/uiLanguage'
import { TranslationSelect } from './TranslationSelect'
import {
  devwispClient,
  type SshMcpProfile,
  type SshMcpSettings,
} from '../lib/devwispClient'

interface ProfileDraft {
  id: number
  value: SshMcpProfile
  persisted: boolean
  collapsed: boolean
}

let nextDraftId = 1

const permissionPresets = {
  viewer: { group: 'dev', role: 'viewer', readOnly: true },
  operator: { group: 'dev', role: 'operator', readOnly: false },
  admin: { group: 'dev', role: 'admin', readOnly: false },
} satisfies Record<string, Pick<SshMcpProfile, 'group' | 'role' | 'readOnly'>>

function permissionLevel(profile: SshMcpProfile): keyof typeof permissionPresets | 'custom' {
  if (profile.readOnly) return 'viewer'
  if ((profile.group === 'dev' || profile.group === 'staging') && (profile.role === 'operator' || profile.role === 'admin')) return profile.role
  return 'custom'
}

function permissionLabel(profile: SshMcpProfile): string {
  const level = permissionLevel(profile)
  return ui(level === 'viewer' ? '只读' : level === 'operator' ? '运维' : level === 'admin' ? '管理员' : '保留现有权限')
}

function permissionDescription(profile: SshMcpProfile): string {
  const level = permissionLevel(profile)
  if (level === 'viewer') return ui('查看文件、日志和状态；不允许修改。')
  if (level === 'operator') return ui('允许修改文件、删除和重启等运维操作；不允许 sudo 提权。')
  if (level === 'admin') return ui('允许修改和提权操作；实际权限由 SSH 登录账号决定。')
  return uiFormat('保留现有设置：分组 {0}，角色 {1}。选择其他级别会应用对应权限。', profile.group, profile.role)
}

function approvalDescription(policy: SshMcpProfile['approvalPolicy']): string {
  if (policy === 'auto') return ui('在所选操作权限内自动批准，不再逐次询问。')
  if (policy === 'ask-all') return ui('每次操作都在 Codex 对话中请求批准，包括只读操作。')
  if (policy === 'deny') return ui('拒绝被判定为高风险的操作；其余操作按所选权限执行。')
  return ui('仅对被判定为高风险的操作询问，例如递归删除目录或提权。')
}

function newProfile(name: string): SshMcpProfile {
  return {
    name, host: '', port: 22, user: '', auth: 'password', group: 'dev', role: 'admin',
    readOnly: false, approvalPolicy: 'auto',
  }
}

function draftsFor(profiles: SshMcpProfile[]): ProfileDraft[] {
  return profiles.map((value) => ({ id: nextDraftId++, value: { ...value }, persisted: true, collapsed: true }))
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : '读取 SSH MCP 配置失败。'
}

export function SshMcpSettingsCard({ token }: { token: string }) {
  useUiLanguage()
  const [settings, setSettings] = useState<SshMcpSettings | null>(null)
  const [profiles, setProfiles] = useState<ProfileDraft[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [removingId, setRemovingId] = useState<number | null>(null)
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null)
  const disabled = loading || saving || removingId !== null
  const saveDisabled = disabled || settings === null

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setMessage(null)
    void devwispClient.sshMcpSettings(token).then((result) => {
      if (cancelled) return
      setSettings(result)
      setProfiles(draftsFor(result.profiles))
    }).catch((reason: unknown) => {
      if (!cancelled) setMessage({ text: errorMessage(reason), error: true })
    }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [token])

  function update(id: number, changes: Partial<SshMcpProfile>) {
    setProfiles((current) => current.map((draft) => draft.id === id
      ? { ...draft, value: { ...draft.value, ...changes } }
      : draft))
    setMessage(null)
  }

  function addProfile() {
    const id = nextDraftId++
    setProfiles((current) => {
      const names = new Set(current.map(({ value }) => value.name.toLowerCase()))
      let name = `ssh-server-${id}`
      let suffix = 1
      while (names.has(name.toLowerCase())) name = `ssh-server-${id}-${suffix++}`
      return [...current, { id, value: newProfile(name), persisted: false, collapsed: false }]
    })
    setMessage(null)
  }

  function toggleProfile(id: number) {
    setProfiles((current) => current.map((draft) => draft.id === id && draft.persisted
      ? { ...draft, collapsed: !draft.collapsed }
      : draft))
  }

  async function removeProfile(id: number) {
    if (disabled) return
    const draft = profiles.find((profile) => profile.id === id)
    if (!draft) return
    setMessage(null)
    if (!draft.persisted) {
      setProfiles((current) => current.filter((profile) => profile.id !== id))
      return
    }
    setRemovingId(id)
    try {
      const result = await devwispClient.removeSshMcpProfile(token, draft.value.name)
      setSettings(result)
      setProfiles((current) => current.filter((profile) => profile.id !== id))
      setMessage({ text: 'SSH 服务器已移除。', error: false })
    } catch (reason) {
      setMessage({ text: errorMessage(reason), error: true })
    } finally {
      setRemovingId(null)
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (disabled || !event.currentTarget.reportValidity()) return
    setSaving(true)
    setMessage(null)
    try {
      const result = await devwispClient.saveSshMcpSettings(token, profiles.map(({ value }) => {
        const profile = { ...value }
        delete profile.hasPassword
        return {
          ...profile,
          displayName: profile.displayName?.trim() || undefined,
          keyRef: profile.auth === 'key' ? profile.keyRef?.trim() || undefined : undefined,
          trustedHostKey: profile.trustedHostKey?.trim() || undefined,
          workdir: profile.workdir?.trim() || undefined,
        }
      }))
      setSettings(result)
      setProfiles(draftsFor(result.profiles))
      setMessage({ text: result.profiles.length ? 'SSH MCP 配置已保存；新建或重新打开的 Codex 对话会使用这些服务器。' : 'SSH MCP 配置已移除。', error: false })
    } catch (reason) {
      setMessage({ text: errorMessage(reason), error: true })
    } finally {
      setSaving(false)
    }
  }

  return <section className="theme-settings-card theme-settings-card--page native-config ssh-mcp-card" aria-label={ui('SSH MCP 服务器')}>
    <div className="theme-settings-card__heading">
      <div>
        <h2><span className="ssh-mcp-shield" aria-hidden="true">SSH</span>{ui('SSH MCP 服务器')}</h2>
        <p>{ui('添加 SSH 服务器，并分别设置 AI 的操作权限和审批方式。')}</p>
      </div>
      <span className="theme-settings-card__badge">{ui('仅 Codex')}</span>
    </div>
    <div className="native-config__body">
      {loading ? <p className="native-config__message" role="status">{ui('正在读取 SSH MCP 配置…')}</p> : null}
      {!loading && settings && !settings.available ? <p className="native-config__message native-config__message--error" role="status">
        {ui('SSH MCP 运行依赖不可用。请确认 Dev Wisp 工作区已安装 pnpm 依赖。')}
      </p> : null}
      {profiles.length === 0 && !loading ? <p className="native-config__message">{ui('尚未配置 SSH 服务器。')}</p> : null}
      <form className="native-config__form" onSubmit={(event) => { void save(event) }}>
        {profiles.map(({ id, value, persisted, collapsed }, index) => {
          const fieldsId = `ssh-mcp-profile-fields-${id}`
          const title = value.displayName?.trim() || value.name || `${ui('服务器')} ${index + 1}`
          const level = permissionLevel(value)
          const summary = [value.host ? `${value.host}:${value.port}` : '', value.user, permissionLabel(value)].filter(Boolean).join(' · ')
          return <section className="native-config__group ssh-mcp-profile" key={id} aria-label={ui('SSH MCP 服务器配置')}>
          <div className="ssh-mcp-profile__heading">
            <div className="ssh-mcp-profile__heading-info">
              <h3>{persisted ? <button className="ssh-mcp-profile__toggle" type="button" aria-expanded={!collapsed} aria-controls={fieldsId} aria-label={collapsed ? ui('展开服务器配置') : ui('折叠服务器配置')} disabled={disabled} onClick={(event) => {
                if (!collapsed) {
                  const invalidField = event.currentTarget.closest('.ssh-mcp-profile')?.querySelector<HTMLInputElement>('input:invalid')
                  if (invalidField) {
                    invalidField.reportValidity()
                    return
                  }
                }
                toggleProfile(id)
              }}><ChevronDown size={16} aria-hidden="true" />{title}</button> : title}</h3>
              {collapsed && summary ? <small className="ssh-mcp-profile__summary">{summary}</small> : null}
            </div>
            <button className="theme-notification-toggle ssh-mcp-profile__remove" type="button" disabled={disabled} onClick={() => { void removeProfile(id) }}>
              {removingId === id ? <LoaderCircle size={15} className="ssh-mcp-spin" /> : <Trash2 size={15} />}{removingId === id ? ui('移除中…') : ui('移除')}
            </button>
          </div>
          <fieldset id={fieldsId} className="ssh-mcp-profile__fields" disabled={disabled || collapsed} hidden={collapsed}>
          <div className="native-config__columns">
            <label className="native-config__field ssh-mcp-profile__name"><span>{ui('服务器名称')}</span><input value={value.displayName ?? ''} onChange={(event) => update(id, { displayName: event.target.value })} placeholder={value.name} maxLength={80} autoComplete="off" disabled={disabled} /><small>{ui('支持中文和空格；留空使用默认名称。')}</small></label>
            <label className="native-config__field"><span>{ui('IP 地址 / 主机名')}</span><input value={value.host} onChange={(event) => update(id, { host: event.target.value })} placeholder="192.168.1.10" maxLength={255} autoCapitalize="none" autoCorrect="off" spellCheck={false} required disabled={disabled} /></label>
            <label className="native-config__field"><span>{ui('端口')}</span><input type="number" min={1} max={65535} value={value.port} onChange={(event) => update(id, { port: Number(event.target.value) })} required disabled={disabled} /></label>
            <label className="native-config__field"><span>{ui('用户名')}</span><input value={value.user} onChange={(event) => update(id, { user: event.target.value })} maxLength={128} autoCapitalize="none" autoCorrect="off" spellCheck={false} required disabled={disabled} /></label>
            <label className="native-config__field"><span>{ui('密码')}</span><input type="password" autoComplete="new-password" maxLength={4096} value={value.password ?? ''} onChange={(event) => update(id, { password: event.target.value, ...(event.target.value ? { auth: 'password' } : {}) })} placeholder={value.hasPassword ? ui('已保存，留空则保持不变') : ui('输入 SSH 登录密码')} required={value.auth === 'password' && !value.hasPassword} disabled={disabled} /><small>{value.hasPassword ? ui('密码已保存；留空会继续使用。') : ui('密码保存在 Dev Wisp 主机，不会在设置中显示。')}</small></label>
            <div className="native-config__field">
              <TranslationSelect label={ui('操作权限')} value={level} options={[
                ...(level === 'custom' ? [{ value: 'custom', label: ui('保留现有权限'), disabled: true }] : []),
                { value: 'viewer', label: ui('只读') },
                { value: 'operator', label: ui('运维') },
                { value: 'admin', label: ui('管理员') },
              ]} disabled={disabled || collapsed} onChange={(next) => {
                if (next === 'viewer' || next === 'operator' || next === 'admin') update(id, permissionPresets[next])
              }} />
              <small>{permissionDescription(value)}</small>
            </div>
            <div className="native-config__field">
              <TranslationSelect label={ui('审批方式')} value={value.approvalPolicy} options={[
                { value: 'auto', label: ui('自动批准') },
                { value: 'ask-all', label: ui('每次操作都询问') },
                { value: 'ask-destructive', label: ui('仅高风险操作询问') },
                { value: 'deny', label: ui('拒绝高风险操作') },
              ]} disabled={disabled || collapsed} onChange={(next) => {
                if (next === 'auto' || next === 'ask-all' || next === 'ask-destructive' || next === 'deny') update(id, { approvalPolicy: next })
              }} />
              <small>{approvalDescription(value.approvalPolicy)}</small>
            </div>
          </div>
          </fieldset>
        </section>
        })}
        <div className="native-config__actions ssh-mcp-actions">
          <button className="theme-notification-toggle" type="button" disabled={disabled} onClick={addProfile}><Plus size={16} />{ui('添加服务器')}</button>
          <button className={`theme-notification-toggle${saveDisabled ? '' : ' theme-notification-toggle--enabled'}`} type="submit" disabled={saveDisabled}>
            {saving ? <LoaderCircle size={15} className="ssh-mcp-spin" /> : <Save size={15} />}{saving ? ui('保存中…') : ui('保存配置')}
          </button>
        </div>
        {message ? <p className={message.error ? 'native-config__message native-config__message--error' : 'native-config__message'} role={message.error ? 'alert' : 'status'}>{uiMessage(message.text)}</p> : null}
      </form>
      <p className="native-config__hint">{ui('配置保存在运行 Dev Wisp 的电脑上。保存后，新建或重新打开的 Codex 对话使用最新设置；正在运行的对话不受影响。')}</p>
      <p className="native-config__hint">{ui('Claude Code 暂不支持此功能，因为它的命令审批还不能转发到网页。')}</p>
    </div>
  </section>
}
