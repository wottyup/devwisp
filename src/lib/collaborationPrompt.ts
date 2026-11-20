// Match the complete server envelope, not ordinary mentions of collaboration.
export function isCollaborationPrompt(text: string): boolean {
  text = text.replace(/\r\n/g, '\n')
  return /^你是协同群聊成员 [^\n]+。群内成员：[^\n]+。\n/.test(text)
    && text.includes('\n项目目录：')
    && text.includes('\n\n群内最近记录：\n')
    && text.includes('\n\n用户本轮要求：\n')
}
