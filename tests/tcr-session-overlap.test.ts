import { expect, test } from 'claude-code/testing'
import { start, tcrProject } from './fixtures/tcr'
import { edit, toldPlain, END_MS, START_MS } from './fixtures/tcr-session'
import { ON_NOTICE, RESUME, SHARDS, SHARD_TIMEOUT_MS, SLOW_SEARCH_MS, endSession, expected, gapsOf, playOverlap, resumingElsewhere } from './fixtures/tcr-overlap'

const SLOW_SEARCH_GAPS_MS = [0, 1, 20, 150]
const GAPS_IN_SHARDS = [16, 15, 15, 15, 15, 15, 15, 15, 15, 15]
const ALL_GAPS = 151

test('the shards of the gaps hold each gap from 0 to 150 ms once', { timeoutMs: SHARD_TIMEOUT_MS }, () => {
  const all = Array.from({ length: SHARDS }, (_, shard) => gapsOf(shard))
  expect(all.map(gaps => gaps.length)).toEqual(GAPS_IN_SHARDS)
  expect(all.flat().sort((first, second) => first - second)).toEqual(Array.from({ length: ALL_GAPS }, (_, gap) => gap))
})

test('the same holds while the search of the old session is slow, so that the new session decides TCR before the old one ends', { timeoutMs: SHARD_TIMEOUT_MS }, async ($, on) => {
  const project = tcrProject(resumingElsewhere(on), { findDelayMs: SLOW_SEARCH_MS })
  const seen = []
  for (const gapMs of SLOW_SEARCH_GAPS_MS) {
    seen.push(await playOverlap($, project, { gapMs, windowMs: SLOW_SEARCH_MS + START_MS }))
    await endSession($, project)
  }
  expect(seen).toEqual(SLOW_SEARCH_GAPS_MS.map(expected))
})

test('a session that starts after the end of the last returned is told TCR is on, once, and its first edit is committed', { timeoutMs: SHARD_TIMEOUT_MS }, async ($, on) => {
  const project = tcrProject(on)
  await $.session.start(start)
  await project.clock.advance(START_MS)
  const ending = $.session.end(RESUME)
  await project.clock.advance(END_MS)
  await ending
  const before = toldPlain(project).length
  await $.session.start(start)
  await project.clock.advance(START_MS)
  await $.turn.start({ text: 'go', turnId: 't1' })
  await project.clock.advance(START_MS)
  const toldBeforeTheEdit = toldPlain(project).slice(before)
  await edit($, project, ['a.py'], 'f1')
  expect({ toldBeforeTheEdit, told: toldPlain(project).slice(before), logged: project.workspace.logged, commits: project.workspace.commits }).toEqual({ toldBeforeTheEdit: [ON_NOTICE], told: [ON_NOTICE], logged: [], commits: ['tcr: a.py'] })
})
