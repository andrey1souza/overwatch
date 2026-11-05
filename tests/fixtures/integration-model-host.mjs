/** Native plugin callbacks with the real Python checker and isolated repository files. */
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readdirSync, readFileSync, symlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { serialChecker } from '../../integrations/check.js'
import { createV1Hooks, setupV2 } from '../../integrations/opencode.js'
import { registerPi } from '../../integrations/pi.js'

/** The runner's output bytes for a model input, without deriving any expected decisions. */
export function buildOutput(names) {
  assert(Array.isArray(names), `runner names ${names} must be an array`)
  const body = names.map(name => `error: '${name}' failed:\n`).join('')
  const summary = names.length ? `Build Summary: 2/3 steps succeeded (1 failed); 0/${names.length} tests passed (${names.length} failed)\n`
    : 'Build Summary: 3/3 steps succeeded; 1/1 tests passed\n'
  assert(summary.endsWith('\n'), `runner summary ${summary} must close its line`)
  return body + summary
}

/** A pushable event subscription that acknowledges each event after the production loop handles it. */
function eventStream() {
  let deliver, acknowledge
  const source = {
    async *subscribe({ signal }) {
      while (!signal.aborted) {
        const event = await new Promise(resolve => { deliver = resolve; signal.addEventListener('abort', () => resolve(undefined), { once: true }) })
        if (event === undefined) return
        yield event
        acknowledge()
      }
    },
    async send(event) {
      assert(deliver !== undefined, `event subscription is not ready for ${event.type}`)
      await new Promise(resolve => { acknowledge = resolve; deliver(event) })
    },
  }
  assert(typeof source.subscribe === 'function', 'model event source needs subscribe()')
  return source
}

function piHost(project, check) {
  const handlers = {}, statuses = []
  registerPi({ on: (name, handler) => { handlers[name] = handler } }, check)
  const ctx = { cwd: project, sessionManager: { getSessionId: () => 'model' }, ui: { setStatus: (_key, text) => statuses.push(text) } }
  assert(handlers.session_start && handlers.agent_before_settle, 'Pi must register startup and settlement handlers')
  assert(statuses.length === 0, 'Pi must not publish a status before startup')
  return {
    start: () => handlers.session_start({ reason: 'startup' }, ctx),
    read: () => handlers.tool_result({ toolName: 'edit', toolCallId: 'model', input: {}, content: [] }, ctx),
    complete: async () => (await handlers.agent_before_settle({ entries: [] }, ctx))?.continue === true,
    publish: async () => (await handlers.tool_call({ toolName: 'Publish', input: {} }, ctx))?.block === true,
    status: () => statuses.at(-1),
    close: () => handlers.session_shutdown({}, ctx),
  }
}

function v1Host(project, check, outputs) {
  const warnings = []
  const hooks = createV1Hooks({ directory: project, client: { tui: { showToast: async ({ body }) => warnings.push(body.message) } } }, check)
  assert(typeof hooks.event === 'function', 'OpenCode V1 needs its event hook')
  assert(warnings.length === 0, 'OpenCode V1 must not warn before an event')
  return {
    start: () => hooks.event({ event: { type: 'session.created', sessionID: 'model' } }),
    read: () => hooks['tool.execute.after']({ tool: 'edit', sessionID: 'model', args: {} }, { output: 'edited' }),
    complete: async () => {
      const before = warnings.length
      await hooks.event({ event: { type: 'session.idle', sessionID: 'model' } })
      const guarded = outputs.at(-1).decision === 'block'
      assert.equal(warnings.length - before, guarded ? 1 : 0)
      return guarded
    },
    publish: async () => {
      try { await hooks['tool.execute.before']({ tool: 'Publish', sessionID: 'model' }, { args: {} }); return false }
      catch (error) { assert.match(error.message, /guarded/); return true }
    },
    close: () => {},
  }
}

async function v2Host(project, check, outputs) {
  const hooks = {}, prompts = [], events = eventStream()
  const close = await setupV2({ location: { directory: project },
    session: { hook: async (name, fn) => { hooks[name] = fn }, prompt: async event => prompts.push(event) },
    tool: { hook: async (name, fn) => { hooks[name] = fn } }, event: events }, check)
  assert(hooks.context && hooks['execute.after'], 'OpenCode V2 needs context and editing hooks')
  assert(prompts.length === 0, 'OpenCode V2 must not queue a prompt at setup')
  return {
    start: () => hooks.context({ sessionID: 'model', system: [] }),
    read: () => hooks['execute.after']({ tool: 'edit', sessionID: 'model', input: {} }),
    complete: async () => {
      const before = prompts.length
      await events.send({ type: 'session.idle', sessionID: 'model' })
      const guarded = outputs.at(-1).decision === 'block'
      assert.equal(prompts.length - before, guarded ? 1 : 0)
      return guarded
    },
    publish: async () => {
      try { await hooks['execute.before']({ tool: 'Publish', sessionID: 'model', input: {} }); return false }
      catch (error) { assert.match(error.message, /guarded/); return true }
    }, close,
  }
}

/** All filesystem and environment changes belong to the node test's cleanup lifecycle. */
export async function integrationHost(t, kind) {
  const root = mkdtempSync(join(tmpdir(), 'overwatch-model-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const project = join(root, 'project'), data = join(root, 'data')
  mkdirSync(project)
  execFileSync('git', ['init', '-q', project])
  writeFileSync(join(project, '.git/HEAD'), 'a'.repeat(40) + '\n')
  writeFileSync(join(project, 'build.zig'), 'initial')
  writeFileSync(join(project, '.overwatch.json'), JSON.stringify({ baseline: ['sh', '-c', 'cat result; if [ -f overlap ]; then echo changed > build.zig; rm overlap; fi'], gate: { protected_actions: [{ tool: 'Publish' }] } }))
  const previous = { PATH: process.env.PATH, PLUGIN_DATA: process.env.PLUGIN_DATA }
  const python = execFileSync('python3', ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8' }).trim()
  const bin = join(root, 'bin')
  mkdirSync(bin)
  symlinkSync(python, join(bin, 'python3'))
  process.env.PATH = `${bin}:/usr/bin:/bin`
  process.env.PLUGIN_DATA = data
  t.after(() => { for (const [key, value] of Object.entries(previous)) { if (value === undefined) delete process.env[key]; else process.env[key] = value } })
  const backend = serialChecker(), outputs = []
  const check = async event => { const output = await backend(event); outputs.push(output); return output }
  const host = kind === 'pi' ? piHost(project, check) : kind === 'opencode1' ? v1Host(project, check, outputs) : await v2Host(project, check, outputs)
  t.after(host.close)
  assert(project !== data, `model state ${data} must be outside project ${project}`)
  assert(outputs.length === 0, 'creating an integration must not run a check')
  return { ...host,
    build: names => writeFileSync(join(project, 'result'), buildOutput(names)),
    edit: version => writeFileSync(join(project, 'build.zig'), `version ${version}`),
    overlap: () => writeFileSync(join(project, 'overlap'), ''),
    move: () => writeFileSync(join(project, '.git/HEAD'), 'b'.repeat(40) + '\n'),
    report: () => {
      const name = readdirSync(data).find(name => name.endsWith('.json') && !name.endsWith('.baseline.json'))
      assert(name, `checker saved no state under ${data}`)
      return JSON.parse(readFileSync(join(data, name), 'utf8')).state.reports[0]
    },
  }
}
