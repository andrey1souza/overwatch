import { expect, test } from 'claude-code/testing'
import { start, ended, gatingProject } from './fixtures/gating'

test('a failing watcher permits diagnostic and repair tool calls', async ($, on) => {
  const { clock, calls } = gatingProject(on)
  try {
    await $.session.start(start)
    await clock.advance(1500)
    await $.tool.call({ tool: 'Read', file_path: '/repo/example.py' })
    await $.tool.call({ tool: 'Edit', file_path: '/repo/example.py', old_string: 'a', new_string: 'b' })
    for (const command of ['rg --files', 'cat example.py', 'zanity check .', 'pytest']) {
      await $.tool.call({ tool: 'Bash', command })
    }
    expect(calls).toEqual(['Read', 'Edit', 'Bash', 'Bash', 'Bash', 'Bash'])
  } finally {
    const ending = $.session.end({ reason: 'prompt_input_exit', sessionId: 's1', resume: { id: 's1' } })
    await clock.advance(1000)
    await ending
  }
})

for (const inherited of ['allow', 'block'] as const) {
  test(`inherited watcher findings follow the ${inherited} policy without denying inspection`, async ($, on) => {
    const warnings = 'zanity: 0 errors and 29 warnings in 1 of 1 files\n'
    const { clock, calls } = gatingProject(on, { inherited, initial: warnings })
    try {
      await $.session.start(start)
      await clock.advance(1500)
      await $.tool.call({ tool: 'Read', file_path: '/repo/example.py' })
      try { await $.tool.call({ tool: 'Publish' }) } catch { /* Protected actions may be denied under the strict policy. */ }
      await $.turn.complete(ended)
      expect(calls).toEqual(inherited === 'block' ? ['Read'] : ['Read', 'Publish', 'complete'])
    } finally {
      const ending = $.session.end({ reason: 'prompt_input_exit', sessionId: 's1', resume: { id: 's1' } })
      await clock.advance(1000)
      await ending
    }
  })
}

test('publication stays protected while an idle correction settles, then passing checks release the gate', async ($, on) => {
  const { clock, calls, repair } = gatingProject(on)
  try {
    await $.session.start(start)
    await clock.advance(1500)
    await $.turn.start({ text: 'finish', turnId: 't1' })
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try { await $.tool.call({ tool: 'Publish' }) } catch { /* A denial must not reach the tool implementation. */ }
      try { await $.turn.complete(ended) } catch { /* A denial must not reach the completion implementation. */ }
    }
    // A correction turn with no tool progress must be allowed to settle.
    expect(calls).toEqual(['complete'])
    repair()
    await clock.advance(1000)
    await $.tool.call({ tool: 'Publish' })
    await $.turn.complete(ended)
    expect(calls).toEqual(['complete', 'Publish', 'complete'])
  } finally {
    const ending = $.session.end({ reason: 'prompt_input_exit', sessionId: 's1', resume: { id: 's1' } })
    await clock.advance(1000)
    await ending
  }
})
