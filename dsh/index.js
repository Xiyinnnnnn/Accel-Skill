import { defineTool } from '@deepseek-ai/dsh-tools'
import { makeTools } from './tools.js'

export const name = 'accel-skill'
export const inject = ['tools']

export function apply(ctx, config = {}) {
  for (const tool of makeTools(defineTool, config)) ctx.tools.register(tool)
}
