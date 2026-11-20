import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import ts from 'typescript'
const source = await readFile(new URL('../../src/lib/workspaceOperationStream.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText
const {readWorkspaceOperation} = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const encode = new TextEncoder()
function response(text, chunkSize=1) {
  const bytes=encode.encode(text)
  return new Response(new ReadableStream({start(controller){
    for(let i=0;i<bytes.length;i+=chunkSize)controller.enqueue(bytes.slice(i,i+chunkSize))
    controller.close()
  }}),{headers:{'Content-Type':'application/x-ndjson'}})
}

test('decodes split UTF-8 and NDJSON chunks with progress and pings',async()=>{
  const text=[{type:'progress',phase:'scan',scannedFiles:20001,matchCount:72},{type:'ping'},{type:'done',result:{paths:['中文.txt'],matchCount:72}}].map(x=>JSON.stringify(x)).join('\n')+'\n'
  for(const size of [1,2,7,65536]){
    const progress=[]
    let activity=0
    const result=await readWorkspaceOperation(response(text,size),()=>activity++,p=>progress.push(p))
    assert.deepEqual(result,{paths:['中文.txt'],matchCount:72})
    assert.equal(progress.length,1)
    assert.equal(progress[0].scannedFiles,20001)
    assert.ok(activity>0)
  }
})

test('accepts a completed final record without a trailing newline',async()=>{
  assert.deepEqual(await readWorkspaceOperation(response('{"type":"done","result":{"matchCount":3}}'),()=>{}),{matchCount:3})
})

test('does not treat an interrupted stream or progress-only response as success',async()=>{
  for(const value of ['', '{"type":"ping"}\n','{"type":"progress","scannedFiles":25}\n']){
    await assert.rejects(readWorkspaceOperation(response(value),()=>{}),/连接已中断/)
  }
})

test('surfaces preflight errors without reporting replacement success',async()=>{
  await assert.rejects(readWorkspaceOperation(response('{"type":"error","status":409,"detail":"文件已变化"}\n'),()=>{},undefined,(status,detail)=>new Error(`${status}: ${detail}`)),/409: 文件已变化/)
})

test('retains compatibility with a JSON-only backend',async()=>{
  const result={fileCount:29,matchCount:72,truncated:false}
  assert.deepEqual(await readWorkspaceOperation(Response.json(result),()=>{}),result)
})

test('releases and cancels a streaming reader after an error',async()=>{
  let cancelled=false
  const body=new ReadableStream({start(c){c.enqueue(encode.encode('{bad json}\n'))},cancel(){cancelled=true}})
  await assert.rejects(readWorkspaceOperation(new Response(body,{headers:{'Content-Type':'application/x-ndjson'}}),()=>{}),SyntaxError)
  assert.equal(cancelled,true)
  assert.equal(body.locked,false)
})
