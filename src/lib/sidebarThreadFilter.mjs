export function filterSidebarThreads(threads, engineFilter = null, search = '') {
  const normalizedSearch = search.trim().toLocaleLowerCase()
  return threads.filter((thread) => {
    const engine = thread.engine ?? (thread.id?.startsWith('deepseek:') ? 'deepseek-harness' : 'codex')
    if (engineFilter && !(thread.participantEngines ?? [engine]).includes(engineFilter)) return false
    if (!normalizedSearch) return true
    return [thread.name, thread.preview]
      .some((value) => value?.toLocaleLowerCase().includes(normalizedSearch))
  })
}
