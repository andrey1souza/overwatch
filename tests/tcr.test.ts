import { expect, test } from 'claude-code/testing'
import { assert } from '../hooks/assert'
import { CHECK_TIMEOUT_MS, COMMAND_LIMITS_MS, JUDGEMENT_LIMIT_MS } from '../hooks/tcr'
import { CLEAN, WARNINGS, ZIG_RED, start, tcrProject, type Workspace } from './fixtures/tcr'
import { inSession, edit, during, personWrites, told, START_MS, SETTLE_MS, END_MS, DECIDE_MS, JUDGE_MS, WORSE, OFFLINE, type Project } from './fixtures/tcr-session'

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
    const reverted = project.prompts.filter(text => text.includes('TCR reverted'))
    expect(reverted.length).toBe(1)
    expect(reverted[0]).toContain('29 warnings')
    expect(reverted[0]).toContain('`example.py`')
    expect(reverted[0]).toContain('`extra.py`')
    expect(project.prompts.filter(text => text.includes('failed after the latest change'))).toEqual([])
  })
})

test('a watcher that stays silent for an edit does not hold the edit: its own run judges it', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['README.md'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: README.md'])
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f2')
    expect(project.workspace.reverted).toEqual([['example.py']])
  }, { silentWatcher: true })
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
    expect(project.workspace.reverted.length).toBe(2)
    expect(told(project).filter(text => text.includes('TCR reverted')).length).toBe(2)
  })
})

test('a tool that changes no file is never judged, a checkpoint or a revert, whatever its name', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await $.tool.call({ tool: 'Bash', command: 'git status' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.oneShots).toEqual([])
    expect(project.workspace.asked).not.toContain('checkpoint')
    expect(project.workspace.asked).not.toContain('revert')
  })
})

test('a tool that changes no file while an edit waits does not judge it again', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(300)
    await $.tool.call({ tool: 'Bash', command: 'git status' })
    await project.clock.advance(300)
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
    expect(project.workspace.oneShots).toEqual(['zanity'])
  })
})

test('two edits made before the first is judged are judged once, together', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(100)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual(['tcr: a.py, b.py'])
    expect(project.workspace.oneShots).toEqual(['zanity'])
  })
})

test('an edit made while the first is being judged cancels that judgement', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(JUDGE_MS + 100)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(SETTLE_MS + 400)
    expect(project.workspace.commits).toEqual(['tcr: a.py, b.py'])
  }, { judgeDelayMs: 300 })
})

test('each new edit makes the judgement wait again until the files are quiet', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(300)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(300)
    expect(project.workspace.oneShots).toEqual([])
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual(['tcr: a.py, b.py'])
  })
})

test('a judgement started before a newer edit is never read as that edit\'s', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(550)
    project.setReport(WARNINGS)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(1900)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([['a.py', 'b.py']])
  }, { judgeDelayMs: 300 })
})

test('files that change while the checks judge the edit are acted on by nothing', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    project.willChange(['example.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/example.py' })
    await project.clock.advance(JUDGE_MS + 100)
    project.workspace.fingerprint = 'someone-else'
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  }, { judgeDelayMs: 500 })
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
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable'))).toBe(true)
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
    await edit($, project, ['again.py'], 'f2')
    expect(project.workspace.reverted).toEqual([['again.py']])
  })
})

test('a person\'s change seen at a tool that does not edit, while an edit waits, is never committed or reverted as the agent\'s', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.willChange(['agent1.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/agent1.py' })
    project.willChange(['person.py'], 'p1')
    await $.tool.call({ tool: 'Read', file_path: '/repo/person.py' })
    project.willChange(['agent2.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/agent2.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.reverted).toEqual([])
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.dirty).toEqual(['agent1.py', 'person.py', 'agent2.py'])
  })
})

