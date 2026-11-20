import { Check } from 'lucide-react'
import type { ReactNode } from 'react'

export interface QuestionOption { value: string; label: string; description?: string }

/** Shared presentation only: each engine retains ownership of answer state and delivery. */
export function QuestionFields({ title, description, detail, options, selected, disabled, multiple, onToggle, children }: {
  title: string
  description?: string
  detail?: string
  options: QuestionOption[]
  selected: readonly string[]
  disabled?: boolean
  multiple?: boolean
  onToggle: (value: string) => void
  children?: ReactNode
}) {
  return <fieldset className="message-choices__group" disabled={disabled}>
    <legend>{title}</legend>
    {description ? <p className="message-choices__description">{description}</p> : null}
    {detail ? <pre className="message-choices__detail">{detail}</pre> : null}
    <div className="message-choices__options" role="group" aria-label={title}>
      {options.map((option) => {
        const checked = selected.includes(option.value)
        return <button key={option.value} type="button" className={'message-choices__option' + (checked ? ' message-choices__option--selected' : '')}
          disabled={disabled} aria-pressed={checked} onClick={() => onToggle(option.value)}>
          {checked ? <Check size={14} aria-hidden="true" /> : multiple ? <span className="message-choices__check" aria-hidden="true" /> : null}
          <span>{option.label}{option.description ? <small>{option.description}</small> : null}</span>
        </button>
      })}
    </div>
    {children}
  </fieldset>
}
