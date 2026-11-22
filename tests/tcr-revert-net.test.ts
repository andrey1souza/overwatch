import { expect, test } from 'claude-code/testing'
import { tcrProject } from './fixtures/tcr'
import { edit, LONG_TEST_MS } from './fixtures/tcr-session'
import { FAILED_EXIT, NET_TEST_MS, WORKED_EXIT, expectToldOfRevert, play, type Printed } from './fixtures/tcr-net'
import type { Project } from './fixtures/tcr-session'

const HUGE_CHARACTERS = 1_000_000
const REPLACEMENT = '�'

const printing = (code: number, over: Record<string, unknown>): Printed => answer => ({ code, text: JSON.stringify({ ...answer, ...over }) })

/** An answer of the revert command, how many steps the plugin makes after it spawned the command, and whether the plugin can learn the ref from it and whether it fails the revert by design. */
type Shape = { name: string; steps: number; opening: 'done' | 'tried'; as?: Record<string, unknown>; prints?: Printed; names: boolean; isFailing: boolean }

const SHAPES: Shape[] = [
  { name: 'an answer that is right', steps: 4, opening: 'done', names: true, isFailing: false },
  { name: 'a JSON array and exit 0', steps: 4, opening: 'tried', prints: () => ({ code: WORKED_EXIT, text: '[]' }), names: false, isFailing: true },
  { name: 'a JSON array and exit 1', steps: 4, opening: 'tried', prints: () => ({ code: FAILED_EXIT, text: '[]' }), names: false, isFailing: true },
  { name: 'exit 0 with an error field', steps: 4, opening: 'done', prints: printing(WORKED_EXIT, { error: 'it broke' }), names: true, isFailing: true },
  { name: 'exit 1 with an empty ref', steps: 4, opening: 'done', prints: printing(FAILED_EXIT, { error: 'it broke', saved: '' }), names: false, isFailing: true },
  { name: 'exit 1 with the ref as a number', steps: 4, opening: 'done', prints: printing(FAILED_EXIT, { error: 'it broke', saved: 7 }), names: false, isFailing: true },
  { name: 'reverted given as text', steps: 4, opening: 'done', as: { reverted: 'true' }, names: true, isFailing: true },
  { name: 'reverted false with a done reset and a ref', steps: 4, opening: 'done', as: { reverted: false }, names: true, isFailing: true },
  { name: 'empty output', steps: 3, opening: 'tried', prints: () => ({ code: WORKED_EXIT, text: '' }), names: false, isFailing: true },
  { name: 'two lines of JSON', steps: 4, opening: 'tried', prints: answer => ({ code: WORKED_EXIT, text: `${JSON.stringify(answer)}\n${JSON.stringify(answer)}\n` }), names: false, isFailing: true },
  { name: 'output that is no UTF-8 text', steps: 4, opening: 'tried', prints: () => ({ code: FAILED_EXIT, text: `${REPLACEMENT}${REPLACEMENT}{` }), names: false, isFailing: true },
  { name: 'output of a million characters', steps: 4, opening: 'tried', prints: () => ({ code: FAILED_EXIT, text: 'x'.repeat(HUGE_CHARACTERS) }), names: false, isFailing: true },
]

const prepared = (shape: Shape) => (project: Project) => {
  project.workspace.revertAs = shape.as
  project.workspace.revertPrints = shape.prints
}

/**
 * The class check of the revert path for host throws, which `steppingSpawn` turns into failures of the command, so that the
 * plugin's own boundary, `revertTcr`, is pinned by tests/tcr-boundaries.test.ts: for each answer the command can print, the host throws at each step the plugin makes
 * after it spawned the revert command, and in every run the tree was reverted, the agent is told a revert with the ref it
 * could learn, TCR is paused, and nothing but the thrown error is left unhandled, at most once.
 */
for (const shape of SHAPES) {
  test(`${shape.name}: host throws during revert are told as failures of the command, at any step after it was spawned: the agent is told of the revert, and TCR is paused`, { timeoutMs: NET_TEST_MS + LONG_TEST_MS }, async ($, on) => {
    const project = tcrProject(on)
    const entry = (current: Project) => edit($, current, ['a.py'], 'f1')
    const base = { $, project, prepare: prepared(shape), entry }
    const plain = await play({ ...base, throwAt: undefined })
    expectToldOfRevert(plain, { throwAt: undefined, names: shape.names, isFailing: shape.isFailing, opening: shape.opening })
    expect({ shape: shape.name, reverted: plain.project.workspace.reverted.length, isSpawned: Number.isFinite(plain.revertStep) }).toEqual({ shape: shape.name, reverted: 1, isSpawned: true })
    const stepsAfterTheRevert = plain.steps.length - plain.revertStep
    expect({ shape: shape.name, stepsAfterTheRevert }).toEqual({ shape: shape.name, stepsAfterTheRevert: shape.steps })
    let played = 0
    for (let throwAt = plain.revertStep + 1; throwAt <= plain.steps.length; throwAt += 1) {
      expectToldOfRevert(await play({ ...base, throwAt }), { throwAt, names: shape.names, isFailing: shape.isFailing, opening: shape.opening })
      played += 1
    }
    expect({ shape: shape.name, played }).toEqual({ shape: shape.name, played: stepsAfterTheRevert })
  })
}