test('a failed checkpoint commit pauses TCR and reverts nothing', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.refusal = 'the checkpoint commit failed and its changes are left staged: refused'
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('left staged'))).toBe(true)
    project.setReport(WARNINGS)
    await edit($, project, ['again.py'], 'f2')
    expect(project.workspace.reverted).toEqual([])
  })
})

test('a check that cannot run here neither commits nor reverts, and the agent is told', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(OFFLINE)
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    expect(told(project).some(text => text.includes('these checks cannot run in this environment'))).toBe(true)
    expect(told(project).some(text => text.includes('TCR left the latest change uncommitted'))).toBe(true)
  })
})

test('the upstream check is not judged and never causes a revert', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: example.py'])
    expect(project.workspace.oneShots).toEqual(['zanity'])
  }, { withUpstream: true })
})

test('with inherited findings set to block, an edit that leaves one failing is neither committed nor reverted', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  }, { initial: WARNINGS, settings: { mode: 'tcr', gate: { inherited: 'block' } } })
})

test('without the mode nothing is judged, committed, reverted or asked of git', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.asked).toEqual([])
    expect(project.workspace.oneShots).toEqual([])
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

for (const [mode, wording] of [['foo', "field 'mode' is 'foo'; the only mode is 'tcr'"], [true, "field 'mode' is True; the only mode is 'tcr'"], [['tcr'], "field 'mode' is ['tcr']; the only mode is 'tcr'"]] as const) {
  test(`a mode of ${JSON.stringify(mode)} is reported in the Python hook's wording`, async ($, on) => {
    await inSession({ $, on }, async project => {
      await $.turn.start({ text: 'go', turnId: 't1' })
      await project.clock.advance(SETTLE_MS)
      expect(project.toasts.some(text => text.includes(`.overwatch.json ${wording}`))).toBe(true)
      expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes(`.overwatch.json ${wording}`))).toBe(true)
    }, { settings: { mode } })
  })
}

test('a new failure in any one check reverts the edit', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setZigReport(ZIG_RED)
    await edit($, project, ['b.py'], 'f2')
    expect(project.workspace.reverted).toEqual([['b.py']])
    expect(project.workspace.commits).toEqual([])
  }, { withZig: true })
})

test('an edit is committed only when every check judges it green', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
    expect(project.workspace.oneShots.sort()).toEqual(['zanity', 'zig'])
  }, { withZig: true })
})

test('an edit made while the decision is being made pauses TCR instead of being settled unseen', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(1)
    project.willChange(['a.py'], 'f1')
    const call = $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS)
    await call
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting or deciding'))).toBe(true)
  }, { reasonDelayMs: 100 })
})

test('a failure with nothing to restore is told as a failure and reverts nothing', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.willChange([], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS + END_MS)
    expect(project.workspace.reverted).toEqual([])
    expect(told(project).filter(text => text.includes('TCR reverted'))).toEqual([])
    expect(told(project).some(text => text.includes('29 warnings'))).toBe(true)
    expect(told(project).some(text => text.includes('TCR left the latest change uncommitted and unreverted') && text.includes('but no file differs from the latest checkpoint, so there is nothing to revert'))).toBe(true)
    expect(project.workspace.asked.filter(each => each === 'revert').length).toBe(1)
  })
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

test('a check with a baseline command is run to its end and judged by its exit code', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['a.rs'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: a.rs'])
    project.setRustCode(1)
    await edit($, project, ['b.rs'], 'f2')
    expect(project.workspace.reverted).toEqual([['b.rs']])
    expect(told(project).some(text => text.includes('TCR reverted') && text.includes('cargo test'))).toBe(true)
  }, { withRust: true, withoutChecks: true })
})

test('an edit undone before it is judged is not judged', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(100)
    project.workspace.dirty = []
    project.workspace.fingerprint = 'clean'
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.oneShots).toEqual([])
    expect(project.workspace.commits).toEqual([])
  })
})

