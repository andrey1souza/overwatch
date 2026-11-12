import type { On } from 'claude-code'
import { mock } from 'claude-code/testing'
import { assert } from '../../hooks/assert'

export const start = { cwd: '/repo', surface: 'terminal' as const, isInteractive: true }
export const ended = { turnId: 't1', answer: '', durationMs: 1, isAborted: false, reason: 'answer' as const }
export const CLEAN = 'zanity: checked 1 file, no issues found.\n'
export const WARNINGS = 'zanity: 0 errors and 29 warnings in 1 of 1 files\n'
const UNCOMMITTED = 'the working tree has uncommitted changes that a revert would discard'
const COMMIT = 'c'.repeat(40)
const EMISSIONS = 60
const EMISSION_MS = 450
const ZIG_SLOWNESS = 4
const ZIG_GREEN = 'Build Summary: 3/3 steps succeeded; 1/1 tests passed\n'
export const ZIG_RED = 'Build Summary: 2/3 steps succeeded (1 failed); 0/1 tests passed (1 failed)\n'

type Options = { settings?: Record<string, unknown>; initial?: string; withUpstream?: boolean; withZig?: boolean; reasonDelayMs?: number }

/** What the fake checkpoint command line knows about the repository, and what it was asked to do. */
export type Workspace = {
  fingerprint: string
  committed: string
  dirty: string[]
  refusal: string | undefined
  reasonOverride: string | null | undefined
  commits: string[]
  reverted: string[][]
  asked: string[]
  next: { files: string[]; fingerprint: string } | undefined
  onRevert: () => void
}

/** A real hook chain whose watcher results and git repository are controlled by the test. */
export function tcrProject(on: On, options: Options = {}) {
  const { settings = { mode: 'tcr' }, initial = CLEAN, withUpstream = false, withZig = false, reasonDelayMs = 0 } = options
  assert(initial.startsWith('zanity: '), `initial output ${JSON.stringify(initial)} must be a zanity summary`)
  assert(typeof settings === 'object' && settings !== null, `the project's settings ${JSON.stringify(settings)} must be an object`)
  const session = mock.session(on)
  const clock = mock.clock(on)
  const prompts: string[] = []
  const toasts: string[] = []
  const workspace: Workspace = { fingerprint: 'clean', committed: 'clean', dirty: [], refusal: undefined, reasonOverride: undefined, commits: [], reverted: [], asked: [], next: undefined, onRevert: () => { output = initial } }
  let output = initial
  let zigOutput = ZIG_GREEN
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', (_$, e) => { toasts.push(JSON.stringify(e)); return { value: undefined } })
  mock.env(on, { PATH: '/usr/bin' })
  on('fs.read', ($, e) => (e.path.endsWith('/.overwatch.json') ? { value: JSON.stringify(settings) } : { deny: 'missing' }))
  on('fs.exists', ($, e) => ({ value: ['/repo/.git', '/usr/bin/zanity', ...(withUpstream ? ['/usr/bin/git'] : []), ...(withZig ? ['/repo/build.zig'] : [])].includes(e.path) }))
  on('process.spawn', processes({ workspace, clock, output: () => output, zig: () => zigOutput, reportsInitial: initial, reasonDelayMs }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.end', ($, e) => ({ sessionId: e.sessionId }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('tool.call', () => {
    if (workspace.next !== undefined) {
      workspace.dirty = [...new Set([...workspace.dirty, ...workspace.next.files])]
      workspace.fingerprint = workspace.next.fingerprint
      workspace.next = undefined
    }
    return { result: 'ok' }
  })
  on('prompt.submit', ($, e) => { prompts.push(e.text); return { text: e.text } })
  return {
    clock,
    prompts,
    toasts,
    workspace,
    appended: () => session.appended().map(row => JSON.stringify(row.message.content)),
    setReport: (report: string) => { output = report },
    setZigReport: (report: string) => { zigOutput = report },
    /** The agent's next tool call changes these files, leaving the workspace with this fingerprint. */
    willChange: (files: string[], fingerprint: string) => { workspace.next = { files, fingerprint } },
  }
}

