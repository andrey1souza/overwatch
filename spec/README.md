# Overwatch's behavioral specification

The specification covers Claude Code, Codex, OpenCode 1, OpenCode 2, Pi, and Neovim. Their different host capabilities are explicit contracts, not exceptions in a shared test adapter.

There are two layers:

- `Overwatch.tla` models workspace versions, inherited baselines, runs, buffered results, and notification ordering. A read consumes the newest completed result, preserves unfinished output, and never makes the agent's edits inherited debt.
- `IntegrationContract.tla` models requests and responses, check contention, selected checks, inherited-finding policy, report delivery, completion feedback, shutdown, and restart. `IntegrationLifecycle.cfg` checks another bounded instance of this same contract with a restart. Both layers use `FailurePolicy.tla`; they do not maintain independent definitions of a regression.

Failure atoms represent what a runner exposes: named identities or count slots for summary-only checkers. The model cannot recover identities that the runner does not emit.

The interface model receives completed snapshots from the result layer. This is a composition boundary: it abstracts process bytes and workspace discovery, while the result model abstracts host callbacks. Runtime replays test both sides of that boundary. It is not an automatic refinement proof of the implementation.

## Host contracts

`Capabilities` in `IntegrationContract.tla` is authoritative for this table.

| Host | Protected calls during a check | Completion with guarded failures | Failure delivery | Persistent result status | Idle wakeup |
| --- | --- | --- | --- | --- | --- |
| Claude Code | Consult the latest available report | Block; deduplicate queued corrections | Batch into the turn, or submit a prompt when idle | Yes | Yes |
| Codex | Wait for the running check, then consult its result | Block on every Stop attempt | Hook context | No | No |
| OpenCode 1 | Wait for the running check, then consult its result | Warn; the API cannot refuse settlement | Tool output and idle warnings | No | No |
| OpenCode 2 | Wait for the running check, then consult its result | Queue a correction once per unchanged finding without work | Retain the report until the context hook consumes it | No | Yes |
| Pi | Wait for the running check, then consult its result | Continue with correction entries | Startup and tool-result context | Yes | No unsolicited wakeup |
| Neovim | No agent permission gate | No agent completion gate | Quickfix | Yes | No |

Every host must permit unprotected inspection and repair independently of check progress. Protected calls consult only their selected checks; an empty selection means all checks. Inherited findings guard only under the configured `block` policy. Deduplicating a message or correction never releases a failure gate. Completed tool work can permit one fresh correction request for unchanged findings.

The interface model represents notification counters as failure deliveries, not every log line, inherited-backlog notice, or recovery message. Completion decisions, queued corrections, warnings, and idle wakeups are distinct observations. In particular, an OpenCode 1 warning is never recorded as a blocked completion.

Shutdown clears pending delivery and visible status. Old callbacks cannot affect a later session or overwrite a user's quickfix list. Restart creates a fresh baseline and delivery state while retaining project policy. The snapshots and counters in a replay are observed through the host's actual callback or output channel.

## Checked properties and assumptions

The result layer checks result ordering, correspondence to a checked version, baseline isolation, eventual check completion, and eventual reporting of regressions.

The interface layer checks that inspection and repair responses are enabled while a check is held, that those responses are allowed, that host capabilities determine completion feedback, that editor paths never invent agent gates, that pending context is current, that queued corrections are deduplicated, and that closed sessions stay silent. It also checks response and delivery liveness.

Delivery liveness assumes check completion fairness, corresponding to the result layer's `RunsEnd` guarantee, and fair host servicing of report/context callbacks. Inspection does not require that assumption: the responder remains enabled even if a check is still held. The runtime contention tests hold a real command-hook session lock until after inspection returns. The model does not specify a universal wall-clock response deadline.

The committed model-checking configurations use finite bounds: two checks, a small failure alphabet, up to two requests, and up to one report change or session restart. These bounds exercise concurrency and policy combinations; passing them is not proof for every unbounded program execution or every external host version.

## Model-derived conformance tests

`python3 scripts/model_traces.py --jar /path/to/tla2tools.jar` generates the result-layer traces. `python3 scripts/integration_traces.py --jar /path/to/tla2tools.jar` generates the host contracts. Both scripts contain input programs; TLC computes the expected observations. `--check` rejects stale TypeScript or JSON fixtures.

- Claude runs the production `register` hook chain with a controlled host protocol.
- Codex uses real hook processes, commands, and file-backed state, including lock contention.
- OpenCode and Pi run native callbacks through the real shared checker. Held-check requests, deferred context, persistent status, and correction counts are observed at their public callbacks.
- Neovim runs the real watcher callbacks with controlled process I/O, checking status, editor actions, and late callbacks after shutdown.

CI model-checks both layers and the lifecycle instance, verifies generated expectations, and runs all six adapters. Changes should fix code against the contracts. A spec change requires an intentional behavior or capability change, not a workaround for a failing implementation.