test('a failure with no earlier result of its check to compare with is held and told, not reverted', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.reverted).toEqual([])
    expect(project.workspace.commits).toEqual([])
    expect(told(project).some(text => text.includes('overwatch: TCR held the edit: zanity failed and has no earlier result to compare with'))).toBe(true)
  }, { mute: true })
})

test('a green edit is committed even when its check has no earlier result', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
  }, { mute: true })
})

test('a message of overwatch that is not about TCR still reaches the agent in a turn that TCR already told', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(1500)
    expect(project.appended().some(text => text.includes('TCR is on'))).toBe(true)
    await project.clock.advance(1500)
    expect(project.appended().some(text => text.includes('exited, so build failures are not reported'))).toBe(true)
  }, { watcherEndsAfterTicks: 4 })
})

const SLOW = { OVERWATCH_TCR_TIMEOUT_SCALE: '0.005' }

test('an edit made while the checkpoint is being committed pauses TCR and says so', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(JUDGE_MS + 100)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing'))).toBe(true)
    expect(project.workspace.oneShots).toEqual(['zanity'])
  }, { commandDelayMs: 300 })
})

test('an edit made while the revert is running pauses TCR and says so', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(JUDGE_MS + 100)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting'))).toBe(true)
    expect(told(project).filter(text => text.includes('TCR reverted')).length).toBe(1)
  }, { commandDelayMs: 300 })
})

test('taking the mode away while the checks run stops the judgement and tells the agent TCR is off', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(JUDGE_MS)
    project.setSettings({})
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    expect(told(project).some(text => text.includes('TCR is off'))).toBe(true)
  }, { judgeDelayMs: 300 })
})

test('taking the mode away between two tools tells the agent TCR is off and judges nothing more', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setSettings({})
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.oneShots).toEqual([])
    expect(told(project).some(text => text.includes('TCR is off'))).toBe(true)
  })
})

test('an invalid mode set while TCR runs is reported and TCR does not act', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setSettings({ mode: 'fast' })
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    expect(project.toasts.some(text => text.includes('mode') && text.includes('fast'))).toBe(true)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('mode'))).toBe(true)
    expect(project.workspace.reverted).toEqual([])
    expect(project.workspace.commits).toEqual([])
  })
})

test('a setting of TCR that is no true or false is reported and TCR does not start', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(SETTLE_MS)
    expect(project.toasts.some(text => text.includes('tcr.saveReverted'))).toBe(true)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('tcr.saveReverted'))).toBe(true)
  }, { settings: { mode: 'tcr', tcr: { saveReverted: 'yes' } } })
})

test('a project without checks says TCR is unavailable because no checks are configured', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    await edit($, project, ['example.py'], 'f1')
    expect(told(project).some(text => text.includes('overwatch: TCR is unavailable: no checks are configured'))).toBe(true)
    expect(project.workspace.commits).toEqual([])
  }, { withoutChecks: true })
})

test('a check that writes files names them in the reason TCR is unavailable', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    await edit($, project, ['example.py'], 'f1')
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('the check zanity wrote files (coverage.xml)'))).toBe(true)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
  }, { checkWrites: ['coverage.xml'] })
})

test('a revert keeps the content in a ref by default and names how to get it back', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    const reverted = told(project).filter(text => text.includes('TCR reverted'))
    expect(reverted.length).toBe(1)
    expect(reverted[0]).toContain('refs/overwatch/tcr/1')
    expect(reverted[0]).toContain('git checkout refs/overwatch/tcr/1 -- .')
    expect(reverted[0]).not.toContain('is gone')
    expect(project.workspace.flags.filter(flags => flags.includes('--no-save-ref'))).toEqual([])
  })
})

test('a revert with saveReverted off keeps no copy, says the content is gone and tells the command so', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    const reverted = told(project).filter(text => text.includes('TCR reverted'))
    expect(reverted.length).toBe(1)
    expect(reverted[0]).toContain('is gone')
    expect(reverted[0]).not.toContain('git checkout')
    expect(project.workspace.flags.filter(flags => flags.includes('--no-save-ref')).length).toBe(1)
  }, { settings: { mode: 'tcr', tcr: { saveReverted: false } } })
})

