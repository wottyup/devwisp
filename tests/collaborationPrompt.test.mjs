import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/collaborationPrompt.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText
const { isCollaborationPrompt } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const prompt = '你是协同群聊成员 Codex 2。群内成员：DeepSeek 2, Codex 2。\n本轮按加入顺序各回复一次。\n项目目录：/work\n\n群内最近记录：\n[用户]\n测试\n\n用户本轮要求：\n测试'

test('recognizes historical member envelopes with either newline format', () => {
  assert.equal(isCollaborationPrompt(prompt), true)
  assert.equal(isCollaborationPrompt(prompt.replaceAll('\n', '\r\n')), true)
})

test('keeps normal discussion, quoted envelopes and incomplete headers expanded', () => {
  for (const text of ['协同模式怎么加入？', `示例：\n${prompt}`, prompt.split('\n项目目录：')[0], '用户本轮要求：\n项目目录：/work']) {
    assert.equal(isCollaborationPrompt(text), false)
  }
})
