import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../../src/lib/questionImages.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText
const { clipboardQuestionImages, readQuestionImages, answerWithImageReferences } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'))

test('plain text paste is not consumed, clipboard files are not duplicated as items', () => {
  assert.deepEqual(clipboardQuestionImages({ files: [], items: [{ kind: 'string', type: 'text/plain' }] }), [])
  const file = { name: 'screenshot.png', type: 'image/png', size: 4 }
  assert.deepEqual(clipboardQuestionImages({ files: [file], items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }] }), [file])
  assert.deepEqual(clipboardQuestionImages({ files: [], items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }] }), [file])
})

test('pasted image data is kept as a real attachment with file name and media type', async () => {
  globalThis.FileReader = class {
    readAsDataURL() { this.result = 'data:image/png;base64,YWJjZA=='; this.onload() }
  }
  assert.deepEqual(await readQuestionImages([{ name: 'screenshot.png', type: 'image/png', size: 4 }]),
    [{ name: 'screenshot.png', mediaType: 'image/png', data: 'YWJjZA==' }])
  delete globalThis.FileReader
})

test('invalid formats, counts, sizes and read failures cannot become answers', async () => {
  await assert.rejects(readQuestionImages(Array(9).fill({ size: 1, type: 'image/png' })), /8/)
  await assert.rejects(readQuestionImages([{ size: 1, type: 'image/svg+xml' }]), /PNG/)
  await assert.rejects(readQuestionImages([{ size: 9 * 1024 * 1024, type: 'image/png' }]), /8 MB/)
  await assert.rejects(readQuestionImages(Array(4).fill({ size: 7 * 1024 * 1024, type: 'image/png' })), /24 MB/)
  globalThis.FileReader = class { readAsDataURL() { this.onerror() } }
  await assert.rejects(readQuestionImages([{ name: 'bad.png', size: 1, type: 'image/png' }]), /重新粘贴/)
  delete globalThis.FileReader
})

test('tool answers contain readable quoted paths, not base64 blobs, including image-only replies', () => {
  const image = { name: 'screen.png', path: 'F:\\images\\screen.png' }
  const value = answerWithImageReferences('  see this  ', [image])
  assert.ok(value.startsWith('see this\n'))
  assert.ok(value.includes(JSON.stringify(image.path)))
  assert.ok(answerWithImageReferences('', [image]).includes(JSON.stringify(image.path)))
  assert.equal(answerWithImageReferences('  text only  ', []), 'text only')
})
