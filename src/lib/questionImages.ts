import type { CodexImageAttachment } from './devwispClient'

export const maxQuestionImages = 8
const types = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif'])

export function clipboardQuestionImages(data: Pick<DataTransfer, 'items' | 'files'>): File[] {
  const files = Array.from(data.files).filter((file) => file.type.startsWith('image/'))
  if (files.length) return files
  return Array.from(data.items).flatMap((item) => {
    const file = item.kind === 'file' && item.type.startsWith('image/') ? item.getAsFile() : null
    return file ? [file] : []
  })
}

export async function readQuestionImages(files: File[]): Promise<CodexImageAttachment[]> {
  if (files.length > maxQuestionImages) throw new Error('每次回答最多添加 8 张图片')
  if (files.reduce((sum, file) => sum + file.size, 0) > 24 * 1024 * 1024) throw new Error('图片总大小不能超过 24 MB')
  return Promise.all(files.map((file) => {
    if (!types.has(file.type)) throw new Error('仅支持 PNG、JPEG、WebP、GIF 图片')
    if (file.size > 8 * 1024 * 1024) throw new Error('单张图片不能超过 8 MB')
    return new Promise<CodexImageAttachment>((resolve, reject) => {
      const reader = new FileReader()
      reader.onerror = () => reject(new Error('无法读取图片，请重新粘贴'))
      reader.onload = () => {
        const value = String(reader.result ?? '')
        const comma = value.indexOf(',')
        if (comma < 0) { reject(new Error('图片数据无效')); return }
        resolve({ name: file.name || 'image', mediaType: file.type as CodexImageAttachment['mediaType'], data: value.slice(comma + 1) })
      }
      reader.readAsDataURL(file)
    })
  }))
}

export function answerWithImageReferences(text: string, images: { name: string; path: string }[]): string {
  if (!images.length) return text.trim()
  return [text.trim(), '已附加图片，请读取以下本机图片文件：', ...images.map((image) => JSON.stringify(image.path))].filter(Boolean).join('\n')
}
