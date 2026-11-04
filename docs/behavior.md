# How Overwatch behaves

Generated from the executable scenarios in `tests/acceptance/catalog.py`.
Run `python3 scripts/scenarios.py` to verify the scenarios and regenerate this page.

## Verified behavior

### Claude retry limits

#### Claude stops repeating corrections when no work occurs

Tags: claude, completion, retry-loop.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L7)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [The correction turns repeatedly finish without using tools](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [Only one correction is requested and the turns settle while publication stays guarded](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### Denied publication does not restart Claude corrections

Tags: claude, completion, protected-actions.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L12)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [An empty correction settles and publication is repeatedly denied](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L305).
- **Then** [Publication never executes and no further correction is submitted](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

### Claude recovery and changed findings

#### Claude requests a fresh correction after work resumes

Tags: claude, completion, repair.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L17)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [An empty correction settles and the agent resumes inspection in a new turn](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L278).
- **Then** [Inspection succeeds and the remaining warnings request another correction](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### Changed findings give Claude one fresh correction

Tags: claude, completion, new-findings.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L22)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [An empty correction settles and the watcher reports additional warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L323).
- **Then** [The new warnings request one fresh correction and empty retries settle](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### Passing checks release a queued Claude correction

Tags: claude, completion, recovery.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L27)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [The checks recover during the correction turn and the agent attempts publication](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [Publication and completion succeed without another correction](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### A recovered Claude failure can request correction again

Tags: claude, completion, failure-cycle.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L32)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [The failure is corrected and the same warnings return after another change](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [The returning failure gets one fresh correction and empty retries settle](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

### Claude interruption

#### An interrupted Claude turn settles without a correction

Tags: claude, completion, interruption.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L37)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [The agent turn is interrupted](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [The turn settles without submitting a correction prompt](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### An interrupted Claude correction stays stopped

Tags: claude, completion, interruption, retry-loop.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L42)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [The correction turn is interrupted and the watcher repeats the same failure](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [The interrupted turn settles without submitting another correction](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### Claude API-error completion does not queue a correction

Tags: claude, completion, api-error, rate-limit.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L47)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [Claude completes with the runtime's API-error event and the watcher repeats its failure](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [The unsuccessful turn settles without requesting another correction](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### A user turn restores Claude reporting after interruption

Tags: claude, completion, user-resume.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L52)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [The turn is interrupted and the user starts a new investigation turn](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [Inspection succeeds and the remaining failure requests a fresh correction](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

### Claude multiple watchers

#### Claude gates follow each watcher's recovery

Tags: claude, completion, check-scope, recovery.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L57)

- **Given** [Claude has failing Zig and Zanity watchers and publication requires only Zanity](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L416).
- **When** [Zanity recovers before Zig and the agent attempts publication and completion](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [Publication follows Zanity while completion waits for both watchers to recover](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

### Claude session lifecycle

#### A restarted Claude session resets correction suppression

Tags: claude, session, restart.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L62)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [A correction is interrupted and the session is ended and started again](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [The new session reports its new failure and requests a fresh correction](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

#### A resumed Claude session resets correction suppression

Tags: claude, session, resume.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L67)

- **Given** [A previously passing Claude watcher now reports warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L236).
- **When** [A correction is interrupted and Claude resumes the session](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L248).
- **Then** [The resumed session reports its new failure and requests a fresh correction](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L255).

### Failure gates

#### Repair remains possible during failure

Tags: repair, warnings.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L72)

- **Given** [A previously passing check now reports 29 warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L82).
- **When** [The agent lists, searches, reads, edits, and runs check commands](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L90).
- **Then** [Every repair operation is allowed and the edited file can be read](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L108).

#### Protected actions stay protected on every attempt

Tags: protected-actions.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L77)

- **Given** [A previously passing check now reports 29 warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L82).
- **When** [The agent attempts configured publication actions twice](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L115).
- **Then** [Every attempt is denied with the failing check's summary](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L123).

#### Completion stays guarded during a continuation

Tags: completion.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L82)

- **Given** [A previously passing check now reports 29 warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L82).
- **When** [The agent repeatedly tries to finish, including a Stop continuation](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L131).
- **Then** [Every completion attempt remains guarded](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L137).

#### Successful checks release the failure gate

Tags: recovery.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L87)

- **Given** [A previously passing check now reports 29 warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L82).
- **When** [The repaired code passes its check](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L143).
- **Then** [Completion and configured publication actions are allowed](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L150).

#### Protected actions consult their relevant checks

Tags: check-scope.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L105)

- **Given** [Publication requires the passing Go check](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L202).
- **And** [The unrelated Zanity check develops 29 warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L82).
- **When** [The agent attempts publication and completion](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L176).
- **Then** [Publication is allowed while overall completion remains guarded](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L212).

#### Inspection never starts a check

Tags: repair, check-execution.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L111)

- **Given** [A previously passing check now reports 29 warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L82).
- **And** [The checker is unavailable for a new run](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L217).
- **When** [The agent lists files](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L224).
- **Then** [Listing succeeds without rerunning or replacing the failed result](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L230).

### Legacy debt

#### Inherited debt does not absorb new failures

Tags: baseline, regressions.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L92)

- **Given** [The session starts with 29 inherited warnings under the default policy](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L156).
- **When** [The agent attempts completion and publication](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L176).
- **Then** [Completion and configured publication actions are allowed](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L184).
- **When** [A change increases the warning count to 30](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L189).
- **Then** [The new failure guards completion](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L196).

#### Strict inherited-finding policy still permits inspection

Tags: baseline, strict.
[View test](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L99)

- **Given** [Inherited findings are configured to block](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L163).
- **And** [The session starts with 29 inherited warnings](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L156).
- **When** [The agent attempts completion, publication, and inspection](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L176).
- **Then** [Completion and publication are denied while inspection remains available](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/world.py#L170).

## Proposed behavior (not implemented)

### Changed-code cleanup

#### Editing a function activates its inherited findings

Tags: changed-code.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L117)

- **Given** A function has three inherited warnings and another function has twelve.
- **When** The agent changes the first function.
- **Then** Its three warnings require cleanup while the other twelve remain backlog.
- **And** Repair tools remain available while completion is guarded.

#### Changed code activates the cross-file findings it participates in

Tags: changed-code, cross-file.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L128)

- **Given** An inherited finding involves functions in two files.
- **When** The agent changes one of those participating functions.
- **Then** That cross-file finding requires cleanup while unrelated findings remain backlog.

#### Disabling a rule is not a resolution

Tags: rules, resolution.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L138)