test('a checkpoint that was refused pauses TCR, names each file with its rule, and commits and reverts nothing', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.refused = [{ path: '.env', rule: 'secret file name' }, { path: 'big.bin', rule: 'larger than 5 MB' }]
    await edit($, project, ['.env', 'big.bin'], 'f1')
    await project.clock.advance(SETTLE_MS)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    const said = told(project).filter(text => text.includes('TCR is unavailable') && text.includes('the checkpoint would commit'))
    expect(said.length).toBe(1)
    expect(said[0]).toContain('.env (secret file name)')
    expect(said[0]).toContain('big.bin (larger than 5 MB)')
    expect(said[0]).toContain('`tcr.allow` in .overwatch.json, or to .gitignore, and commit the pending edit yourself')
    expect(said[0]).toContain('nothing was committed or reverted')
  })
})

test('a refusal that found a line of a file names the file and the line, as the Python hook does', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.refused = [{ path: 'src/a.py', rule: 'AWS access key', line: 12 }, { path: '.env', rule: 'secret file name', line: null }]
    await edit($, project, ['src/a.py', '.env'], 'f1')
    const said = told(project).filter(text => text.includes('the checkpoint would commit'))
    expect(said.length).toBe(1)
    expect(said[0]).toContain('src/a.py:12 (AWS access key), .env (secret file name)')
  })
})

test('a refusal whose line is no line number is reported and pauses TCR', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.refused = [{ path: 'a.py', rule: 'AWS access key', line: 0 }]
    await edit($, project, ['a.py'], 'f1')
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('it must say'))).toBe(true)
  })
})

test('TCR resumes at the next turn start once the refused file is allowed or ignored', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.refused = [{ path: '.env', rule: 'secret file name' }]
    await edit($, project, ['.env'], 'f1')
    project.workspace.refused = []
    project.workspace.dirty = []
    project.workspace.committed = project.workspace.fingerprint
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(DECIDE_MS)
    await edit($, project, ['example.py'], 'f2')
    expect(project.workspace.commits).toEqual(['tcr: example.py'])
  })
})

test('a checkpoint answer that also lists no refusal is a plain commit', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.workspace.isNewShape = true
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: example.py'])
  })
})

test('a checkpoint answer whose commit is no sha is reported and pauses TCR', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.rawCheckpoint = { commit: 7 }
    await edit($, project, ['example.py'], 'f1')
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('it must say'))).toBe(true)
    expect(project.workspace.commits).toEqual([])
  })
})

test('a tcr setting that leaves saveReverted out still keeps the copy of what a revert removes', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(WARNINGS)
    await edit($, project, ['example.py'], 'f1')
    expect(told(project).some(text => text.includes('git checkout refs/overwatch/tcr/1 -- .'))).toBe(true)
    expect(project.workspace.flags.filter(flags => flags.includes('--no-save-ref'))).toEqual([])
  }, { settings: { mode: 'tcr', tcr: { allow: ['fixture.bin'] } } })
})

test('a checkpoint answer that is neither shape is reported and pauses TCR', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.refused = [{ path: 7 as unknown as string, rule: 'x' }]
    await edit($, project, ['example.py'], 'f1')
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('it must say'))).toBe(true)
  })
})

test('a slow question to the checkpoint command makes TCR unavailable and says why', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(1500)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('took longer than'))).toBe(true)
  }, { env: SLOW, hang: 'reason' })
})

test('a slow fingerprint makes TCR unavailable instead of failing silently', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(1500)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('took longer than'))).toBe(true)
  }, { env: SLOW, hang: 'fingerprint' })
})

test('a fingerprint that is too slow at a tool makes TCR unavailable and says why', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.hang = 'fingerprint'
    const call = $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(1500)
    await call
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('took longer than'))).toBe(true)
  }, { env: SLOW })
})

