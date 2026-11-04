import assert from "node:assert/strict"
import { spawn, spawnSync } from "node:child_process"
import { randomUUID } from "node:crypto"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import test from "node:test"
import { fileURLToPath } from "node:url"

import { LLMock } from "@copilotkit/aimock"

const ROOT = fileURLToPath(new URL("..", import.meta.url))
const FAILURE = "failed after the latest change: fail"
const BREAK = "echo fail > result"
const FIX = "echo pass > result"
const TIMEOUT_MS = 240_000
/** The mock model accepts any key; a fresh one each run, since the agents need one but no real key is involved. */
const MOCK_KEY = randomUUID()

/** The agents overwatch integrates with: each one's tool for shell commands, how to call it, and how to start it on a prompt against the mock model at url. */
const AGENTS = {
  pi: {
    tool: "bash",
    call: command => ({ command }),
    setup: ({ home, url, prompt }) => {
      mkdirSync(join(home, ".pi", "agent"), { recursive: true })
      const provider = { baseUrl: `${url}/v1`, api: "openai-completions", apiKey: MOCK_KEY, compat: { supportsDeveloperRole: false, supportsReasoningEffort: false }, models: [{ id: "mock" }] }
      writeFileSync(join(home, ".pi", "agent", "models.json"), JSON.stringify({ providers: { mock: provider } }))
      return ["pi", "-p", "--no-session", "--model", "mock/mock", "-e", join(ROOT, "integrations", "pi.js"), prompt]
    },
  },
  opencode: {
    tool: "bash",
    call: command => ({ command, description: "run the command" }),
    setup: ({ project, url, prompt }) => {
      const provider = { npm: "@ai-sdk/openai-compatible", options: { baseURL: `${url}/v1`, apiKey: MOCK_KEY }, models: { mock: {} } }
      writeFileSync(join(project, "opencode.json"), JSON.stringify({ plugin: [`file://${join(ROOT, "integrations", "opencode.js")}`], provider: { mock: provider }, model: "mock/mock", permission: { bash: "allow" } }))
      return ["opencode", "run", prompt]
    },
  },
  codex: {
    tool: "exec_command",
    call: cmd => ({ cmd }),
    setup: ({ home, project, url, prompt, env }) => {
      mkdirSync(env.CODEX_HOME, { recursive: true })
      writeFileSync(join(env.CODEX_HOME, "config.toml"), [
        'model = "mock"',
        'model_provider = "mock"',
        "[model_providers.mock]",
        'name = "mock"',
        `base_url = "${url}/v1"`,
        'wire_api = "responses"',
        'env_key = "MOCK_API_KEY"',
        `[projects."${project}"]`,
        'trust_level = "trusted"',
      ].join("\n"))
      for (const argv of [["plugin", "marketplace", "add", ROOT], ["plugin", "add", "overwatch@overwatch"]]) {
        const installed = spawnSync("codex", argv, { cwd: home, env, encoding: "utf8" })
        assert.equal(installed.status, 0, `codex ${argv.join(" ")} failed: ${installed.stderr}`)
      }
      return ["codex", "exec", "--dangerously-bypass-hook-trust", "--skip-git-repo-check", "-s", "workspace-write", prompt]
    },
  },
}

/** Whether command runs on this machine's PATH. */
function installed(command) {
  assert(command !== "" && !/[\s/]/.test(command), `installed was given ${JSON.stringify(command)}; pass an agent's command name`)
  const { error } = spawnSync(command, ["--version"], { stdio: "ignore" })
  const isInstalled = error === undefined
  assert(isInstalled || error.code === "ENOENT", `${command} --version could not start for a reason other than being missing: ${error}`)
  return isInstalled
}

/** A git project whose check passes until its result file says fail. */
function project() {
  assert(existsSync(tmpdir()), `the temporary directory ${tmpdir()} is missing; the project is made inside it`)
  const dir = mkdtempSync(join(tmpdir(), "overwatch-agent-"))
  const init = spawnSync("git", ["init", "-q"], { cwd: dir, encoding: "utf8" })
  assert.equal(init.status, 0, `git init in ${dir} failed: ${init.stderr}`)
  writeFileSync(join(dir, "result"), "pass\n")
  writeFileSync(join(dir, ".overwatch.json"), JSON.stringify({ baseline: ["sh", "-c", "cat result"], summary: "^(?:pass|fail)$", failed: "fail" }))
  assert(existsSync(join(dir, ".git")), `${dir} has no .git after git init; overwatch only watches inside a repository`)
  assert.equal(readFileSync(join(dir, "result"), "utf8"), "pass\n", `${dir} must start with a passing check`)
  return dir
}

