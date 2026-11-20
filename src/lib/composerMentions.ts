/** Suggest only leading addressees, so prose, emails and code keep normal editing. */
export function composerMentionQuery(text: string, caret: number, names: string[]) {
  const prefix = text.slice(0, caret)
  const start = prefix.lastIndexOf('@')
  if (start < 0 || /[\r\n]/.test(prefix.slice(start))) return null
  const allowed = [...new Set([...names, ...names.map((name) => name.replace(/ \d+$/, '')), 'all'])]
    .sort((left, right) => right.length - left.length)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
  if (!new RegExp(`^\\s*(?:@(?:${allowed})[\\s,，:：;；。]+)*$`, 'i').test(prefix.slice(0, start))) return null
  return { start, end: caret, query: prefix.slice(start + 1).toLocaleLowerCase() }
}

export function insertComposerMention(text: string, range: { start: number; end: number }, name: string) {
  const value = `@${name} `
  return { text: text.slice(0, range.start) + value + text.slice(range.end), caret: range.start + value.length }
}
