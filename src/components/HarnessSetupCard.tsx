import { useEffect, useState } from 'react'
import { devwispClient, type HarnessSetupStatus } from '../lib/devwispClient'

export function HarnessSetupCard({ token }: { token: string }) {
  const [status, setStatus] = useState<HarnessSetupStatus | null>(null)
  const [path, setPath] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let disposed = false
    let timer: number | undefined
    const refresh = async () => {
      try {
        const value = await devwispClient.deepseekRuntime(token)
        if (disposed) return
        setStatus(value)
        if (value.busy) timer = window.setTimeout(() => void refresh(), 1500)
      } catch (reason) {
        if (!disposed) setError(reason instanceof Error ? reason.message : '无法读取运行组件状态')
      }
    }
    void refresh()
    return () => { disposed = true; window.clearTimeout(timer) }
  }, [token, pending])
  const busy = pending || status?.busy === true
  async function run(operation: 'install' | 'check' | 'save') {
    if (busy) return
    setPending(true); setError('')
    try {
      const value = operation === 'save'
        ? await devwispClient.saveDeepseekRuntime(token, path)
        : await devwispClient.manageDeepseekRuntime(token, operation)
      setStatus(value)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '操作失败，请重试。') }
    finally { setPending(false) }
  }
  return <section className="native-config__group harness-setup" aria-label="Harness 运行组件">
    <h3>运行组件</h3>
    <p role="status">{status?.busy ? '正在安装或检查，请稍候…' : status?.checked ? '运行就绪' : status?.installed ? '已安装，可检查运行状态' : '尚未就绪，请安装运行组件'}</p>
    <small>默认由 DevWisp 管理匹配版本，无需指定安装盘符。API 配置在下方保存。</small>
    <div className="native-config__actions">
      <button type="button" className="theme-notification-toggle" disabled={busy || !status} onClick={() => void run('install')}>{status?.installed ? '修复安装' : '安装运行组件'}</button>
      <button type="button" className="theme-notification-toggle" disabled={busy || !status?.installed} onClick={() => void run('check')}>检查 Harness 启动</button>
    </div>
    {status?.message ? <p role="status">{status.message}</p> : null}
    {error ? <p role="alert" className="native-config__message--error">{error}</p> : null}
    <details><summary>高级：使用外部安装</summary>
      <p>默认使用 SDK 自带组件。仅当你需要外部原生 Harness 时填写绝对路径，保存前会检查兼容性。留空恢复自动管理。</p>
      <label className="native-config__field"><span>外部程序路径</span><input value={path} placeholder={status?.executable || '留空：自动管理'} onChange={(event) => setPath(event.target.value)} disabled={busy} /></label>
      <button type="button" className="theme-notification-toggle" disabled={busy || !status} onClick={() => void run('save')}>验证并保存运行方式</button>
      <p>当前方式：{status?.executable || '自动管理'}</p>
      <p>SDK：{status?.sdkVersion || '未安装'} · 要求版本：{status?.requiredVersion}</p>
      <p className="harness-setup__path">Python：{status?.python}</p>
    </details>
  </section>
}
