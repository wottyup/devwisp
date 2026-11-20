import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { afterEach, test } from 'node:test'
import ts from 'typescript'

async function moduleUrl(url) {
  const source = await readFile(url, 'utf8')
  let compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  }).outputText
  for (const match of [...compiled.matchAll(/from ['"]([^'"]+)['"]/g)]) {
    const dependency = match[1]
    const resolved = dependency.startsWith('.')
      ? await moduleUrl(new URL(`${dependency}.ts`, url))
      : import.meta.resolve(dependency)
    compiled = compiled.replace(match[0], `from '${resolved}'`)
  }
  return `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
}

const { devwispClient, RemoteApiError } = await import(await moduleUrl(new URL('../src/lib/devwispClient.ts', import.meta.url)))
const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })
function respond(detail, status = 404) {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail }), {
    status, headers: { 'Content-Type': 'application/json' },
  })
}

for (const [name, call] of [
  ['list', () => devwispClient.collaborationTasks('test-token')],
  ['create', () => devwispClient.createCollaborationGroup('test-token', { requestId: 'group-request' })],
]) {
  test(`explains an unloaded collaboration backend for ${name}`, async () => {
    respond('Not Found')
    await assert.rejects(call, (error) => error instanceof RemoteApiError
      && error.status === 404 && /后台尚未加载协同功能/.test(error.message)
      && /当前任务结束后/.test(error.message))
  })
}

test('preserves the missing-group error from an updated backend', async () => {
  respond('协同群聊不存在')
  await assert.rejects(() => devwispClient.collaborationTask('test-token', 'missing-group'),
    (error) => error.status === 404 && error.message === '协同群聊不存在')
})

test('keeps authentication errors available for pairing recovery', async () => {
  respond('需要配对码', 401)
  await assert.rejects(() => devwispClient.collaborationTasks('test-token'),
    (error) => error.status === 401 && error.message === '需要配对码')
})

test('does not relabel missing routes outside collaboration', async () => {
  respond('Not Found')
  await assert.rejects(() => devwispClient.status('test-token'),
    (error) => error.status === 404 && error.message === 'Not Found')
})

test('a retry reads the now-available backend after activation', async () => {
  respond('Not Found')
  await assert.rejects(() => devwispClient.collaborationTasks('test-token'))
  globalThis.fetch = async () => new Response(JSON.stringify({ tasks: [] }), {
    headers: { 'Content-Type': 'application/json' },
  })
  assert.deepEqual(await devwispClient.collaborationTasks('test-token'), { tasks: [] })
})
