// Task rows are navigation entries, never native engine conversation IDs.
export function mergeSidebarConversations(threads, tasks) {
  const members = new Set(tasks.flatMap((task) => Object.values(task.members).map((member) => member.threadId).filter(Boolean)))
  return [
    ...threads.filter((thread) => !members.has(thread.id)),
    ...tasks.map((task) => ({
      id: `collaboration:${task.id}`,
      collaboration: task,
      participantEngines: Object.values(task.members).map((member) => member.engine),
      name: task.title,
      preview: task.goal,
      cwd: task.cwd,
      createdAt: task.createdAt,
      updatedAt: task.updatedAt,
    })),
  ].sort((left, right) => timestamp(right.updatedAt ?? right.createdAt) - timestamp(left.updatedAt ?? left.createdAt))
}

function timestamp(value) {
  const numeric = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : NaN
  return Number.isFinite(numeric) ? numeric > 10_000_000_000 ? numeric : numeric * 1000 : Date.parse(value ?? '') || 0
}