- **Given** A finding requires cleanup in changed code.
- **When** Its rule is disabled or its source path is excluded.
- **Then** The finding is not recorded as fixed.

### Finding age

#### Moving a finding preserves its identity and age

Tags: identity, age.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L123)

- **Given** An inherited finding has a recorded identity and first-seen date.
- **When** Unrelated lines are inserted above it or the agent session restarts.
- **Then** Its identity and age remain unchanged and movement alone does not activate it.

#### Untouched debt escalates at its configured deadline

Tags: age, deadlines.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L133)

- **Given** An inherited finding has a project-configured deadline.
- **When** A check runs after that deadline.
- **Then** The finding escalates according to project policy without resetting its first-seen date.

### Modes and limits

#### Advisory mode reports failures without guarding

Tags: modes, advisory.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L78)

- **Given** The project runs overwatch in advisory mode.
- **And** A change introduces a new failure.
- **When** The agent tries to finish.
- **Then** Completion is allowed.
- **And** The agent is told of the new failure.

#### Strict mode waits for a result of the latest edit

Tags: modes, strict.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L85)

- **Given** The project runs overwatch in strict mode.
- **When** The agent edits and tries to finish before the checks have run on that edit.
- **Then** Completion is guarded until a result of the latest edit passes.

#### Edits wait while the agent is too far ahead of its checks

Tags: limits, max-ahead.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L90)

- **Given** The project allows the agent three edits ahead of its latest result.
- **And** Checks are still running on an earlier edit.
- **When** The agent attempts a fourth edit.
- **Then** The edit waits for a result.
- **And** Reading and running checks remain available.

#### Strict mode hands a lingering failure to a person

Tags: limits, red-budget.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L97)

- **Given** The project runs overwatch in strict mode with a budget of edits while red.
- **And** A new failure has been reported.
- **When** The agent spends its budget without clearing the failure.
- **Then** The turn stops and waits for a person instead of continuing.

#### A check that exceeds its time limit fails

Tags: limits, timeout.
[View proposal](https://github.com/benomahony/overwatch/blob/main/tests/acceptance/catalog.py#L103)

- **Given** The project sets a time limit on checks.
- **When** A run exceeds it.
- **Then** The run is stopped and reported to the agent as a timeout.
- **And** A timeout is never recorded as inherited backlog.
