import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const MAX_OUT = 65536

function run(config, script, args, signal) {
  const key = config.key ?? process.env.DEEPSEEK_API_KEY ?? ''
  return new Promise((resolve2, reject) => {
    const child = spawn(config.python ?? 'python3', [script, ...args], {
      cwd: config.cwd ?? process.cwd(),
      env: { ...process.env, TRIAL_KEY: key, BOOSTER_KEY: key },
      stdio: ['ignore', 'pipe', 'pipe'],
      signal,
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (d) => { stdout += d; if (stdout.length > MAX_OUT) stdout = stdout.slice(-MAX_OUT) })
    child.stderr.on('data', (d) => { stderr += d; if (stderr.length > MAX_OUT) stderr = stderr.slice(-MAX_OUT) })
    child.on('error', reject)
    child.on('close', (code) => resolve2({ exitCode: code === null ? -1 : code, stdout, stderr }))
  })
}

function common(a, args) {
  a.push('--docs', args.docs, '--work', args.work)
  if (args.workers !== undefined) a.push('--workers', String(args.workers))
  if (args.maxTurns !== undefined) a.push('--max-turns', String(args.maxTurns))
  if (args.model) a.push('--model', args.model)
  if (args.apiUrl) a.push('--api-url', args.apiUrl)
  if (args.mock) a.push('--mock')
  if (args.seed !== undefined) a.push('--seed', String(args.seed))
  if (args.json) a.push('--json')
  return a
}

const output = {
  schema: { type: 'object', properties: { exitCode: { type: 'number' }, stdout: { type: 'string' }, stderr: { type: 'string' } } },
  render: (_a, v) => [{ type: 'text', text: `exit=${v.exitCode}\n${v.stdout}${v.stderr ? '\n[stderr]\n' + v.stderr : ''}` }],
}

export function makeTools(def, config = {}) {
  const trialScript = resolve(config.scripts?.trial ?? join(here, '..', 'Trial', 'trial.py'))
  const boosterScript = resolve(config.scripts?.booster ?? join(here, '..', 'Booster', 'booster.py'))
  return [
    def({
      name: 'accel_trial',
      description: 'Accel-Skill Trial: 大任务前 N 子代理并发头脑风暴, 三阶段文件权威收敛到唯一方案 final.md. 传 mock=true 可零成本自测; 真实调用 key 走插件环境变量, 不进模型上下文.',
      parameters: {
        docs: { type: 'string', required: true, description: '资料目录(只读)' },
        work: { type: 'string', required: true, description: '工作目录(md写入)' },
        goals: { type: 'string', description: '目标列表 | 分隔, 重复=同目标多并发' },
        goalsFile: { type: 'string', description: '目标文件路径, 每行一个' },
        goalsJson: { type: 'string', description: '目标JSON数组字符串' },
        workers: { type: 'number', description: '并发 0-256, 0=全并行(默认)' },
        maxTurns: { type: 'number', description: '单子代理最大LLM轮次, 0=无限(默认)' },
        model: { type: 'string', description: '模型名(默认走环境变量)' },
        apiUrl: { type: 'string', description: 'API地址(默认走环境变量)' },
        mock: { type: 'boolean', description: '模拟LLM不联网不耗key' },
        seed: { type: 'number', description: '随机种子(可复现)' },
        json: { type: 'boolean', description: '结构化输出' },
      },
      output,
      async execute(args, exec) {
        const a = common([], args)
        if (args.goals) a.push('--goals', args.goals)
        else if (args.goalsFile) a.push('--goals-file', args.goalsFile)
        else if (args.goalsJson) a.push('--goals-json', args.goalsJson)
        return run(config, trialScript, a, exec.signal)
      },
    }),
    def({
      name: 'accel_booster',
      description: 'Accel-Skill Booster: N×X 模块变体并发开发 + 测到死验收 + 组合审查, 收敛最优主体 final.md. 传 mock=true 可零成本自测; key 走插件环境变量.',
      parameters: {
        docs: { type: 'string', required: true, description: '资料目录(只读)' },
        work: { type: 'string', required: true, description: '工作目录(md写入)' },
        modules: { type: 'number', required: true, description: '任务数 N(模块数)' },
        perModule: { type: 'number', description: '每模块并发变体数 X(默认1)' },
        goal: { type: 'string', description: '主agent定下的大目标' },
        combos: { type: 'string', description: '组合方向, 逗号分隔(默认 best,stability,safety,lightweight)' },
        workers: { type: 'number', description: '并发 0-256, 0=全并行(默认)' },
        maxTurns: { type: 'number', description: '单子代理最大LLM轮次, 0=无限(默认)' },
        model: { type: 'string', description: '模型名(默认走环境变量)' },
        apiUrl: { type: 'string', description: 'API地址(默认走环境变量)' },
        mock: { type: 'boolean', description: '模拟LLM不联网不耗key' },
        seed: { type: 'number', description: '随机种子(可复现)' },
        authMode: { type: 'string', enum: ['ask', 'auto-deny', 'allow'], description: '授权模式(默认ask)' },
        json: { type: 'boolean', description: '结构化输出' },
      },
      output,
      async execute(args, exec) {
        const a = common(['--modules', String(args.modules)], args)
        if (args.perModule !== undefined) a.push('--per-module', String(args.perModule))
        if (args.goal) a.push('--goal', args.goal)
        if (args.combos) a.push('--combos', args.combos)
        if (args.authMode) a.push('--auth-mode', args.authMode)
        return run(config, boosterScript, a, exec.signal)
      },
    }),
  ]
}
