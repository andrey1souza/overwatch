/**
 * Helpers the Claude Code hook's tests share: watchers from watchers.json, reading their output, and a repository
 * tree to search for sub-projects.
 *
 * Copyright (c) 2026 benomahony. SPDX-License-Identifier: MIT
 */
import { assert } from '../../hooks/assert'
import { WATCHERS, readReport, statusLine } from '../../hooks/watchers'

/**
 * The one watcher in watchers.json named `name`, with `marker` when the name has several. A test names one that
 * exists, so any other is a mistake in the test, thrown as such.
 */
export const watcherNamed = (name: string, marker?: string) => {
  const found = WATCHERS.filter(watcher => watcher.name === name && (marker === undefined || watcher.marker === marker))
  const [first] = found
  if (first === undefined || found.length !== 1) {
    throw new Error(`${found.length} watchers in watchers.json are named ${name} with marker ${marker}; name exactly one, with its marker when the name has several`)
  }
  assert(first.name === name, `watcherNamed(${name}) found ${first.name}; filter by name`)
  assert(marker === undefined || first.marker === marker, `watcherNamed(${name}, ${marker}) found marker ${first.marker}; filter by marker`)
  return first
}

/** The status line and failures a watcher reads from `output`, which must hold a report. */
export const readResult = (watcher: (typeof WATCHERS)[number], output: string, code?: number) => {
  const report = readReport(watcher, output, code)
  assert(report !== undefined, `${watcher.name} read no report from ${output.length} characters of output; check its summary pattern`)
  const status = statusLine(watcher, report)
  assert(status.startsWith(`overwatch ${watcher.name} `), `status ${status} does not name ${watcher.name}`)
  return [status, report.failures]
}

/** Slots look for their watchers side by side, so the order they start in is not fixed. */
export const sorted = (argvs: string[][]) => {
  const result = [...argvs].sort((a, b) => a.join(' ').localeCompare(b.join(' ')))
  assert(result.length === argvs.length, `sorting ${argvs.length} commands gave ${result.length}; sort a copy of all of them`)
  assert(result.every((argv, index) => index === 0 || (result[index - 1] ?? []).join(' ').localeCompare(argv.join(' ')) <= 0), `commands ${result} are out of order; compare them as joined lines`)
  return result
}

/** A repository with sub-projects at several depths, and a node_modules that must not be searched. */
export const SUBPROJECT_TREE: Record<string, string[]> = {
  '/repo': ['services/', 'tools/', 'node_modules/', '.git/', 'README.md'],
  '/repo/services': ['api/', 'web/'],
  '/repo/services/api': ['go.mod', 'main.go'],
  '/repo/services/web': ['package.json', 'bun.lock'],
  '/repo/tools': ['Cargo.toml', 'nested/'],
  '/repo/tools/nested': ['Cargo.toml'],
  '/repo/node_modules': ['left-pad/'],
  '/repo/node_modules/left-pad': ['go.mod'],
}

/**
 * A fake directory listing's entry for `name`, a directory when it ends with a slash. The slash is this fixture's
 * own notation for SUBPROJECT_TREE, not a path separator, so it reads the same on every platform.
 */
export const treeEntry = (name: string) => {
  if (!/^[^/]+\/?$/.test(name)) throw new Error(`tree entry ${JSON.stringify(name)} is not one name with an optional trailing slash; write it as name or name/`)
  const entry = {
    name: name.replace(/\/$/, ''),
    kind: name.endsWith('/') ? ('dir' as const) : ('file' as const),
    size: 0,
    mtimeMs: 0,
    isLink: false,
  }
  assert(entry.name !== '' && !entry.name.includes('/'), `tree entry ${name} must be one name, with a trailing / for a directory`)
  assert((entry.kind === 'dir') === name.endsWith('/'), `tree entry ${name} read as a ${entry.kind}`)
  return entry
}