/** The mock model's script: every request is answered by the step it calls for, see `reply`. */
function script(mock, agent) {
  assert(Object.values(AGENTS).includes(agent), `script was given an agent using ${agent.tool} that is not one of AGENTS`)
  assert.equal(mock.getFixtures().length, 0, "script a fresh mock; an earlier fixture would answer before this one")
  mock.addFixture({ match: {}, response: req => reply(agent, JSON.stringify(req)) })
  const [fixture, ...extra] = mock.getFixtures()
  assert(extra.length === 0 && fixture?.response !== undefined, `the mock holds ${extra.length + 1} fixtures; script adds exactly one, answering every request`)
}

/**
 * The model's next step, given the whole request so far as text: break the check, fix it once overwatch has
 * reported the failure, then finish.
 */
function reply(agent, request) {
  assert(request.startsWith("{"), `the mock model was given ${request.slice(0, 80)}, not a JSON request`)
  const command = !request.includes(BREAK) ? BREAK : request.includes(FAILURE) && !request.includes(FIX) ? FIX : undefined
  if (command === undefined) return { content: "done" }
  const args = JSON.stringify(agent.call(command))
  assert(args.includes(JSON.stringify(command)), `the ${agent.tool} arguments ${args} do not carry the command ${command}`)
  return { toolCalls: [{ name: agent.tool, arguments: args }] }
}

/** Runs argv in cwd with env to its exit, giving its exit code and everything it printed. */
function run(argv, cwd, env) {
  assert(argv.length > 0 && typeof argv[0] === "string", `run was given ${JSON.stringify(argv)}; pass the command and its arguments`)
  assert(env.PATH !== undefined && env.HOME !== undefined, `run's env ${Object.keys(env)} lacks PATH or HOME; the agents need both`)
  return new Promise(resolve => {
    const [command, ...args] = argv
    const child = spawn(command, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"], timeout: TIMEOUT_MS })
    let output = ""
    child.stdout.on("data", chunk => { output += chunk })
    child.stderr.on("data", chunk => { output += chunk })
    child.on("error", error => resolve({ code: -1, output: String(error) }))
    child.on("close", code => resolve({ code, output }))
  })
}

for (const [name, agent] of Object.entries(AGENTS)) {
  test(`${name} hands the model overwatch's failure after an edit breaks the check, and finishes once it is fixed`, { skip: !process.env.CI && !installed(name) && `${name} is not on PATH`, timeout: TIMEOUT_MS + 30_000 }, async () => {
    const mock = new LLMock({ port: 0 })
    script(mock, agent)
    await mock.start()
    try {
      const dir = project()
      const home = mkdtempSync(join(tmpdir(), "overwatch-home-"))
      const env = { PATH: process.env.PATH, HOME: home, CODEX_HOME: join(home, ".codex"), MOCK_API_KEY: MOCK_KEY }
      const argv = agent.setup({ home, project: dir, url: mock.url, prompt: "break the check", env })
      const { code, output } = await run(argv, dir, env)
      const requests = mock.journal.getAll().map(entry => JSON.stringify(entry.body))
      const reported = requests.findIndex(body => body.includes(FAILURE))
      const fixed = requests.findIndex(body => body.includes(FIX))
      assert.equal(code, 0, `${name} exited with code ${code}:\n${output.slice(-3000)}`)
      assert(reported >= 0, `no request to the model carried overwatch's failure; ${name} printed:\n${output.slice(-3000)}`)
      assert(fixed > reported, `the model fixed the check at request ${fixed}, not after overwatch reported it at ${reported}`)
      assert.equal(readFileSync(join(dir, "result"), "utf8"), "pass\n")
    } finally {
      await mock.stop()
    }
  })
}
