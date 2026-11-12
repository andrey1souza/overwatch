import { expect, test } from 'claude-code/testing'
import { assert } from '../hooks/assert'
import { CLEAN, WARNINGS, ZIG_RED, start, tcrProject } from './fixtures/tcr'

type Project = ReturnType<typeof tcrProject>
type Engine = Parameters<Parameters<typeof test>[1]>[0]
type On = Parameters<Parameters<typeof test>[1]>[1]

/** The engine and the hook registration a test is given. */
type Kit = { $: Engine; on: On }

const START_MS = 600
const SETTLE_MS = 1300
const OFFLINE = 'dns error: failed to lookup address information\nzanity: 1 error, 0 warnings in 1 of 1 files\n'
const END_MS = 500
const ZIG_WAIT_MS = 1500
const DECIDE_MS = 100

/** Runs body in a started session of a project that runs in TCR mode, and always ends the session. */
async function inSession({ $, on }: Kit, body: (project: Project) => Promise<void>, options: Parameters<typeof tcrProject>[1] = {}) {
  const project = tcrProject(on, options)
  assert(typeof $.session.start === 'function', 'the engine has no session to start')
  assert(project.workspace.asked.length === 0, `the project asked the checkpoint command before the session started: ${project.workspace.asked}`)
  try {
    await $.session.start(start)
    await project.clock.advance(START_MS)
    await body(project)
  } finally {
    const ending = $.session.end({ reason: 'prompt_input_exit', sessionId: 's1', resume: { id: 's1' } })
    await project.clock.advance(END_MS)
    await ending
  }
}

/** The agent edits the files, and the checks report what the project says next. */
async function edit($: Engine, project: Project, files: string[], fingerprint: string) {
  assert(files.length > 0 && fingerprint !== '', `an edit changes files and leaves a fingerprint; got ${files} and ${JSON.stringify(fingerprint)}`)
  project.willChange(files, fingerprint)
  await $.tool.call({ tool: 'Edit', file_path: `/repo/${files[0]}` })
  await project.clock.advance(SETTLE_MS)
  assert(project.workspace.next === undefined, `the edit of ${files} never reached the project; the tool call must apply it`)
}

test('an edit that keeps the checks green is committed as a checkpoint', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: example.py'])
    expect(project.workspace.reverted).toEqual([])
  })
})

test('an edit that adds a failure is reverted and the agent is told which failure and which files', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py', 'extra.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([['example.py', 'extra.py']])
    const told = project.prompts.filter(text => text.includes('TCR reverted'))
    expect(told.length).toBe(1)
    expect(told[0]).toContain('29 warnings')
    expect(told[0]).toContain('`example.py`')
    expect(told[0]).toContain('`extra.py`')
    expect(project.prompts.filter(text => text.includes('failed after the latest change'))).toEqual([])
  })
})

test('a failure the session started with is left alone', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: example.py'])
    expect(project.workspace.reverted).toEqual([])
  }, { initial: WARNINGS })
})

test('a failure that returns after a revert is reverted and told again', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    await $.turn.start({ text: project.prompts[0] ?? '', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f2')
    await project.clock.advance(END_MS)
    expect(project.workspace.reverted.length).toBe(2)
    expect(project.prompts.concat(project.appended()).filter(text => text.includes('TCR reverted')).length).toBe(2)
  })
})

test('a result made before the edit is not read as the edit\'s result', async ($, on) => {
  await inSession({ $, on }, async project => {
    await project.clock.advance(400)
    project.setReport(WARNINGS)
    project.willChange(['example.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/example.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([['example.py']])
  })
})

test('an edit waits for every check to report on it before it is committed', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual([])
    await project.clock.advance(ZIG_WAIT_MS)
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
  }, { withZig: true })
})

test('a new failure in any one check reverts the edit', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setZigReport(ZIG_RED)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(SETTLE_MS + ZIG_WAIT_MS)
    expect(project.workspace.reverted).toEqual([['b.py']])
  }, { withZig: true })
})

test('a check that cannot run holds the edit even while another check is green', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(OFFLINE)
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS + ZIG_WAIT_MS)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  }, { withZig: true })
})

test('a check that stops being able to run after its result still holds the edit', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(700)
    project.setReport(OFFLINE)
    await project.clock.advance(1000)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  }, { withZig: true })
})

test('an edit made as the decision is still being made is settled once it is', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(1)
    project.willChange(['a.py'], 'f1')
    const call = $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS)
    await call
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
  }, { reasonDelayMs: 100 })
})

