import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { DevWispPage } from './components/DevWispPage'
import { PairingPage } from './components/PairingPage'
import { devwispClient, RemoteApiError, type CodexThread, type CollaborationTask } from './lib/devwispClient'
import { letterSpacingStyle, readThemePreferences, saveThemePreferences, themeRootStyle, type ThemePreferences } from './lib/themePreferences'

const STORAGE_KEY = 'devwisp-token'

export default function App() {
  const [token, setToken] = useState<string | null>(() => window.localStorage.getItem(STORAGE_KEY))
  const currentToken = useRef(token)
  currentToken.current = token
  const refreshRevision = useRef(0)
  const [surface, setSurface] = useState<'chat' | 'collaboration'>('chat')
  const [collaborationTasks, setCollaborationTasks] = useState<CollaborationTask[]>([])
  const [collaborationError, setCollaborationError] = useState('')
  const [collaborationId, setCollaborationId] = useState<string | null>(null)
  const [memberRequest, setMemberRequest] = useState<{ thread: CodexThread; sequence: number } | null>(null)
  const [themePreferences, setThemePreferences] = useState<ThemePreferences>(readThemePreferences)

  useEffect(() => {
    saveThemePreferences(themePreferences)
  }, [themePreferences])

  useLayoutEffect(() => {
    const root = document.documentElement
    const previousLetterSpacing = root.style.letterSpacing
    const previousDelta = root.style.getPropertyValue('--user-letter-spacing-delta')
    const spacing = letterSpacingStyle(themePreferences)
    root.style.letterSpacing = spacing.letterSpacing
    root.style.setProperty('--user-letter-spacing-delta', spacing['--user-letter-spacing-delta'])
    return () => {
      root.style.letterSpacing = previousLetterSpacing
      if (previousDelta) root.style.setProperty('--user-letter-spacing-delta', previousDelta)
      else root.style.removeProperty('--user-letter-spacing-delta')
    }
  }, [themePreferences.letterSpacingPercent])

  const paired = useCallback((nextToken: string) => {
    window.localStorage.setItem(STORAGE_KEY, nextToken)
    setToken(nextToken)
  }, [])

  const unpair = useCallback(async () => {
    const currentToken = token
    window.localStorage.removeItem(STORAGE_KEY)
    setToken(null)
    if (!currentToken) return
    try {
      await devwispClient.unpair(currentToken)
    } catch {
      // The local token is already removed. A later server restart cannot restore it in this browser.
    }
  }, [token])

  const refreshCollaboration = useCallback(async () => {
    if (!token) return
    const revision = ++refreshRevision.current
    try {
      const result = await devwispClient.collaborationTasks(token)
      if (currentToken.current !== token || revision !== refreshRevision.current) return
      setCollaborationTasks(result.tasks); setCollaborationError('')
    } catch (error) {
      if (currentToken.current !== token || revision !== refreshRevision.current) return
      if (error instanceof RemoteApiError && error.status === 401) void unpair()
      else setCollaborationError(error instanceof Error ? error.message : '读取任务失败')
    }
  }, [token, unpair])

  useEffect(() => {
    if (!token) {
      setCollaborationTasks([]); setCollaborationId(null); setMemberRequest(null)
      setCollaborationError(''); setSurface('chat'); return
    }
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      await refreshCollaboration()
      if (!stopped) timer = setTimeout(() => void poll(), surface === 'collaboration' ? 2000 : 10000)
    }
    void poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [token, surface, refreshCollaboration])

  return (
    <div className={`app-root app-root--theme-${themePreferences.theme}`} style={themeRootStyle(themePreferences)}>
      {!token ? <PairingPage onPaired={paired} /> : (
        <DevWispPage
          token={token}
          memberRequest={memberRequest}
          collaboration={{
            visible: surface === 'collaboration', selectedId: collaborationId, listError: collaborationError,
            onSelect: setCollaborationId, onRefresh: refreshCollaboration,
            onDissolved: (id) => {
              ++refreshRevision.current
              setCollaborationTasks((previous) => previous.filter((task) => task.id !== id))
              setCollaborationId((previous) => previous === id ? null : previous)
            },
            onOpenChat: (thread) => { if (thread) setMemberRequest((previous) => ({ thread, sequence: (previous?.sequence ?? 0) + 1 })); setSurface('chat') },
          }}
          collaborationTasks={collaborationTasks}
          onOpenCollaboration={(id) => { if (id !== undefined) setCollaborationId(id); setSurface('collaboration') }}
          onUnauthorized={unpair}
          onUnpair={unpair}
          themePreferences={themePreferences}
          onThemePreferencesChange={setThemePreferences}
        />
      )}
    </div>
  )
}
