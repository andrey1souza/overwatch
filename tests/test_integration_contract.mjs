/** Every command-host profile must satisfy the behavior derived by IntegrationContract.tla. */
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { contractHost } from './fixtures/contract-host.mjs'

const traces = JSON.parse(readFileSync(new URL('./fixtures/integration-contract.json', import.meta.url), 'utf8'))
for (const trace of traces.filter(trace => ['codex', 'opencode1', 'opencode2', 'pi'].includes(trace.host))) {
  test(`contract ${trace.host}: ${trace.name}`, async t => {
    const host = await contractHost(t, trace)
    for (let index = 0; index < trace.events.length; index++) await host.step(trace.events[index], trace.expected[index + 1])
  })
}
