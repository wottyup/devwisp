import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/composerMentions.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } }).outputText
const { composerMentionQuery, insertComposerMention } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const names = ['Codex 1', 'Codex 2', 'Claude 1', 'DeepSeek 1', '所有人']

test('suggests leading members and filters the typed name including its number', () => {
  assert.equal(composerMentionQuery('@', 1, names).query, '')
  assert.equal(composerMentionQuery('  @cOdEx ', 9, names).query, 'codex ')
  assert.equal(composerMentionQuery('@Codex 2 @Cl', 12, names).query, 'cl')
  assert.equal(composerMentionQuery('@Codex 2，@', 10, names).query, '')
})

test('does not suggest mentions inside prose, email, code or after the request', () => {
  for (const value of ['email@example.com', '看看 @Codex', '```\n@Codex', '@Codex 1 请核实 @Claude']) {
    assert.equal(composerMentionQuery(value, value.length, names), null)
  }
})

test('insertion replaces only the active mention and preserves the following request', () => {
  const value = '@Codex 1 @De 请核实附件'
  const range = composerMentionQuery(value, 12, names)
  const result = insertComposerMention(value, range, 'DeepSeek 1')
  assert.equal(result.text, '@Codex 1 @DeepSeek 1  请核实附件')
  assert.equal(result.caret, '@Codex 1 @DeepSeek 1 '.length)
})