type Processes = { workspace: Workspace; clock: ReturnType<typeof mock.clock>; output: () => string; zig: () => string; reportsInitial: string; reasonDelayMs: number }

/** The processes the plugin starts: the checkpoint command, the upstream check, and the watcher, which reports once a second. */
function processes({ workspace, clock, output, zig, reportsInitial, reasonDelayMs }: Processes) {
  assert(reportsInitial.startsWith('zanity: '), `the watcher starts with ${JSON.stringify(reportsInitial)}, not a zanity summary`)
  assert(workspace.asked.length === 0, `the processes start after the checkpoint command was asked ${workspace.asked}`)
  return async function* ($: unknown, e: { argv: readonly string[] }) {
    if (e.argv[1]?.endsWith('checkpoint.py')) {
      const command = e.argv[2] ?? ''
      workspace.asked.push(command)
      const { code, answer } = answerTo(workspace, command)
      if (command === 'reason' && reasonDelayMs > 0) await clock.sleep(reasonDelayMs)
      yield { stream: 'stdout' as const, text: `${JSON.stringify(answer)}\n` }
      return { value: { code, signal: null } }
    }
    if (e.argv[0] === 'zig') {
      yield { stream: 'stdout' as const, text: ZIG_GREEN }
      for (let tick = 1; tick <= EMISSIONS; tick++) {
        await clock.sleep(EMISSION_MS)
        yield { stream: 'stdout' as const, text: tick % ZIG_SLOWNESS === 0 ? zig() : '\n' }
      }
      return { value: { code: 0, signal: null } }
    }
    if (e.argv[0] === 'sh') {
      yield { stream: 'stdout' as const, text: 'no upstream to track\n' }
      for (let tick = 0; tick < EMISSIONS; tick++) {
        await clock.sleep(EMISSION_MS)
        yield { stream: 'stdout' as const, text: '\n' }
      }
      return { value: { code: 0, signal: null } }
    }
    yield { stream: 'stdout' as const, text: reportsInitial }
    for (let report = 0; report < EMISSIONS; report++) {
      await clock.sleep(EMISSION_MS)
      yield { stream: 'stdout' as const, text: output() }
    }
    return { value: { code: 0, signal: null } }
  }
}

/** What the fake checkpoint command answers to command, and its exit code. */
function answerTo(workspace: Workspace, command: string): { code: number; answer: Record<string, unknown> } {
  assert(['reason', 'fingerprint', 'checkpoint', 'revert'].includes(command), `the plugin asked the checkpoint command for ${JSON.stringify(command)}; it knows reason, fingerprint, checkpoint and revert`)
  if (command === 'reason') return { code: 0, answer: { reason: workspace.reasonOverride !== undefined ? workspace.reasonOverride : workspace.dirty.length > 0 ? UNCOMMITTED : null } }
  if (command === 'fingerprint') return { code: 0, answer: { fingerprint: workspace.fingerprint } }
  if (command === 'checkpoint') {
    if (workspace.refusal !== undefined) return { code: 1, answer: { error: workspace.refusal } }
    if (workspace.dirty.length === 0) return { code: 0, answer: { commit: null } }
    workspace.commits.push(`tcr: ${workspace.dirty.join(', ')}`)
    workspace.dirty = []
    workspace.committed = workspace.fingerprint
    return { code: 0, answer: { commit: COMMIT } }
  }
  const restored = workspace.dirty
  if (restored.length > 0) {
    workspace.reverted.push(restored)
    workspace.dirty = []
    workspace.fingerprint = workspace.committed
    workspace.onRevert()
  }
  assert(workspace.dirty.length === 0 && (restored.length === 0 || workspace.fingerprint === workspace.committed), `a revert left ${workspace.dirty} dirty at ${workspace.fingerprint}, not ${workspace.committed}`)
  return { code: 0, answer: { restored } }
}
