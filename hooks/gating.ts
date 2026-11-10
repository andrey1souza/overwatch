import { assert } from './assert'

/** Only explicitly protected actions are subject to a pre-tool failure gate. */
export type GatePolicy = {
  inherited?: 'allow' | 'block'
  protected_actions?: { tool: string; command?: string; checks?: string[] }[]
}

/** The shell command a tool call runs, from its command or cmd argument; empty when it has neither. */
export function commandOf(input: object): string {
  const arguments_ = input as Record<string, unknown>
  const command = String(arguments_.command ?? arguments_.cmd ?? '')
  assert(typeof command === 'string', `a tool call's command read as ${typeof command}; read it as text`)
  assert(arguments_.command !== undefined || arguments_.cmd !== undefined || command === '', `a tool call with no command gave ${JSON.stringify(command)}; give none as empty text`)
  return command
}

const COMMAND_BREAK = /&&|\|\||[;|\n]/
const WORD = /'([^']*)'|"([^"]*)"|(\S+)/g
/** Git subcommands that can bring files into the workspace from somewhere other than the agent's own edits. */
const FILE_BRINGING = new Set(['am', 'apply', 'checkout', 'cherry-pick', 'merge', 'pull', 'read-tree', 'rebase', 'reset', 'restore', 'revert', 'stash', 'switch'])
/** Git options that take the next word as their value, so it is not the subcommand. */
const GIT_VALUE_OPTIONS = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace'])
/** How many levels of quoting, as in sh -c 'sh -c "git merge x"', are read for a git command. */
const QUOTE_DEPTH = 3

/**
 * The git subcommand among `words`: the first word after git that is no option and no option's value. Undefined when
 * `words` do not run git, or run it with no subcommand.
 */
function gitSubcommand(words: readonly string[]): string | undefined {
  const at = words.findIndex(word => word.split('/').pop() === 'git')
  let found: string | undefined
  for (let index = at + 1; at >= 0 && index < words.length && found === undefined; index++) {
    const word = words[index]!
    if (GIT_VALUE_OPTIONS.has(word)) index++
    else if (word !== '' && !word.startsWith('-')) found = word
  }
  assert(found === undefined || at >= 0, `found ${found} in ${words} with no git before it; look only after git`)
  assert(found === undefined || !found.startsWith('-'), `${found} is an option, not a subcommand; skip options`)
  return found
}

/**
 * The git subcommand of each part of `command` that runs git, in order: parts split at &&, ||, ;, | and new lines,
 * global options such as -C dir skipped, and a quoted word holding a command, as in sh -c '...', read too.
 */
export function gitSubcommands(command: string): string[] {
  const names: string[] = []
  let texts = [command]
  for (let depth = 0; depth < QUOTE_DEPTH; depth++) {
    const nested: string[] = []
    for (const text of texts) {
      for (const part of text.split(COMMAND_BREAK)) {
        const words = [...part.matchAll(WORD)].map(found => found[1] ?? found[2] ?? found[3] ?? '')
        nested.push(...words.filter(word => word.includes(' ')))
        const name = gitSubcommand(words)
        if (name !== undefined) names.push(name)
      }
    }
    texts = nested
  }
  assert(names.every(name => name !== '' && !name.startsWith('-')), `subcommands ${names} include an option or nothing; keep only words`)
  assert(names.length <= command.length, `${names.length} subcommands from a command of ${command.length} characters; read each part once`)
  return names
}

/**
 * Whether `command` runs a git operation that can bring in files the agent did not write, as a merge, pull or
 * cherry-pick can. Such a command may leave a plain commit in the reflog while the workspace holds another's changes.
 */
export function bringsInFiles(command: string): boolean {
  const names = gitSubcommands(command)
  const found = names.some(name => FILE_BRINGING.has(name))
  assert(found === [...FILE_BRINGING].some(name => names.includes(name)), `${names} gave ${found}; test names against FILE_BRINGING`)
  assert(!found || names.length > 0, `${JSON.stringify(command)} brings in files but names no git subcommand; read the subcommands first`)
  return found
}

/** Undefined means unprotected; an empty list selects every check. */
export function protectedChecks(tool: string, input: object, policy: GatePolicy): string[] | undefined {
  const command = commandOf(input)
  const names = new Set<string>()
  let matched = false
  for (const action of policy.protected_actions ?? []) {
    if (action.tool.toLowerCase() !== tool.toLowerCase()) continue
    if (action.command !== undefined && !new RegExp(action.command).test(command)) continue
    matched = true
    if (!action.checks?.length) return []
    for (const name of action.checks) names.add(name)
  }
  assert(matched || names.size === 0, `no action protects ${tool} yet it selected checks ${[...names]}; select only from matching actions`)
  assert([...names].every(name => name !== ''), `${tool} selected an unnamed check from ${[...names]}; name each check in .overwatch.json`)
  return matched ? [...names] : undefined
}