test('a check that runs past its limit holds the edit and the agent is told', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    await edit($, project, ['example.py'], 'f1')
    await project.clock.advance(1500)
    expect(project.workspace.commits).toEqual([])
    expect(told(project).some(text => text.includes('TCR left the latest change uncommitted') && text.includes('gave no result to judge by'))).toBe(true)
  }, { env: { OVERWATCH_TCR_TIMEOUT_SCALE: '0.002' }, hang: 'check' })
})

test('the limit of a whole judgement is longer than its slowest check and its slowest commit or revert together', () => {
  const questions = 5 * COMMAND_LIMITS_MS.fingerprint
  expect(JUDGEMENT_LIMIT_MS).toBeGreaterThanOrEqual(CHECK_TIMEOUT_MS + Math.max(COMMAND_LIMITS_MS.checkpoint, COMMAND_LIMITS_MS.revert) + questions)
  expect(JUDGEMENT_LIMIT_MS).toBeGreaterThan(CHECK_TIMEOUT_MS + COMMAND_LIMITS_MS.checkpoint)
})

test('a slow check and a slow commit, each within its own limit, are not stopped as one judgement that took too long', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(8000)
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
    expect(told(project).some(text => text.includes('took longer'))).toBe(false)
  }, { env: { OVERWATCH_TCR_TIMEOUT_SCALE: '0.01' }, judgeDelayMs: 5000, commandDelayMs: 1000 })
})

test('a judgement stuck in a step with no limit of its own is stopped at its limit and acts on nothing afterwards', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    project.workspace.settingsDelayMs = 5000
    await project.clock.advance(7000)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('the judgement of the edit took longer than'))).toBe(true)
    expect(project.workspace.commits).toEqual([])
  }, { env: { OVERWATCH_TCR_TIMEOUT_SCALE: '0.004' } })
})

test('a branch named main gets a notice once, at the start of TCR', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(SETTLE_MS)
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).filter(text => text.includes('overwatch: TCR checkpoints land on main')).length).toBe(1)
  }, { branch: 'main' })
})

test('a branch with another name gets no notice', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR checkpoints land on'))).toBe(false)
  }, { branch: 'feature' })
})

const CAN_WRITE_FILES = ['Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash', 'Task', 'Agent', 'Skill', 'SlashCommand', 'mcp__files__write_file']
const JUDGED_TOOLS = ['Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash']

for (const tool of CAN_WRITE_FILES) {
  test(`a file written at ${tool} is judged or makes TCR unavailable, and is never settled unseen`, async ($, on) => {
    await inSession({ $, on }, async project => {
      await $.turn.start({ text: 'go', turnId: 't1' })
      await project.clock.advance(DECIDE_MS)
      project.willChange(['written.py'], 'f1')
      await $.tool.call({ tool, file_path: '/repo/written.py' })
      await project.clock.advance(SETTLE_MS)
      const isJudged = JUDGED_TOOLS.includes(tool)
      expect(project.workspace.commits).toEqual(isJudged ? ['tcr: written.py'] : [])
      expect(told(project).some(text => text.includes('TCR is unavailable'))).toBe(!isJudged)
    })
  })
}

test('a project without checks commits nothing', async ($, on) => {
  await inSession({ $, on }, async project => {
    project.setReport(CLEAN)
    await edit($, project, ['example.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
  }, { withoutChecks: true })
})

test('a revert that fails with a file left behind pauses TCR with what the command said and reverts nothing more', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.workspace.revertError = "the revert in /repo left ['late.py']; restore them by hand"
    await edit($, project, ['a.py'], 'f1')
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes("left ['late.py']; restore them by hand"))).toBe(true)
    expect(project.workspace.asked.filter(each => each === 'revert').length).toBe(1)
    expect(told(project).filter(text => text.includes('TCR reverted'))).toEqual([])
  })
})

