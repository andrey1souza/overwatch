import { expect, test } from 'claude-code/testing'
import { SHARD_TIMEOUT_MS, gapsOf, runGaps } from './fixtures/tcr-overlap'

const SHARD = 2

test('a session that starts before the end of the last returns is told TCR is on, once, and its first edit is committed, in each gap of its share of 0 to 150 ms (shard 2 of 10)', { timeoutMs: SHARD_TIMEOUT_MS }, async ($, on) => {
  expect(await runGaps({ $, on }, SHARD)).toBe(gapsOf(SHARD).length)
})
