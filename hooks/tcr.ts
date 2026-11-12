import type { EngineInterface } from 'claude-code'
import { assert } from './assert'
import { OVERRIDE_PATH, type Override } from './watchers'

export const TCR_MODE = 'tcr'
export const TCR_ACTIVE = 'active'
/** The language of the upstream check, whose failure is another person's push, not an edit of the agent. */
export const UPSTREAM_LANGUAGE = 'git'
export const OUTSIDE_CHANGE = "files changed outside the agent's edits, and a revert would discard them"
const SHORT_COMMIT = 7
const CHECKPOINT_COMMAND = decodeURIComponent(new URL('./checkpoint.py', import.meta.url).pathname)

/**
 * What TCR knows between events. `status` is undefined before it is decided, TCR_ACTIVE, or the reason it cannot run.
 * `settled` is the fingerprint of the files at the latest checkpoint or revert. `pending` is the fingerprint after the
 * agent's latest edit that no result has settled yet, and `editedAt` is when that edit ended, which a result must follow.
 */
export type Tcr = { status: string | undefined; settled: string | undefined; pending: string | undefined; editedAt: number | undefined }

export function initialTcr(): Tcr {
  const tcr: Tcr = { status: undefined, settled: undefined, pending: undefined, editedAt: undefined }
  assert(tcr.status === undefined && tcr.pending === undefined, 'a new session starts with TCR undecided and nothing pending')
  assert(Object.keys(tcr).length === 4, `TCR starts with ${Object.keys(tcr)}; give it status, settled, pending and editedAt`)
  return tcr
}

/** The checkpoint command could not answer, or refused; the message says why. */
export class CommandError extends Error {}

/** The project's mode: TCR_MODE, or undefined when it names none. Any other mode is an error naming the field. */
export function readMode(override: Override): typeof TCR_MODE | undefined {
  const { mode } = override
  if (mode !== undefined && mode !== TCR_MODE) throw new CommandError(`${OVERRIDE_PATH} field 'mode' is ${JSON.stringify(mode)}; the only mode is "${TCR_MODE}"`)
  assert(mode === undefined || mode === TCR_MODE, `readMode accepted ${JSON.stringify(mode)}; accept only ${TCR_MODE}`)
  assert(mode === undefined || typeof mode === 'string', `the mode ${JSON.stringify(mode)} is not text`)
  return mode
}

/** What hooks/checkpoint.py answers to `command` for the repository at `root`, the one JSON object it prints. */
export async function ask($: EngineInterface, root: string, command: 'reason' | 'fingerprint' | 'checkpoint' | 'revert'): Promise<Record<string, unknown>> {
  assert(root.startsWith('/'), `the checkpoint command was asked about ${root}; pass the repository's absolute path`)
  assert(CHECKPOINT_COMMAND.startsWith('/') && !CHECKPOINT_COMMAND.includes('%20'), `the checkpoint command is at ${CHECKPOINT_COMMAND}, which is no path a process can run`)
  let text = ''
  let code: number | null
  try {
    const child = $.process.spawn({ argv: ['python3', CHECKPOINT_COMMAND, command, root], cwd: root })
    for await (const { text: part } of child) text += part
    ;({ code } = await child.result)
  } catch (error) {
    throw new CommandError(`python3 could not run ${CHECKPOINT_COMMAND}: ${String(error)}`)
  }
  let answer: unknown
  try {
    answer = JSON.parse(text)
  } catch {
    throw new CommandError(`${CHECKPOINT_COMMAND} ${command} printed ${JSON.stringify(text)}, not JSON`)
  }
  if (answer === null || typeof answer !== 'object') throw new CommandError(`${CHECKPOINT_COMMAND} ${command} printed ${JSON.stringify(text)}, not an object`)
  const printed = answer as Record<string, unknown>
  if (code !== 0) throw new CommandError(typeof printed.error === 'string' ? printed.error : `${CHECKPOINT_COMMAND} ${command} exited ${code}`)
  assert(code === 0 && printed.error === undefined, `${command} exited ${code} with the error ${JSON.stringify(printed.error)} but did not fail`)
  return printed
}

/** The fingerprint of the files of the repository at `root`. */
export async function fingerprintOf($: EngineInterface, root: string): Promise<string> {
  const { fingerprint } = await ask($, root, 'fingerprint')
  if (typeof fingerprint !== 'string' || fingerprint === '') throw new CommandError(`the checkpoint command gave the fingerprint ${JSON.stringify(fingerprint)}; it must be text`)
  assert(root.startsWith('/'), `the fingerprint of ${root} is for a relative path; pass the repository's absolute path`)
  assert(fingerprint === fingerprint.trim(), `the fingerprint ${JSON.stringify(fingerprint)} has spaces around it; the command prints it bare`)
  return fingerprint
}

/** TCR_ACTIVE, or the reason TCR cannot run in the repository at `root`. */
export async function stateOf($: EngineInterface, root: string): Promise<string> {
  const { reason } = await ask($, root, 'reason').catch(error => ({ reason: String(error.message) }))
  if (reason !== null && (typeof reason !== 'string' || reason === '')) throw new CommandError(`the checkpoint command gave the reason ${JSON.stringify(reason)}; it must be text or null`)
  const state = reason ?? TCR_ACTIVE
  assert(state !== '' && (state === TCR_ACTIVE) === (reason === null), `the state ${JSON.stringify(state)} disagrees with the reason ${JSON.stringify(reason)}`)
  assert(root.startsWith('/'), `the state of ${root} is for a relative path; pass the repository's absolute path`)
  return state
}

/** What the agent is told when the TCR state changed from `previous` to `next`; undefined when it did not. */
export function notice(previous: string | undefined, next: string): string | undefined {
  if (next === previous) return undefined
  const text = next === TCR_ACTIVE
    ? 'overwatch: TCR is on: an edit that keeps the checks green is committed, and an edit that adds a failure is reverted.'
    : `overwatch: TCR is unavailable: ${next}. Nothing is committed or reverted until it can start again at the start of a turn.`
  assert(text.startsWith('overwatch: TCR is '), `notice ${text} does not say it is about TCR`)
  assert((next === TCR_ACTIVE) === text.includes('is on'), `notice ${text} disagrees with the state ${next}`)
  return text
}

/** The short name of a commit, for the status line. */
export function shortCommit(commit: string): string {
  assert(/^[0-9a-f]{40}(?:[0-9a-f]{24})?$/.test(commit), `${JSON.stringify(commit)} is not a commit id; pass what the checkpoint command printed`)
  const short = commit.slice(0, SHORT_COMMIT)
  assert(short.length === SHORT_COMMIT && commit.startsWith(short), `the short name ${short} does not start ${commit}`)
  return short
}
