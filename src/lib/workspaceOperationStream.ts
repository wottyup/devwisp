export interface WorkspaceOperationProgress {
  phase: 'scan' | 'prepare' | 'write'
  scannedFiles: number
  skippedFiles: number
  fileCount: number
  matchCount: number
  processedFiles?: number
  totalFiles?: number
}

export async function readWorkspaceOperation<T>(
  response: Response,
  onActivity: () => void,
  onProgress?: (progress: WorkspaceOperationProgress) => void,
  makeError: (status: number, detail: unknown) => Error = (_status, detail) => new Error(String(detail)),
): Promise<T> {
  // Keep compatibility while the frontend and backend are activated separately.
  if (!response.headers.get('Content-Type')?.includes('application/x-ndjson')) return await response.json() as T
  if (!response.body) throw new Error('目录操作连接已中断，请重新查找。')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  function parse(line: string): { result: T } | undefined {
    if (!line.trim()) return
    const message = JSON.parse(line) as Record<string, unknown>
    if (message.type === 'done') return { result: message.result as T }
    if (message.type === 'error') throw makeError(Number(message.status) || 500, message.detail)
    if (message.type === 'progress') onProgress?.(message as unknown as WorkspaceOperationProgress)
  }
  try {
    while (true) {
      const chunk = await reader.read()
      onActivity()
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      let newline = buffer.indexOf('\n')
      while (newline >= 0) {
        const message = parse(buffer.slice(0, newline))
        buffer = buffer.slice(newline + 1)
        if (message) return message.result
        newline = buffer.indexOf('\n')
      }
      if (chunk.done) {
        const message = parse(buffer)
        if (message) return message.result
        throw new Error('目录操作连接已中断，请重新查找。')
      }
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