test('a failure with nothing to restore is told as a failure and reverts nothing', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.willChange([], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    project.setReport(WARNINGS)
    await project.clock.advance(SETTLE_MS + END_MS)
    expect(project.workspace.reverted).toEqual([])
    const told = project.prompts.concat(project.appended())
    expect(told.filter(text => text.includes('TCR reverted'))).toEqual([])
    expect(told.some(text => text.includes('29 warnings'))).toBe(true)
  })
})

test('a tool that changes no file is never a checkpoint or a revert, whatever its name', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await $.tool.call({ tool: 'Bash', command: 'git status' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    expect(project.workspace.asked).not.toContain('checkpoint')
    expect(project.workspace.asked).not.toContain('revert')
  })
})

test('an edit made before the first result is settled by one result for both', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual(['tcr: a.py, b.py'])
  })
})

test('a result that arrives after the files changed again neither commits nor reverts', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.willChange(['example.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/example.py' })
    project.workspace.fingerprint = 'someone-else'
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    expect(project.prompts.filter(text => text.includes('TCR is unavailable')).length + project.appended().filter(text => text.includes('TCR is unavailable')).length).toBeGreaterThan(0)
  })
})

test('TCR does not start on a dirty tree and nothing is reverted', async ($, on) => {
  const project = tcrProject(on)
  project.workspace.dirty = ['mine.py']
  project.workspace.fingerprint = 'mine'
  try {
    await $.session.start(start)
    await project.clock.advance(START_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    await project.clock.advance(SETTLE_MS)
    expect(project.appended().concat(project.prompts).some(text => text.includes('TCR is unavailable'))).toBe(true)
  } finally {
    const ending = $.session.end({ reason: 'prompt_input_exit', sessionId: 's1', resume: { id: 's1' } })
    await project.clock.advance(END_MS)
    await ending
  }
})

test('files that change at a tool that does not edit pause TCR until the next turn start', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['mine.py'], 'mine')
    await $.tool.call({ tool: 'Read', file_path: '/repo/mine.py' })
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.reverted).toEqual([])
    expect(project.workspace.commits).toEqual([])
    project.workspace.dirty = []
    project.workspace.committed = project.workspace.fingerprint
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(CLEAN)
    await project.clock.advance(SETTLE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['again.py'], 'f2')
    expect(project.workspace.reverted).toEqual([['again.py']])
  })
})

test('a failed checkpoint commit pauses TCR and reverts nothing', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.refusal = 'the checkpoint commit failed and its changes are left staged: refused'
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(project.appended().concat(project.prompts).some(text => text.includes('TCR is unavailable') && text.includes('left staged'))).toBe(true)
    project.setReport(WARNINGS)
    await edit($, project, ['again.py'], 'f2')
    expect(project.workspace.reverted).toEqual([])
  })
})

test('a check that cannot run here neither commits nor reverts', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(OFFLINE)
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  })
})

test('the upstream check is not waited for and never causes a revert', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: example.py'])
  }, { withUpstream: true })
})

test('with inherited findings set to block, an edit that leaves one failing is neither committed nor reverted', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  }, { initial: WARNINGS, settings: { mode: 'tcr', gate: { inherited: 'block' } } })
})

test('without the mode nothing is committed, reverted or asked of git', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.asked).toEqual([])
    expect(project.workspace.commits).toEqual([])
  }, { settings: {} })
})

test('an unknown mode is told to the user and nothing is committed or reverted', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    expect(project.toasts.some(text => text.includes('mode') && text.includes('tcr'))).toBe(true)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  }, { settings: { mode: 'fast' } })
})

test('a revert is told to the agent in the running turn even after another message in that turn', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.willChange(['a.py'], 'f1')
    project.workspace.refusal = 'the checkpoint commit failed and its changes are left staged: refused'
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.appended().filter(text => text.includes('TCR is unavailable')).length).toBe(1)
    project.workspace.refusal = undefined
    project.workspace.dirty = []
    project.workspace.committed = project.workspace.fingerprint
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(DECIDE_MS)
    project.setReport('zanity: 0 errors and 3 warnings in 1 of 1 files\n')
    await edit($, project, ['example.py'], 'f2')
    expect(project.workspace.reverted).toEqual([['example.py']])
    expect(project.appended().filter(text => text.includes('TCR reverted')).length).toBe(1)
  })
})
