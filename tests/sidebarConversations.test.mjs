import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mergeSidebarConversations } from '../src/lib/sidebarConversations.mjs'
import { filterSidebarThreads } from '../src/lib/sidebarThreadFilter.mjs'
import { groupSidebarThreads } from '../src/lib/sidebarThreadGroups.mjs'

const task = {
  id: 'task-1', title: 'Fix shared navigation', goal: 'Keep drafts intact', cwd: '/project',
  createdAt: '2026-11-19T08:00:00Z', updatedAt: '2026-11-19T10:00:00Z',
  members: {
    coder: { engine: 'claude-code', model: 'model-a', threadId: 'claude:coder' },
    reviewer: { engine: 'codex', model: 'model-b', threadId: 'reviewer' },
  },
}

test('one shared list replaces managed members with their task and preserves native identities', () => {
  const native = { id: 'regular-chat', engine: 'codex', name: 'Ordinary chat', updatedAt: 1795082400 }
  const entries = mergeSidebarConversations([native, { id: 'claude:coder' }, { id: 'reviewer' }], [task])
  assert.equal(entries.length, 2)
  assert.equal(entries.find((entry) => !entry.collaboration), native)
  const team = entries.find((entry) => entry.collaboration)
  assert.equal(team.id, 'collaboration:task-1')
  assert.equal(team.collaboration, task)
  assert.notEqual(team.id, task.members.coder.threadId)
  assert.notEqual(team.id, task.members.reviewer.threadId)
})

test('a task matches every participating engine and combines that filter with goal or title search', () => {
  const entries = mergeSidebarConversations([{ id: 'other', engine: 'deepseek-harness' }], [task])
  for (const engine of ['codex', 'claude-code']) {
    assert.deepEqual(filterSidebarThreads(entries, engine, 'DRAFTS').map((entry) => entry.id), ['collaboration:task-1'])
  }
  assert.deepEqual(filterSidebarThreads(entries, 'deepseek-harness', 'navigation'), [])
})

test('two members using the same engine remain two participants', () => {
  const sameEngine = { ...task, members: { coder: task.members.coder, reviewer: { ...task.members.reviewer, engine: 'claude-code' } } }
  const [entry] = mergeSidebarConversations([], [sameEngine])
  assert.deepEqual(entry.participantEngines, ['claude-code', 'claude-code'])
  assert.equal(Object.keys(entry.collaboration.members).length, 2)
  assert.equal(filterSidebarThreads([entry], 'codex').length, 0)
})

test('task and ordinary chat dates sort together while pinned chats keep their own group', () => {
  const entries = mergeSidebarConversations([
    { id: 'pinned', updatedAt: '2026-11-18T08:00:00Z' },
    { id: 'recent', updatedAt: '2026-11-19T09:00:00Z' },
  ], [task])
  assert.deepEqual(entries.map((entry) => entry.id), ['collaboration:task-1', 'recent', 'pinned'])
  const groups = groupSidebarThreads(entries, new Set(['pinned']), new Date('2026-11-19T12:00:00Z'))
  assert.deepEqual(groups[0].threads.map((entry) => entry.id), ['pinned'])
  assert.deepEqual(groups[1].threads.map((entry) => entry.id), ['collaboration:task-1', 'recent'])
})