test('files that change just before the revert are not reverted: TCR stops and asks no revert', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    during(project, 'fingerprint', 3, () => personWrites(project))
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.asked).not.toContain('revert')
    expect(project.workspace.reverted).toEqual([])
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting'))).toBe(true)
  })
})

test('files that change just before the checkpoint are not committed: TCR stops and asks no checkpoint', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    during(project, 'fingerprint', 3, () => personWrites(project))
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.asked).not.toContain('checkpoint')
    expect(project.workspace.commits).toEqual([])
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting'))).toBe(true)
  })
})

test('a file that lands as the revert finishes is told to the agent with the revert, and TCR stops', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.workspace.hooks['revert#1:after'] = () => personWrites(project)
    await edit($, project, ['a.py'], 'f1')
    expect(told(project).filter(text => text.includes('TCR reverted')).length).toBe(1)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting'))).toBe(true)
    expect(project.workspace.asked.filter(each => each === 'revert').length).toBe(1)
  })
})

test('a file that lands as the checkpoint finishes stops TCR instead of settling it unseen', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.hooks['checkpoint#1:after'] = () => personWrites(project)
    await edit($, project, ['a.py'], 'f1')
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting'))).toBe(true)
  })
})

test('a held edit that is still what the files hold at the next turn start resumes TCR, and the next edit commits it too', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(told(project).some(text => text.includes('TCR held the edit'))).toBe(true)
    project.setReport(CLEAN)
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(SETTLE_MS)
    await $.turn.start({ text: 'once more', turnId: 't3' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).filter(text => text.includes('TCR is unavailable'))).toEqual([])
    await edit($, project, ['b.py'], 'f2')
    expect(project.workspace.commits).toEqual(['tcr: a.py, b.py'])
  }, { mute: true })
})

test('an edit that the revert itself wiped is told with the revert and not settled over', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    project.workspace.hooks['revert#1:after'] = () => {
      project.workspace.dirty = []
      project.workspace.fingerprint = project.workspace.committed
    }
    project.willChange(['a.py'], 'f1')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/a.py' })
    await project.clock.advance(JUDGE_MS + 100)
    project.willChange(['b.py'], 'f2')
    await $.tool.call({ tool: 'Edit', file_path: '/repo/b.py' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).filter(text => text.includes('TCR reverted')).length).toBe(1)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting'))).toBe(true)
  }, { commandDelayMs: 300 })
})

test('a held edit that the files no longer are at the next turn start does not resume TCR', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    project.setReport(CLEAN)
    personWrites(project)
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('uncommitted changes'))).toBe(true)
    await edit($, project, ['b.py'], 'f2')
    expect(project.workspace.commits).toEqual([])
  }, { mute: true })
})

test('an edit held for a check that gave no result is not resumed at the next turn start', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    await edit($, project, ['a.py'], 'f1')
    await project.clock.advance(1500)
    expect(told(project).some(text => text.includes('gave no result to judge by'))).toBe(true)
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('uncommitted changes'))).toBe(true)
  }, { env: { OVERWATCH_TCR_TIMEOUT_SCALE: '0.002' }, hang: 'check' })
})

const BEFORE_MOVE = 'a'.repeat(40)
const AFTER_MOVE = 'b'.repeat(40)
const METADATA = '/repo/.git'

/** The repository sits at BEFORE_MOVE on main, as the plugin reads it from its metadata directory. */
function onMain(workspace: Workspace) {
  workspace.files.set(`${METADATA}/HEAD`, 'ref: refs/heads/main\n')
  workspace.files.set(`${METADATA}/packed-refs`, `${BEFORE_MOVE} refs/heads/main\n`)
  assert(workspace.files.get(`${METADATA}/packed-refs`)?.startsWith(BEFORE_MOVE), `main was not placed at ${BEFORE_MOVE} in ${METADATA}`)
  assert(!workspace.files.has(`${METADATA}/logs/HEAD`), `${METADATA} starts with a history of moves; start it with none`)
}

