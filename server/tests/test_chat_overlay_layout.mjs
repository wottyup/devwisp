import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'

const source = await readFile(new URL('../../src/lib/chatOverlayLayout.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText
const { clampOverlayRect, defaultOverlayRect, parseOverlayLayout, resizeOverlayRect } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)

test('saved layouts survive serialization without accepting malformed browser storage', () => {
  const layout = { version: 1, rect: { x: 110, y: 60, width: 640, height: 420 }, maximized: true }
  assert.deepEqual(parseOverlayLayout(JSON.stringify(layout)), layout)
  for (const raw of [null, '', '{', 'null', '[]', '{}', JSON.stringify({ ...layout, version: 2 }), JSON.stringify({ ...layout, rect: { ...layout.rect, width: -1 } }), '{"version":1,"maximized":false,"rect":{"x":0,"y":0,"width":1e400,"height":200}}']) {
    assert.equal(parseOverlayLayout(raw), null)
  }
})

test('defaults and restored windows fit even a tiny or temporarily hidden chat pane', () => {
  for (const bounds of [{ width: 0, height: 0 }, { width: 160, height: 100 }, { width: 700, height: 450 }, { width: 1800, height: 1100 }]) {
    for (const preferred of [defaultOverlayRect(bounds), { x: -800, y: 900, width: 1500, height: 1200 }, { x: 5000, y: -50, width: 10, height: 10 }]) {
      const rect = clampOverlayRect(preferred, bounds)
      assert.ok(Object.values(rect).every(Number.isFinite))
      assert.ok(rect.x >= 0 && rect.y >= 0 && rect.width >= 0 && rect.height >= 0)
      assert.ok(rect.x + rect.width <= bounds.width)
      assert.ok(rect.y + rect.height <= bounds.height)
    }
  }
})

test('temporary viewport clamping preserves the larger preferred rectangle', () => {
  const preferred = { x: 280, y: 160, width: 700, height: 500 }
  const original = structuredClone(preferred)
  const small = clampOverlayRect(preferred, { width: 500, height: 300 })
  assert.deepEqual(small, { x: 0, y: 0, width: 500, height: 300 })
  assert.deepEqual(preferred, original)
  assert.deepEqual(clampOverlayRect(preferred, { width: 1200, height: 800 }), original)
})

test('resizing stops at the edge without moving the opposite corner', () => {
  const start = { x: 200, y: 150, width: 400, height: 300 }
  const bounds = { width: 1000, height: 700 }
  assert.deepEqual(resizeOverlayRect(start, 5000, 5000, bounds), { x: 200, y: 150, width: 800, height: 550 })
  assert.deepEqual(resizeOverlayRect(start, -5000, -5000, bounds), { x: 200, y: 150, width: 320, height: 180 })
  assert.deepEqual(start, { x: 200, y: 150, width: 400, height: 300 })
})

test('height-only resizing preserves window width and position', () => {
  const start = { x: 180, y: 90, width: 500, height: 300 }
  assert.deepEqual(resizeOverlayRect(start, 0, 100, { width: 1000, height: 700 }), { ...start, height: 400 })
})
