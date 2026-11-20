import { useEffect, useRef, useState, type ClipboardEvent } from 'react'
import { X } from 'lucide-react'
import { devwispClient, type CodexImageAttachment } from '../lib/devwispClient'
import { answerWithImageReferences, clipboardQuestionImages, maxQuestionImages, readQuestionImages } from '../lib/questionImages'
import { ui } from '../lib/uiLanguage'

export function useQuestionAnswerImages() {
  const [byQuestion, setByQuestion] = useState<Record<string, CodexImageAttachment[]>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const reading = useRef(false)
  const uploaded = useRef(new Map<CodexImageAttachment, { name: string; path: string }>())
  const current = useRef(byQuestion)
  current.current = byQuestion
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const paste = (key: string, event: ClipboardEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    const files = clipboardQuestionImages(event.clipboardData)
    if (!files.length) return
    event.preventDefault()
    event.stopPropagation()
    if (reading.current) return
    reading.current = true
    setBusy(true)
    setError('')
    const all = Object.values(current.current).flat()
    void (async () => {
      try {
        if (all.length + files.length > maxQuestionImages) throw new Error('每次回答最多添加 8 张图片')
        const existingBytes = all.reduce((sum, image) => sum + Math.ceil(image.data.length * 3 / 4), 0)
        if (existingBytes + files.reduce((sum, file) => sum + file.size, 0) > 24 * 1024 * 1024) throw new Error('图片总大小不能超过 24 MB')
        const images = await readQuestionImages(files)
        if (mounted.current) setByQuestion((value) => ({ ...value, [key]: [...(value[key] ?? []), ...images] }))
      } catch (reason) { if (mounted.current) setError(reason instanceof Error ? reason.message : '无法粘贴图片') }
      finally { reading.current = false; if (mounted.current) setBusy(false) }
    })()
  }
  const get = (key: string) => byQuestion[key] ?? []
  const has = (key: string) => get(key).length > 0
  const remove = (key: string, index: number) => setByQuestion((value) => ({ ...value, [key]: (value[key] ?? []).filter((_, i) => i !== index) }))
  const withReferences = async (token: string, key: string, text: string) => {
    const images = get(key)
    if (!images.length) return text.trim()
    const missing = images.filter((image) => !uploaded.current.has(image))
    if (missing.length) {
      const result = await devwispClient.uploadAnswerImages(token, missing)
      if (result.images.length !== missing.length) throw new Error('图片上传未完成，请重试')
      missing.forEach((image, index) => uploaded.current.set(image, result.images[index]))
    }
    return answerWithImageReferences(text, images.map((image) => uploaded.current.get(image)!))
  }
  return { busy, error, paste, get, has, remove, withReferences, isBusy: () => reading.current }
}

export function QuestionAnswerImages({ images, disabled, onRemove }: {
  images: CodexImageAttachment[]; disabled?: boolean; onRemove: (index: number) => void
}) {
  if (!images.length) return null
  return <div className="question-answer-images" aria-label={ui('回答图片')}>
    {images.map((image, index) => <div className="question-answer-images__item" key={index}>
      <img src={'data:' + image.mediaType + ';base64,' + image.data} alt={image.name} />
      <button type="button" disabled={disabled} onClick={() => onRemove(index)} aria-label={ui('删除图片')} title={ui('删除图片')}><X size={13} /></button>
    </div>)}
  </div>
}