/** A person's pull moves HEAD to AFTER_MOVE, which the reflog records as a pull, not a commit. */
function pull({ workspace }: Project) {
  workspace.files.set(`${METADATA}/refs/heads/main`, `${AFTER_MOVE}\n`)
  workspace.files.set(`${METADATA}/logs/HEAD`, `${BEFORE_MOVE} ${AFTER_MOVE} Dev <dev@example.com> 1700000001 +0000\tpull: Fast-forward\n`)
  assert(workspace.files.get(`${METADATA}/refs/heads/main`)?.trim() === AFTER_MOVE, `main did not move to ${AFTER_MOVE}`)
  assert(workspace.files.get(`${METADATA}/logs/HEAD`)?.includes('pull: '), 'the reflog does not record the move as a pull')
}

test('a held edit is not resumed at the next turn start after someone else moved HEAD, since its baselines are gone', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    expect(told(project).some(text => text.includes('TCR held the edit'))).toBe(true)
    pull(project)
    await $.tool.call({ tool: 'Bash', command: 'git pull' })
    project.setReport(CLEAN)
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('uncommitted changes'))).toBe(true)
    await edit($, project, ['b.py'], 'f2')
    expect(project.workspace.commits).toEqual([])
  }, { mute: true, prepare: onMain })
})

test('a check that outlives its limit neither commits nor reverts the edit, which is held', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    await project.clock.advance(1500)
    expect(project.workspace.commits).toEqual([])
    expect(project.workspace.reverted).toEqual([])
    expect(told(project).some(text => text.includes('gave no result to judge by'))).toBe(true)
  }, { env: { OVERWATCH_TCR_TIMEOUT_SCALE: '0.002' }, hang: 'check' })
})

test('a held edit resumes at the next turn start whenever any one check is held, though another check passes', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    project.setReport(CLEAN)
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).filter(text => text.includes('TCR is unavailable'))).toEqual([])
    await edit($, project, ['b.py'], 'f2')
    expect(project.workspace.commits).toEqual(['tcr: a.py, b.py'])
  }, { mute: true, withZig: true })
})

test('the checkpoint is asked to commit only the files that were judged', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.flags.filter(flags => flags.includes('--expect'))).toEqual([['--expect', 'f1']])
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
  })
})

test('files that change after the judgement and before the commit are not committed unjudged', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    during(project, 'checkpoint', 1, () => personWrites(project))
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.commits).toEqual([])
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('while TCR was committing, reverting'))).toBe(true)
  })
})

test('a checkpoint answer that says the files changed and also names a commit is reported and pauses TCR', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.workspace.rawCheckpoint = { commit: 'c'.repeat(40), refused: [], changed: true }
    await edit($, project, ['a.py'], 'f1')
    expect(told(project).some(text => text.includes('TCR is unavailable'))).toBe(true)
  })
})

test('a held edit does not resume TCR while another obstacle stands in its way', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    project.workspace.reasonOverride = 'HEAD is detached, so a checkpoint would belong to no branch'
    await $.turn.start({ text: 'again', turnId: 't2' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes('HEAD is detached'))).toBe(true)
  }, { mute: true })
})

test('a check held for want of an earlier result stays held when it fails worse, and is never reverted for it', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    project.unmute()
    await project.clock.advance(1000)
    project.setReport(WORSE)
    await edit($, project, ['b.py'], 'f2')
    expect(project.workspace.reverted).toEqual([])
    expect(project.workspace.commits).toEqual([])
    expect(told(project).filter(text => text.includes('TCR held the edit: zanity failed and has no earlier result')).length).toBeGreaterThan(0)
  }, { mute: true })
})

test('a held check that passes is judged by that pass: a failure after it is new and reverts', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(DECIDE_MS)
    project.setReport(WARNINGS)
    await edit($, project, ['a.py'], 'f1')
    project.setReport(CLEAN)
    await edit($, project, ['b.py'], 'f2')
    expect(project.workspace.commits).toEqual(['tcr: a.py, b.py'])
    project.setReport(WARNINGS)
    await edit($, project, ['c.py'], 'f3')
    expect(project.workspace.reverted).toEqual([['c.py']])
  }, { mute: true })
})

const BAD_SETTINGS: [string, Record<string, unknown>, string][] = [
  ['an empty path to allow', { allow: ['ok', ''] }, "field 'tcr.allow' is ['ok', ''], not a list of paths or globs that name files; an entry may not be empty or only wildcards"],
  ['a blank path to allow', { allow: ['  '] }, "field 'tcr.allow' is ['  '], not a list of paths or globs that name files; an entry may not be empty or only wildcards"],
  ['a path to allow of only wildcards', { allow: ['**/*'] }, "field 'tcr.allow' is ['**/*'], not a list of paths or globs that name files; an entry may not be empty or only wildcards"],
  ['a path to allow of only wildcards that match one character', { allow: ['?[]'] }, "field 'tcr.allow' is ['?[]'], not a list of paths or globs that name files; an entry may not be empty or only wildcards"],
  ['paths to allow that are no list', { allow: '.env' }, "field 'tcr.allow' is '.env', not a list of paths or globs that name files; an entry may not be empty or only wildcards"],
  ['a path to allow that is no text', { allow: [7] }, "field 'tcr.allow' is [7], not a list of paths or globs that name files; an entry may not be empty or only wildcards"],
  ['a field TCR does not know', { saveReverted: true, bogus: 1 }, "field 'tcr' has unknown fields ['bogus']; use saveReverted, hostLimitSeconds, allow"],
  ['a host limit of no time', { hostLimitSeconds: 0 }, "field 'tcr.hostLimitSeconds' is 0, not a positive number of seconds"],
  ['a host limit that is no number', { hostLimitSeconds: true }, "field 'tcr.hostLimitSeconds' is True, not a positive number of seconds"],
  ['a save setting that is no boolean', { saveReverted: 'yes' }, "field 'tcr.saveReverted' is 'yes', not a boolean; set it to true or false"],
]

for (const [what, tcr, wording] of BAD_SETTINGS) {
  test(`${what} in the settings of TCR pauses it with the Python hook's wording and commits nothing`, async ($, on) => {
    await inSession({ $, on }, async project => {
      await $.turn.start({ text: 'go', turnId: 't1' })
      await project.clock.advance(DECIDE_MS)
      await edit($, project, ['a.py'], 'f1')
      expect(told(project).some(text => text.includes('TCR is unavailable') && text.includes(`.overwatch.json ${wording}`))).toBe(true)
      expect(project.workspace.commits).toEqual([])
    }, { settings: { mode: 'tcr', tcr } })
  })
}

test('a tcr setting that is no object is reported with the fields it may hold', async ($, on) => {
  await inSession({ $, on }, async project => {
    await $.turn.start({ text: 'go', turnId: 't1' })
    await project.clock.advance(SETTLE_MS)
    expect(told(project).some(text => text.includes("field 'tcr' is [1], not an object; give it saveReverted, hostLimitSeconds, allow"))).toBe(true)
  }, { settings: { mode: 'tcr', tcr: [1] } })
})

test('every field TCR knows, set to a valid value, leaves it running', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
  }, { settings: { mode: 'tcr', tcr: { saveReverted: false, hostLimitSeconds: 30, allow: ['.env', 'fixtures/*'] } } })
})

test('a checker installed on the machine rather than in the project counts as a check', async ($, on) => {
  await inSession({ $, on }, async project => {
    await edit($, project, ['a.py'], 'f1')
    expect(project.workspace.oneShots).toEqual(['zanity'])
    expect(project.workspace.commits).toEqual(['tcr: a.py'])
  })
})
